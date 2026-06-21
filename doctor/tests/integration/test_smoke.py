"""
端到端冒烟测试 — 验证 DiagDoctor 整个诊断流水线是否跑通。

测试流程:
1. 在 demo-app 创建用户、项目、3 个任务
2. 调用不存在的 API 触发错误日志
3. 等待日志写入 Loki
4. 调用 doctor 的 /api/diagnose 接口
5. 断言返回 DiagnosisReport 且 bug_category 不为空

注意:
- 这是一个流水线连通性测试，不要求 Doctor 给出正确诊断
- 要求 Docker Compose 已启动：make up
- demo-backend 默认地址: http://localhost:8000
- doctor-api 默认地址: http://localhost:8001
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest


# ── Configuration ───────────────────────────────────────────────────

DEMO_BACKEND_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"

SERVICE_CHECK_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0
LOG_PROPAGATION_WAIT = 5  # seconds for logs to propagate to Loki


# ── Helpers ─────────────────────────────────────────────────────────


async def _health_check(url: str, timeout: float = SERVICE_CHECK_TIMEOUT) -> bool:
    """Check if a service is reachable at its /health endpoint."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/health")
            return resp.status_code == 200
    except Exception:
        return False


# ── The Smoke Test ──────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.smoke
async def test_end_to_end_smoke() -> None:
    """
    End-to-end smoke test for the DiagDoctor pipeline.

    This test validates that the entire pipeline (demo-app → logs → Doctor)
    is functional end-to-end. It does NOT validate diagnosis quality.
    """
    # ── Pre-flight: verify services are available ──
    demo_ok = await _health_check(DEMO_BACKEND_URL)
    doctor_ok = await _health_check(DOCTOR_URL)

    if not demo_ok:
        pytest.skip(
            f"demo-backend not available at {DEMO_BACKEND_URL}. "
            "Run 'make up' first to start all services."
        )
    if not doctor_ok:
        pytest.skip(
            f"doctor-api not available at {DOCTOR_URL}. Run 'make up' first to start all services."
        )

    # Generate unique test identifiers to avoid collisions across runs
    test_id = uuid.uuid4().hex[:8]
    test_email = f"smoke-{test_id}@example.com"
    test_password = "SmokeTest123!"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        # ─────────────────────────────────────────────────────────
        # Step 1: Register a user
        # ─────────────────────────────────────────────────────────
        register_resp = await client.post(
            f"{DEMO_BACKEND_URL}/api/auth/register",
            json={
                "email": test_email,
                "password": test_password,
                "display_name": "Smoke Tester",
            },
        )
        # 201 = created; 409 = already exists from a previous run
        assert register_resp.status_code in (201, 409), (
            f"User registration failed: HTTP {register_resp.status_code}\n"
            f"Response: {register_resp.text}"
        )

        # ─────────────────────────────────────────────────────────
        # Step 2: Login to get JWT token
        # ─────────────────────────────────────────────────────────
        login_resp = await client.post(
            f"{DEMO_BACKEND_URL}/api/auth/login",
            json={"email": test_email, "password": test_password},
        )
        assert login_resp.status_code == 200, (
            f"User login failed: HTTP {login_resp.status_code}\nResponse: {login_resp.text}"
        )
        token: str = login_resp.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        # ─────────────────────────────────────────────────────────
        # Step 3: Create a project
        # ─────────────────────────────────────────────────────────
        project_resp = await client.post(
            f"{DEMO_BACKEND_URL}/api/projects/",
            json={
                "name": f"Smoke Test Project ({test_id})",
                "description": "Project created by E2E smoke test",
            },
            headers=auth_headers,
        )
        assert project_resp.status_code == 201, (
            f"Create project failed: HTTP {project_resp.status_code}\nResponse: {project_resp.text}"
        )
        project_id: str = project_resp.json()["id"]

        # ─────────────────────────────────────────────────────────
        # Step 4: Create 3 tasks in the project
        # ─────────────────────────────────────────────────────────
        task_ids: list[str] = []
        for i in range(3):
            task_resp = await client.post(
                f"{DEMO_BACKEND_URL}/api/projects/{project_id}/tasks",
                json={
                    "title": f"Smoke Task {i + 1}",
                    "description": f"Task {i + 1} for E2E smoke test",
                    "status": "todo",
                    "priority": i,
                },
                headers=auth_headers,
            )
            assert task_resp.status_code == 201, (
                f"Create task {i + 1} failed: HTTP {task_resp.status_code}\n"
                f"Response: {task_resp.text}"
            )
            task_ids.append(task_resp.json()["id"])

        # ─────────────────────────────────────────────────────────
        # Step 5: Simulate an error — call a nonexistent API
        # ─────────────────────────────────────────────────────────
        error_resp = await client.get(
            f"{DEMO_BACKEND_URL}/api/nonexistent",
            headers=auth_headers,
        )
        # We expect a 404 (or possibly 405 if route exists but method differs)
        assert error_resp.status_code in (404, 405), (
            f"Expected 404/405 from nonexistent endpoint, "
            f"got HTTP {error_resp.status_code}\n"
            f"Response: {error_resp.text}"
        )

        # ─────────────────────────────────────────────────────────
        # Step 6: Wait for logs to propagate to Loki
        # ─────────────────────────────────────────────────────────
        await asyncio.sleep(LOG_PROPAGATION_WAIT)

        # ─────────────────────────────────────────────────────────
        # Step 7: Call doctor's /api/diagnose endpoint
        # ─────────────────────────────────────────────────────────
        diagnose_resp = await client.post(
            f"{DOCTOR_URL}/api/diagnose",
            json={
                "evidence": {
                    "user_report": "调用 /api/nonexistent 返回 404",
                    "logs": [],
                    "traces": [],
                }
            },
        )
        assert diagnose_resp.status_code == 200, (
            f"Doctor diagnose failed: HTTP {diagnose_resp.status_code}\n"
            f"Response: {diagnose_resp.text}"
        )

        result: dict = diagnose_resp.json()

        # ─────────────────────────────────────────────────────────
        # Step 8: Assertions — pipeline must have produced a report
        # ─────────────────────────────────────────────────────────

        # 8a. A thread_id must be assigned
        assert "thread_id" in result, f"Response missing thread_id: {result}"

        # 8b. bug_category must be present and non-empty
        bug_category: str = result.get("bug_category", "")
        assert bug_category, (
            f"bug_category is empty — Doctor did not classify the bug.\nFull response: {result}"
        )

        # 8c. A DiagnosisReport must be present
        report: dict | None = result.get("report")
        assert report is not None, f"No 'report' in Doctor response.\nFull response: {result}"
        assert isinstance(report, dict), f"'report' is not a dict: {type(report)}"

        # 8d. Report must contain the required DiagnosisReport fields
        required_report_fields = [
            "bug_category",
            "root_cause",
            "fix_suggestion",
            "confidence",
        ]
        for field in required_report_fields:
            assert field in report, (
                f"DiagnosisReport missing required field '{field}'.\nReport: {report}"
            )

        # ── Success summary ───────────────────────────────────
        print(f"\n{'=' * 60}")
        print(f"✅ E2E Smoke Test PASSED!")
        print(f"{'=' * 60}")
        print(f"   User email:     {test_email}")
        print(f"   Project ID:     {project_id}")
        print(f"   Task IDs:       {task_ids}")
        print(f"   Doctor thread:  {result['thread_id']}")
        print(f"   Bug category:   {bug_category}")
        print(f"   Confidence:     {report.get('confidence', 'N/A')}")
        print(f"   Root cause:     {report.get('root_cause', 'N/A')[:100]}")
        print(f"{'=' * 60}\n")
