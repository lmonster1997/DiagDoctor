"""Quick diagnostic script to check BE-020 diagnosis results."""

import json
import asyncio
from pathlib import Path
import aiohttp


async def main():
    base = Path("d:/Work/LearnAI/DiagDoctor/bug-factory/output/BE-020/evidence")
    logs = json.loads((base / "logs.json").read_text(encoding="utf-8"))
    traces = json.loads((base / "traces.json").read_text(encoding="utf-8"))

    payload = {
        "evidence": {
            "user_report": "我刚才在任务里写了一条评论,点发送按钮后页面闪了一下就停在原地",
            "logs": logs,
            "traces": traces,
            "browser_errors": [],
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:8001/api/diagnose",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            result = await resp.json()
            report = result.get("report", {})
            print("=== REPORT ===")
            print(f"root_cause: {report.get('root_cause', 'N/A')[:300]}")
            print(f"affected_file: {report.get('affected_file')}")
            print(f"fix_suggestion: {report.get('fix_suggestion', 'N/A')[:300]}")
            print(f"evidence_chain: {report.get('evidence_chain')}")
            print(f"confidence: {report.get('confidence')}")
            print(f"categories: {report.get('categories')}")
            print(f"findings_count: {result.get('findings_count')}")


if __name__ == "__main__":
    asyncio.run(main())
