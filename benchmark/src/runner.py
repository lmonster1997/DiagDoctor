"""Batch Runner — executes evaluation cases against the Doctor agent.

Provides :class:`BatchRunner` for running individual or batched evaluation
cases against a Doctor API endpoint, with concurrency control, progress
display, and structured result persistence.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import structlog
from aiohttp import ClientTimeout, TCPConnector
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from benchmark.src.schema import BatchRunResult, RunMetadata, RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT = ClientTimeout(total=120, connect=10)
_DEFAULT_CONCURRENCY = 4
_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Doctor API response fields we care about
_DOCTOR_DIAGNOSE_PATH = "/api/diagnose"


class BatchRunner:
    """Run evaluation cases against a Doctor agent API.

    Typical usage::

        runner = BatchRunner("http://localhost:8001")
        cases = loader.load_suite()
        batch_result = await runner.run_batch(cases)

    Args:
        doctor_api_url: Base URL of the Doctor API (e.g. ``http://localhost:8001``).
        max_concurrency: Maximum number of concurrent requests to the Doctor API.
        evidence_base_dir: Root directory for resolving evidence file paths.
            Defaults to the project root.
    """

    def __init__(
        self,
        doctor_api_url: str,
        max_concurrency: int = _DEFAULT_CONCURRENCY,
        evidence_base_dir: str | Path | None = None,
    ) -> None:
        self.doctor_api_url = doctor_api_url.rstrip("/")
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        if evidence_base_dir is None:
            evidence_base_dir = _PROJECT_ROOT
        self.evidence_base_dir = Path(evidence_base_dir)

        logger.info(
            "BatchRunner initialised",
            doctor_api_url=self.doctor_api_url,
            max_concurrency=max_concurrency,
        )

    # ── Public API ──────────────────────────────────────────────────

    async def run_one(self, case: EvaluationCase) -> RunResult:
        """Run a single evaluation case against the Doctor agent.

        Workflow:
        1. Load evidence files referenced by the case.
        2. Build the diagnose request payload.
        3. POST to the Doctor API.
        4. Record latency, findings count, and other metadata.
        5. Return a structured :class:`RunResult`.

        Args:
            case: The evaluation case to run.

        Returns:
            A :class:`RunResult` with diagnosis details or error information.
        """
        start = asyncio.get_event_loop().time()
        case_id = case.case_id

        try:
            # 1. Load evidence
            evidence_payload = self._load_evidence(case)

            # 2. Build request
            payload = {
                "evidence": evidence_payload,
            }

            # 3. POST to Doctor API
            async with self._semaphore:
                async with aiohttp.ClientSession(
                    timeout=_DEFAULT_TIMEOUT,
                    connector=TCPConnector(limit=self.max_concurrency),
                ) as session:
                    url = f"{self.doctor_api_url}{_DOCTOR_DIAGNOSE_PATH}"
                    logger.debug("Calling Doctor API", url=url, case_id=case_id)

                    async with session.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    ) as resp:
                        body = await resp.json()

            latency_ms = (asyncio.get_event_loop().time() - start) * 1000

            if resp.status != 200:
                error_detail = body.get("detail", f"HTTP {resp.status}")
                logger.warning(
                    "Doctor API returned non-200",
                    case_id=case_id,
                    status=resp.status,
                    detail=error_detail,
                )
                return RunResult(
                    case_id=case_id,
                    success=False,
                    metadata=RunMetadata(latency_ms=latency_ms),
                    error=f"Doctor API error ({resp.status}): {error_detail}",
                )

            # 4. Parse response
            report = body.get("report")
            bug_category = body.get("bug_category")
            thread_id = body.get("thread_id", "")
            findings_count = body.get("findings_count", 0)

            logger.info(
                "Case completed",
                case_id=case_id,
                latency_ms=round(latency_ms, 1),
                bug_category=bug_category,
                findings_count=findings_count,
            )

            return RunResult(
                case_id=case_id,
                success=True,
                diagnosis=report,
                bug_category=bug_category,
                metadata=RunMetadata(
                    latency_ms=latency_ms,
                    doctor_thread_id=thread_id,
                    findings_count=findings_count,
                ),
            )

        except asyncio.TimeoutError:
            latency_ms = (asyncio.get_event_loop().time() - start) * 1000
            logger.error("Doctor API timeout", case_id=case_id)
            return RunResult(
                case_id=case_id,
                success=False,
                metadata=RunMetadata(latency_ms=latency_ms),
                error="Doctor API request timed out",
            )
        except aiohttp.ClientError as exc:
            latency_ms = (asyncio.get_event_loop().time() - start) * 1000
            logger.error("HTTP client error", case_id=case_id, error=str(exc))
            return RunResult(
                case_id=case_id,
                success=False,
                metadata=RunMetadata(latency_ms=latency_ms),
                error=f"HTTP client error: {exc}",
            )
        except Exception:
            latency_ms = (asyncio.get_event_loop().time() - start) * 1000
            logger.exception("Unexpected error running case", case_id=case_id)
            return RunResult(
                case_id=case_id,
                success=False,
                metadata=RunMetadata(latency_ms=latency_ms),
                error="Unexpected internal error",
            )

    async def run_batch(
        self,
        cases: list[EvaluationCase],
        progress_callback: Any = None,
    ) -> BatchRunResult:
        """Run multiple evaluation cases concurrently.

        Args:
            cases: The evaluation cases to run.
            progress_callback: Optional async callable invoked after each
                case completes.  Receives ``(case_id, RunResult)``.

        Returns:
            A :class:`BatchRunResult` with all individual results and a summary.

        Implementation details:
        * Uses :class:`asyncio.Semaphore` to cap concurrent Doctor API calls.
        * Failed cases do **not** block the remaining cases.
        * Progress is displayed via :mod:`rich` :class:`~rich.progress.Progress`.
        * Raw results are persisted to ``benchmark/runs/{timestamp}/results.json``.
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017
        total = len(cases)

        logger.info("Starting batch run", run_id=run_id, total_cases=total)

        # ── Rich progress bar ────────────────────────────────────
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TextColumn("({task.completed}/{task.total})"),
        ) as progress:
            task = progress.add_task(
                f"[cyan]Running {total} cases...",
                total=total,
            )

            # ── Run all cases concurrently ───────────────────────
            async def _run_with_tracking(case: EvaluationCase) -> RunResult:
                result = await self.run_one(case)
                progress.advance(task)
                status = "✓" if result.success else "✗"
                progress.console.print(
                    f"  {status} [bold]{case.case_id}[/bold] "
                    f"({result.metadata.latency_ms:.0f}ms)"
                    + (f" — [red]{result.error}[/red]" if result.error else "")
                )
                if progress_callback is not None:
                    try:
                        await progress_callback(case.case_id, result)
                    except Exception:
                        logger.exception("progress_callback failed", case_id=case.case_id)
                return result

            coros = [_run_with_tracking(c) for c in cases]
            results = await asyncio.gather(*coros, return_exceptions=False)

        # ── Build batch result ──────────────────────────────────
        batch = BatchRunResult.from_results(run_id, results, cases)

        # ── Persist to disk ──────────────────────────────────────
        self._save_batch_result(batch)

        logger.info(
            "Batch run complete",
            run_id=run_id,
            passed=batch.passed,
            failed=batch.failed,
            pass_rate=f"{batch.summary.pass_rate:.1%}",
        )

        return batch

    # ── Evidence loading ────────────────────────────────────────────

    def _load_evidence(self, case: EvaluationCase) -> dict[str, Any]:
        """Load and transform evidence files into the Doctor API format.

        Resolves evidence file paths from the case YAML, loads the JSON
        files, and converts entries to the format expected by the Doctor's
        ``DiagnoseRequest.evidence`` model.

        Returns:
            A dict matching the Doctor's ``Evidence`` Pydantic model schema.
        """
        logs: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []

        # Resolve evidence directory — try multiple possible locations
        evidence_dir = self._resolve_evidence_dir(case.case_id)

        if evidence_dir is not None:
            logs = self._load_json(evidence_dir / "logs.json")
            traces = self._load_json(evidence_dir / "traces.json")

        # Convert bug-factory LogEntry → Doctor LogEntry
        doctor_logs = [_convert_log_entry(entry) for entry in logs]

        # Convert bug-factory TraceSpan → Doctor TraceSpan
        doctor_traces = [_convert_trace_span(span) for span in traces]

        return {
            "user_report": case.input.user_report,
            "logs": doctor_logs,
            "traces": doctor_traces,
            "error_screenshot_url": None,
            "request": None,
            "response": None,
        }

    def _resolve_evidence_dir(self, case_id: str) -> Path | None:
        """Find the evidence directory for a given case_id.

        Searches multiple possible locations:
        1. ``output/{case_id}/evidence/`` (where EvidenceCollector saves)
        2. The path referenced in the case YAML (relative to project root)

        Returns:
            The resolved directory path, or ``None`` if not found.
        """
        candidates = [
            self.evidence_base_dir / "output" / case_id / "evidence",
            _PROJECT_ROOT / "output" / case_id / "evidence",
        ]

        for candidate in candidates:
            if candidate.is_dir():
                return candidate

        logger.warning("Evidence directory not found", case_id=case_id, searched=candidates)
        return None

    @staticmethod
    def _load_json(path: Path) -> list[dict[str, Any]]:
        """Load a JSON file as a list of dicts, with graceful error handling."""
        if not path.is_file():
            logger.debug("Evidence file not found, skipping", path=str(path))
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            logger.warning("Expected JSON array, got %s", type(data).__name__, path=str(path))
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load evidence file", path=str(path), error=str(exc))
            return []

    # ── Persistence ──────────────────────────────────────────────────

    def _save_batch_result(self, batch: BatchRunResult) -> Path:
        """Persist a batch run result to ``benchmark/runs/{run_id}/``."""
        run_dir = _RUNS_DIR / batch.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        results_path = run_dir / "results.json"
        results_path.write_text(
            batch.model_dump_json(indent=2),
            encoding="utf-8",
        )

        logger.info("Batch results saved", path=str(results_path))
        return run_dir


