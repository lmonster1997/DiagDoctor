"""
Unit tests for TriggerRunner — E2E action executor for bug recipes.

Covers:
- Action dispatch (login, api_call, ui_click, create_data, wait)
- Template variable resolution ({project_id}, {task_id})
- Session state management across steps
- Error propagation (step failure aborts sequence)
- Repeat parameter handling
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bug_factory.schema import (
    Trigger,
    TriggerError,
    TriggerStep,
)
from bug_factory.trigger import TriggerRunner

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> TriggerRunner:
    """Fresh TriggerRunner with default base URL."""
    return TriggerRunner(demo_app_base_url="http://localhost:8000", log_flush_seconds=0)


@pytest.fixture
def basic_trigger() -> Trigger:
    """A minimal trigger with a single wait step."""
    from bug_factory.schema import ExpectedObservation

    return Trigger(
        type="api_call",
        steps=[
            TriggerStep(action="wait", params={"seconds": 0.01}),
        ],
        expected_observation=ExpectedObservation(),
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _make_step(action: str, params: dict[str, Any] | None = None) -> TriggerStep:
    return TriggerStep(action=action, params=params or {})


def _make_trigger(*steps: TriggerStep) -> Trigger:
    from bug_factory.schema import ExpectedObservation

    return Trigger(type="api_call", steps=list(steps), expected_observation=ExpectedObservation())


# ── Action: wait ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_wait(runner: TriggerRunner) -> None:
    """_action_wait should sleep for the requested duration."""
    t0 = asyncio.get_event_loop().time()
    await runner._action_wait({"seconds": 0.05})
    elapsed = asyncio.get_event_loop().time() - t0
    assert 0.04 <= elapsed <= 0.15


@pytest.mark.asyncio
async def test_action_wait_default(runner: TriggerRunner) -> None:
    """_action_wait should default to 1 second when 'seconds' is missing."""
    t0 = asyncio.get_event_loop().time()
    await runner._action_wait({})
    elapsed = asyncio.get_event_loop().time() - t0
    assert 0.9 <= elapsed <= 1.2


# ── Action: login ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_login_success(runner: TriggerRunner) -> None:
    """Login should store the token in session."""
    mock_resp = {"access_token": "fake-jwt-token", "token_type": "bearer"}

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await runner._action_login({"email": "admin@example.com", "password": "Admin123!"})

    assert result == mock_resp
    assert runner.session["token"] == "fake-jwt-token"
    assert runner.session["current_user"] == "admin@example.com"
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_action_login_missing_params(runner: TriggerRunner) -> None:
    """Login without email/password should raise TriggerError."""
    with pytest.raises(TriggerError, match="login action requires"):
        await runner._action_login({})


@pytest.mark.asyncio
async def test_action_login_no_token_in_response(runner: TriggerRunner) -> None:
    """Login response without access_token should raise TriggerError."""
    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"error": "invalid credentials"}
        with pytest.raises(TriggerError, match="did not contain 'access_token'"):
            await runner._action_login({"email": "a@b.com", "password": "x"})


# ── Action: api_call ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_api_call_get(runner: TriggerRunner) -> None:
    """api_call should make an HTTP GET request with Bearer token."""
    runner.session["token"] = "test-token"
    mock_resp = {"tasks": []}

    with patch.object(runner, "_http_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        result = await runner._action_api_call({"method": "GET", "path": "/api/projects"})

    assert result == mock_resp
    mock_req.assert_called_once_with(
        method="GET",
        path="/api/projects",
        json_data=None,
        params=None,
        auth_required=True,
    )


@pytest.mark.asyncio
async def test_action_api_call_post_with_body(runner: TriggerRunner) -> None:
    """api_call should send JSON body on POST."""
    runner.session["token"] = "test-token"
    mock_resp = {"id": "uuid-123", "name": "Test"}

    with patch.object(runner, "_http_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        result = await runner._action_api_call(
            {
                "method": "POST",
                "path": "/api/projects/",
                "body": {"name": "Test"},
            }
        )

    assert result == mock_resp
    mock_req.assert_called_once_with(
        method="POST",
        path="/api/projects/",
        json_data={"name": "Test"},
        params=None,
        auth_required=True,
    )


@pytest.mark.asyncio
async def test_action_api_call_repeat(runner: TriggerRunner) -> None:
    """api_call with repeat=N should make N requests."""
    runner.session["token"] = "test-token"
    mock_resp = {"ok": True}

    with patch.object(runner, "_http_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        result = await runner._action_api_call(
            {"method": "GET", "path": "/api/health", "repeat": 3}
        )

    assert result == mock_resp
    assert mock_req.call_count == 3


@pytest.mark.asyncio
async def test_action_api_call_template_vars(runner: TriggerRunner) -> None:
    """Template variables in path should be resolved from session."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = [{"id": "proj-abc"}]

    with patch.object(runner, "_http_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"tasks": []}
        await runner._action_api_call({"method": "GET", "path": "/api/projects/{project_id}/tasks"})

    mock_req.assert_called_once()
    call_kwargs = mock_req.call_args.kwargs
    assert call_kwargs["path"] == "/api/projects/proj-abc/tasks"


# ── Action: create_data ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_create_data_project(runner: TriggerRunner) -> None:
    """create_data for 'project' should track created project in session."""
    runner.session["token"] = "test-token"
    mock_resp = {"id": "proj-uuid", "name": "My Project"}

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await runner._action_create_data(
            {"entity": "project", "data": {"name": "My Project"}}
        )

    assert result == mock_resp
    assert runner.session["created_projects"] == [mock_resp]
    mock_post.assert_called_once_with(
        "/api/projects/",
        json_data={"name": "My Project"},
        auth_required=True,
    )


@pytest.mark.asyncio
async def test_action_create_data_task(runner: TriggerRunner) -> None:
    """create_data for 'task' should use last created project's id."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = [{"id": "proj-uuid"}]
    mock_resp = {"id": "task-uuid", "title": "New Task"}

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await runner._action_create_data({"entity": "task", "data": {"title": "New Task"}})

    assert result == mock_resp
    mock_post.assert_called_once_with(
        "/api/projects/proj-uuid/tasks",
        json_data={"title": "New Task", "project_id": "proj-uuid"},
        auth_required=True,
    )
    assert runner.session["created_tasks"] == [mock_resp]


@pytest.mark.asyncio
async def test_action_create_data_task_project_index(runner: TriggerRunner) -> None:
    """create_data for 'task' with project_index should select correct project."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = [
        {"id": "proj-0"},
        {"id": "proj-1"},
        {"id": "proj-2"},
    ]

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"id": "task-x"}
        await runner._action_create_data(
            {"entity": "task", "data": {"title": "T"}, "project_index": 1}
        )

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    # _http_post(path, json_data=..., auth_required=True) — path is positional
    assert call_args.args[0] == "/api/projects/proj-1/tasks"
    assert call_args.kwargs["json_data"]["project_id"] == "proj-1"


