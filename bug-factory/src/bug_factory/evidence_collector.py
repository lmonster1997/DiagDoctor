"""
Evidence Collector — fetches logs and traces from Loki and Tempo.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import structlog
from aiohttp import ClientTimeout, TCPConnector

from bug_factory.schema import CollectedEvidence, LogEntry, TraceSpan

logger = structlog.get_logger(__name__)
_DEFAULT_TIMEOUT = ClientTimeout(total=60, connect=10)
_LOKI_MAX_PER_PAGE = 5000
_TEMPO_MIN_TRACES = 20


class EvidenceCollector:
    """Collect logs and traces from Grafana Loki and Tempo."""

    def __init__(
        self,
        loki_url: str = "http://localhost:3100",
        tempo_url: str = "http://localhost:3200",
        output_dir: Path | None = None,
    ) -> None:
        self.loki_url = loki_url.rstrip("/")
        self.tempo_url = tempo_url.rstrip("/")
        if output_dir is None:
            output_dir = (
                Path(__file__).resolve().parent.parent.parent.parent / "bug-factory" / "output"
            )
        self.output_dir = Path(output_dir)
        logger.info(
            "EvidenceCollector initialised",
            loki_url=self.loki_url,
            tempo_url=self.tempo_url,
            output_dir=str(self.output_dir),
        )

    async def collect(
        self,
        recipe_id: str,
        start: datetime,
        end: datetime,
        services: list[str] | None = None,
        expected_evidence: Any = None,
        browser_errors: list[Any] | None = None,
    ) -> CollectedEvidence:
        """Collect logs and traces for *recipe_id* within the time window.

        Args:
            expected_evidence: Optional :class:`ExpectedEvidence` from the
                recipe's trigger.  When provided, evidence collection is
                clipped accordingly — e.g. frontend-crash-only cases skip
                trace fetching, and backend-only cases skip browser errors.

        Note:
            ``diff_evidence`` is NOT collected here (it would embed
            ground-truth expectations that a real user cannot provide).
            The ``collect_diff`` trigger action still runs as a self-check
            during trigger execution, but its output is discarded after
            verification — it is never persisted as an evidence file.
        """
        if services is None:
            services = ["demo-backend", "demo-frontend"]

        # ── Evidence clipping based on expected_evidence ──────────────
        skip_traces = False
        if expected_evidence is not None:
            # Import here to avoid circular dependency at module level.
            from bug_factory.schema import ExpectedEvidence

            if (
                isinstance(expected_evidence, ExpectedEvidence)
                and expected_evidence.frontend_spans == "none"
            ):
                skip_traces = True
                logger.info(
                    "Skipping trace collection per expected_evidence",
                    recipe_id=recipe_id,
                    frontend_spans=expected_evidence.frontend_spans,
                )

        logger.info(
            "Starting evidence collection",
            recipe_id=recipe_id,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        async with aiohttp.ClientSession(
            timeout=_DEFAULT_TIMEOUT, connector=TCPConnector(limit=5)
        ) as session:
            if skip_traces:
                logs, _ = await asyncio.gather(
                    self._fetch_logs(session, start, end, services),
                    asyncio.sleep(0),  # no-op instead of trace fetch
                )
                traces: list[TraceSpan] = []
            else:
                logs, traces = await asyncio.gather(
                    self._fetch_logs(session, start, end, services),
                    self._fetch_traces(session, start, end, services),
                )
        from bug_factory.schema import BrowserError  # noqa: F811

        evidence = CollectedEvidence(
            recipe_id=recipe_id,
            logs=logs,
            traces=traces,
            browser_errors=[
                BrowserError(**be) if isinstance(be, dict) else be for be in (browser_errors or [])
            ],
            time_window=(start.isoformat(), end.isoformat()),
        )
        logger.info(
            "Evidence collection complete",
            recipe_id=recipe_id,
            log_count=len(logs),
            trace_count=len(traces),
        )
        self._save_evidence(recipe_id, evidence)
        return evidence

    async def _fetch_logs(
        self,
        session: aiohttp.ClientSession,
        start: datetime,
        end: datetime,
        services: list[str],
    ) -> list[LogEntry]:
        start_ns = str(int(start.timestamp() * 1_000_000_000))
        end_ns = str(int(end.timestamp() * 1_000_000_000))
        svc_regex = "|".join(services)
        query = f'{{service_name=~"{svc_regex}"}}'
        entries: list[LogEntry] = []
        last_end = end_ns
        for _page in range(50):
            params = {
                "query": query,
                "start": start_ns,
                "end": last_end,
                "limit": str(_LOKI_MAX_PER_PAGE),
                "direction": "backward",
            }
            try:
                resp = await session.get(f"{self.loki_url}/loki/api/v1/query_range", params=params)
                if resp.status == 404:
                    logger.warning("Loki 404 — not running", url=self.loki_url)
                    break
                resp.raise_for_status()
                data = await resp.json()
            except aiohttp.ClientError as exc:
                logger.warning("Loki request failed", error=str(exc))
                break
            streams = data.get("data", {}).get("result", [])
            if not streams:
                break
            prev = len(entries)
            min_ns = None
            for stream in streams:
                labels = stream.get("stream", {})
                for ts_ns, line in stream.get("values", []):
                    entries.append(
                        LogEntry(
                            timestamp=_ns_to_iso(ts_ns),
                            labels=dict(labels),
                            line=line,
                        )
                    )
                    if min_ns is None or ts_ns < min_ns:
                        min_ns = ts_ns
            new = len(entries) - prev
            if new < _LOKI_MAX_PER_PAGE or min_ns is None:
                break
            last_end = min_ns
        return entries

    async def _fetch_traces(
        self,
        session: aiohttp.ClientSession,
        start: datetime,
        end: datetime,
        services: list[str],
    ) -> list[TraceSpan]:
        start_epoch = str(int(start.timestamp()))
        end_epoch = str(int(end.timestamp()))
        params: dict[str, str] = {
            "start": start_epoch,
            "end": end_epoch,
            "limit": str(_TEMPO_MIN_TRACES * 3),
        }
        trace_ids: list[str] = []
        try:
            resp = await session.get(f"{self.tempo_url}/api/search", params=params)
            if resp.status == 404:
                logger.warning("Tempo 404 — not running")
                return []
            resp.raise_for_status()
            # Tempo may return JSON without a proper Content-Type header.
            text = await resp.text()
            data = json.loads(text)
            trace_ids = [t.get("traceID", "") for t in data.get("traces", []) if t.get("traceID")]
        except (aiohttp.ClientError, json.JSONDecodeError) as exc:
            logger.warning("Tempo search failed", error=str(exc))
            return []
        if not trace_ids:
            return []
        all_spans: list[TraceSpan] = []
        for i in range(0, len(trace_ids), 5):
            results = await asyncio.gather(
                *[self._fetch_single_trace(session, tid) for tid in trace_ids[i : i + 5]]
            )
            for s in results:
                all_spans.extend(s)
        return self._select_representative(all_spans, services)

    async def _fetch_single_trace(
        self, session: aiohttp.ClientSession, trace_id: str
    ) -> list[TraceSpan]:
        try:
            resp = await session.get(f"{self.tempo_url}/api/traces/{trace_id}")
            if resp.status == 404:
                return []
            resp.raise_for_status()
            # Tempo may return JSON without a proper Content-Type header.
            text = await resp.text()
            data = json.loads(text)
        except (aiohttp.ClientError, json.JSONDecodeError):
            return []
        batches = data.get("batches", []) or data.get("data", {}).get("batches", [])
        spans: list[TraceSpan] = []
        for batch in batches:
            resource = batch.get("resource", {})
            rattrs = _flatten_attrs(resource.get("attributes", []))
            svc = rattrs.get("service.name", "")
            for scope in batch.get("scopeSpans", []) or batch.get(
                "instrumentationLibrarySpans", []
            ):
                for sd in scope.get("spans", []):
                    sattrs = _flatten_attrs(sd.get("attributes", []))
                    # JSON values from Tempo are strings — convert to int.
                    end_ns = int(sd.get("endTimeUnixNano", 0))
                    start_ns = int(sd.get("startTimeUnixNano", 0))
                    dur = int(sd.get("durationNano", 0)) or (end_ns - start_ns)
                    spans.append(
                        TraceSpan(
                            trace_id=trace_id,
                            span_id=_b64_to_hex(sd.get("spanId") or sd.get("spanID", "")),
                            parent_span_id=_b64_to_hex(
                                sd.get("parentSpanId") or sd.get("parentSpanID", "")
                            ),
                            operation_name=sd.get("name", ""),
                            service_name=svc or sattrs.get("service.name", ""),
                            start_time=_ns_to_iso(str(start_ns)),
                            duration_ms=dur / 1_000_000,
                            status="error" if sd.get("status", {}).get("code", 0) == 2 else "ok",
                            attributes=sattrs,
                        )
                    )
        return spans

    def _select_representative(
        self, spans: list[TraceSpan], services: list[str]
    ) -> list[TraceSpan]:
        if not spans:
            return []
        traces: dict[str, list[TraceSpan]] = {}
        for sp in spans:
            traces.setdefault(sp.trace_id, []).append(sp)
        stats = [
            (tid, sum(s.duration_ms for s in ts), any(s.status == "error" for s in ts))
            for tid, ts in traces.items()
        ]
        slow = {t[0] for t in sorted(stats, key=lambda x: x[1], reverse=True)[:10]}
        err = {t[0] for t in stats if t[2] and t[0] not in slow}
        # Always include traces from target services (demo-backend / demo-frontend)
        target = {
            sp.trace_id
            for sp in spans
            if any(sp.service_name == s or sp.service_name.startswith(s) for s in services)
        }
        return [sp for sp in spans if sp.trace_id in (slow | err | target)]

    def _save_evidence(self, recipe_id: str, evidence: CollectedEvidence) -> None:
        d = self.output_dir / recipe_id / "evidence"
        d.mkdir(parents=True, exist_ok=True)
        (d / "logs.json").write_text(
            json.dumps([e.model_dump() for e in evidence.logs], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (d / "traces.json").write_text(
            json.dumps([t.model_dump() for t in evidence.traces], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # ── Save browser_errors.json (independent channel, see §13.1.2) ──
        # Always write — empty array signals "no frontend crash" to Doctor.
        (d / "browser_errors.json").write_text(
            json.dumps(
                [b.model_dump() for b in evidence.browser_errors],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info("Evidence saved", recipe_id=recipe_id, path=str(d))


def _ns_to_iso(ns_str: str) -> str:
    try:
        ns = int(ns_str)
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()  # noqa: UP017
    except (ValueError, OSError):
        return ns_str


def _b64_to_hex(v: str) -> str:
    """Decode a base64-encoded span/parentSpanId to hex.

    OTLP/JSON transports ``spanId`` and ``parentSpanId`` as base64 strings.
    This helper converts them to 16-char hex for human readability and
    tree reconstruction.
    """
    if not v:
        return ""
    try:
        return base64.b64decode(v).hex()
    except Exception:
        return v  # Already hex, or malformed — pass through.


def _flatten_attrs(attrs: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for a in attrs:
        k = a.get("key", "")
        v = a.get("value", {})
        if "stringValue" in v:
            result[k] = v["stringValue"]
        elif "intValue" in v:
            result[k] = v["intValue"]
        elif "doubleValue" in v:
            result[k] = str(v["doubleValue"])
        elif "boolValue" in v:
            result[k] = str(v["boolValue"])
        else:
            result[k] = str(v)
    return result
