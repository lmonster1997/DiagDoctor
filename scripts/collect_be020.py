"""Quick evidence collection for BE-020 with wide time window."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bug_factory.evidence_collector import EvidenceCollector


async def collect():
    collector = EvidenceCollector(
        loki_url="http://127.0.0.1:3100",
        tempo_url="http://127.0.0.1:3200",
        output_dir=Path(r"d:\Work\LearnAI\DiagDoctor\bug-factory\output"),
    )
    now = datetime.now(timezone.utc)
    evidence = await collector.collect(
        recipe_id="BE-020",
        start=now - timedelta(minutes=5),
        end=now,
    )
    print(f"Collected: {len(evidence.logs)} logs, {len(evidence.traces)} traces")

    # Write files directly to be safe
    out_dir = Path(r"d:\Work\LearnAI\DiagDoctor\bug-factory\output") / "BE-020" / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)

    logs_json = json.dumps([e.model_dump() for e in evidence.logs], indent=2, ensure_ascii=False)
    traces_json = json.dumps(
        [t.model_dump() for t in evidence.traces], indent=2, ensure_ascii=False
    )
    browser_json = json.dumps(
        [b.model_dump() for b in evidence.browser_errors], indent=2, ensure_ascii=False
    )

    (out_dir / "logs.json").write_text(logs_json, encoding="utf-8")
    (out_dir / "traces.json").write_text(traces_json, encoding="utf-8")
    (out_dir / "browser_errors.json").write_text(browser_json, encoding="utf-8")

    print(f"Saved to {out_dir.resolve()}")
    print(f"  logs.json: {len(logs_json)} bytes")
    print(f"  traces.json: {len(traces_json)} bytes")
    print(f"  browser_errors.json: {len(browser_json)} bytes")

    # Show key evidence
    if evidence.logs:
        for log in evidence.logs[:3]:
            level = log.labels.get("detected_level", "?")
            print(f"  [{level}] {log.timestamp} | {log.line[:120]}")
    if evidence.traces:
        for t in evidence.traces[:3]:
            print(f"  [{t.service_name}] {t.operation_name} | {t.start_time}")


asyncio.run(collect())