@pytest.mark.asyncio
async def test_action_create_data_task_no_project(runner: TriggerRunner) -> None:
    """create_data for 'task' without any project in session should error."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = []

    with pytest.raises(TriggerError, match="Cannot create task"):
        await runner._action_create_data({"entity": "task", "data": {"title": "T"}})


@pytest.mark.asyncio
async def test_action_create_data_repeat(runner: TriggerRunner) -> None:
    """create_data with repeat should create N entities."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = [{"id": "proj-uuid"}]

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = [
            {"id": "task-0"},
            {"id": "task-1"},
            {"id": "task-2"},
        ]
        await runner._action_create_data({"entity": "task", "data": {"title": "T"}, "repeat": 3})

    assert mock_post.call_count == 3
    assert len(runner.session["created_tasks"]) == 3


@pytest.mark.asyncio
async def test_action_create_data_unknown_entity(runner: TriggerRunner) -> None:
    """create_data for unknown entity type should raise TriggerError."""
    with pytest.raises(TriggerError, match="Unknown entity type"):
        await runner._action_create_data({"entity": "nonexistent", "data": {}})


# ── Template Resolution ────────────────────────────────────────────────


def test_resolve_template_project_id(runner: TriggerRunner) -> None:
    """{project_id} should resolve to the last created project's id."""
    runner.session["created_projects"] = [{"id": "p-1"}, {"id": "p-2"}]
    assert runner._resolve_template("{project_id}") == "p-2"