# ── Evidence format conversion helpers ────────────────────────────────


def _convert_log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a bug-factory ``LogEntry`` dict to Doctor ``LogEntry`` format.

    bug-factory format:
        {"timestamp": "ISO-str", "labels": {detected_level, service_name, ...}, "line": "..."}

    Doctor format:
        {"timestamp": "ISO-str", "level": "ERROR", "service": "...", "message": "...",
         "trace_id": null, "attributes": {}}
    """
    labels: dict[str, str] = entry.get("labels", {})
    return {
        "timestamp": entry.get("timestamp", ""),
        "level": labels.get("detected_level", "unknown"),
        "service": labels.get("service_name", "unknown"),
        "message": entry.get("line", ""),
        "trace_id": labels.get("trace_id"),
        "attributes": {
            k: v
            for k, v in labels.items()
            if k not in ("detected_level", "service_name", "trace_id")
        },
    }


def _convert_trace_span(span: dict[str, Any]) -> dict[str, Any]:
    """Convert a bug-factory ``TraceSpan`` dict to Doctor ``TraceSpan`` format.

    bug-factory format:
        {"trace_id": "...", "span_id": "...", "operation_name": "...",
         "service_name": "...", "start_time": "...", "duration_ms": 0.0,
         "status": "ok", "attributes": {}}

    Doctor format:
        {"span_id": "...", "parent_id": null, "name": "...", "service": "...",
         "start": "ISO-str", "duration_ms": 0.0, "attributes": {}, "status": "ok"}
    """
    attrs: dict[str, str] = span.get("attributes", {})
    return {
        "span_id": span.get("span_id", ""),
        "parent_id": attrs.get("parent_id"),
        "name": span.get("operation_name", span.get("name", "")),
        "service": span.get("service_name", ""),
        "start": span.get("start_time", ""),
        "duration_ms": span.get("duration_ms", 0.0),
        "attributes": attrs,
        "status": span.get("status", "unset"),
    }