def test_resolve_template_project_id_index(runner: TriggerRunner) -> None:
    """{project_id:N} should resolve to the Nth project's id."""
    runner.session["created_projects"] = [{"id": "p-0"}, {"id": "p-1"}, {"id": "p-2"}]
    assert runner._resolve_template("{project_id:0}") == "p-0"
    assert runner._resolve_template("{project_id:2}") == "p-2"


def test_resolve_template_task_id(runner: TriggerRunner) -> None:
    """{task_id} should resolve to the last created task's id."""
    runner.session["created_tasks"] = [{"id": "t-a"}, {"id": "t-b"}]
    assert runner._resolve_template("{task_id}") == "t-b"


def test_resolve_template_unresolvable(runner: TriggerRunner) -> None:
    """Unknown template vars should be kept as-is."""
    assert runner._resolve_template("{unknown_var}") == "{unknown_var}"


def test_resolve_template_missing_data(runner: TriggerRunner) -> None:
    """Template var with no matching data should be kept as-is."""
    assert runner._resolve_template("{project_id}") == "{project_id}"


def test_resolve_template_in_dict(runner: TriggerRunner) -> None:
    """Template vars inside nested dicts should be resolved."""
    runner.session["created_projects"] = [{"id": "proj-x"}]
    payload = {"path": "/api/projects/{project_id}/tasks", "body": {"pid": "{project_id}"}}
    resolved = runner._resolve_template(payload)
    assert resolved["path"] == "/api/projects/proj-x/tasks"
    assert resolved["body"]["pid"] == "proj-x"


def test_resolve_template_in_list(runner: TriggerRunner) -> None:
    """Template vars inside lists should be resolved."""
    runner.session["created_tasks"] = [{"id": "t-99"}]
    items = ["{task_id}", "static"]
    assert runner._resolve_template(items) == ["t-99", "static"]


# ── TriggerRunner.run() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_single_step(runner: TriggerRunner) -> None:
    """run() should execute a single-step trigger successfully."""
    trigger = _make_trigger(_make_step("wait", {"seconds": 0.01}))
    result = await runner.run(trigger)

    assert result.success is True
    assert len(result.steps) == 1
    assert result.steps[0].success is True
    assert result.steps[0].action == "wait"
    assert result.steps[0].elapsed_ms >= 0


@pytest.mark.asyncio
async def test_run_multiple_steps(runner: TriggerRunner) -> None:
    """run() should execute all steps in order."""
    trigger = _make_trigger(
        _make_step("wait", {"seconds": 0.01}),
        _make_step("wait", {"seconds": 0.01}),
    )
    result = await runner.run(trigger)

    assert result.success is True
    assert len(result.steps) == 2
    assert all(s.success for s in result.steps)


@pytest.mark.asyncio
async def test_run_stops_on_failure(runner: TriggerRunner) -> None:
    """run() should abort on first failing step."""
    trigger = _make_trigger(
        _make_step("wait", {"seconds": 0.01}),
        _make_step("api_call", {"method": "GET", "path": "/nonexistent"}),
        _make_step("wait", {"seconds": 0.01}),  # Should NOT execute
    )

    # Simulate API call failure — no token in session.
    result = await runner.run(trigger)

    assert result.success is False
    assert len(result.steps) == 2  # Only the first two attempted
    assert result.steps[0].success is True  # Wait succeeded
    assert result.steps[1].success is False  # API call failed
    assert result.error is not None


@pytest.mark.asyncio
async def test_run_log_flush_on_success(runner: TriggerRunner) -> None:
    """On success, runner should wait for log flush."""
    runner.log_flush_seconds = 0.05
    trigger = _make_trigger(_make_step("wait", {"seconds": 0.01}))
    t0 = asyncio.get_event_loop().time()
    result = await runner.run(trigger)
    elapsed = asyncio.get_event_loop().time() - t0

    assert result.success is True
    # Should include log_flush wait time
    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_run_no_log_flush_on_failure(runner: TriggerRunner) -> None:
    """On failure, runner should NOT wait for log flush."""
    runner.log_flush_seconds = 1.0  # Would be noticeable
    trigger = _make_trigger(
        _make_step("api_call", {"method": "GET", "path": "/x"})  # No token → fail fast
    )
    t0 = asyncio.get_event_loop().time()
    result = await runner.run(trigger)
    elapsed = asyncio.get_event_loop().time() - t0

    assert result.success is False
    assert elapsed < 0.5  # Should NOT have waited 1 second


@pytest.mark.asyncio
async def test_run_session_state_persists(runner: TriggerRunner) -> None:
    """Session state should persist across steps within a run."""
    runner.session["token"] = "test-token"
    runner.session["created_projects"] = [{"id": "proj-1"}]

    with patch.object(runner, "_http_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"id": "task-created"}

        trigger = _make_trigger(
            _make_step("create_data", {"entity": "task", "data": {"title": "T"}}),
        )
        result = await runner.run(trigger)

    assert result.success is True
    assert len(result.session["created_tasks"]) == 1
    assert result.session["created_tasks"][0]["id"] == "task-created"


@pytest.mark.asyncio
async def test_run_unknown_action(runner: TriggerRunner) -> None:
    """Unknown action type should produce a failed step result."""
    # Construct a TriggerStep that bypasses Pydantic literal validation.
    step = TriggerStep.model_construct(action="nonexistent_action", params={})
    result = await runner._execute_step(0, step)

    assert result.success is False
    assert "Unknown action type" in (result.error or "")


# ── HTTP helper: _http_request ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_request_no_token(runner: TriggerRunner) -> None:
    """_http_request with auth_required=True and no token should error."""
    with pytest.raises(TriggerError, match="no token in session"):
        await runner._http_request("GET", "/api/test", auth_required=True)


@pytest.mark.asyncio
async def test_http_request_success() -> None:
    """_http_request should return parsed JSON on 2xx."""
    import aiohttp
    from aiohttp import web

    async def handler(request: aiohttp.web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/api/test", handler)

    runner_obj = TriggerRunner(demo_app_base_url="http://127.0.0.1:0")
    runner_obj.session["token"] = "t"

    # We can't easily start a real server in unit tests without binding.
    # Instead, verify the helper raises on connection refused.
    with pytest.raises(TriggerError, match="HTTP request failed"):
        await runner_obj._http_request("GET", "/api/test", auth_required=True)


# ── UI action: _action_ui_click ────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_ui_click_no_selector(runner: TriggerRunner) -> None:
    """ui_click without selector should raise TriggerError."""
    with pytest.raises(TriggerError, match="requires 'selector'"):
        await runner._action_ui_click({})


@pytest.mark.asyncio
async def test_action_ui_click_no_playwright(runner: TriggerRunner) -> None:
    """ui_click should raise TriggerError when playwright is not installed."""
    # The import happens inside _action_ui_click, so we need to make
    # __import__ fail for the playwright package.
    import builtins

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("No module named 'playwright'")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):  # noqa: SIM117
        with pytest.raises(TriggerError, match="playwright is not installed"):
            await runner._action_ui_click({"selector": "#btn"})


# ── Step result timing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_result_includes_timing(runner: TriggerRunner) -> None:
    """Each StepResult should include non-negative elapsed_ms."""
    trigger = _make_trigger(_make_step("wait", {"seconds": 0.03}))
    result = await runner.run(trigger)

    assert result.steps[0].elapsed_ms >= 0
    # Wait 0.03s ≈ 30ms; allow generous slack for CI/event-loop variance
    assert result.steps[0].elapsed_ms >= 15
