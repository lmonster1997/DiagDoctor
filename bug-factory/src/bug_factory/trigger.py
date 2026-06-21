"""
Trigger Runner — executes trigger step sequences against the demo-app.

The :class:`TriggerRunner` takes a :class:`Trigger` definition (from a bug
recipe) and executes each step against a live demo-app instance.  It supports
five action types:

- ``login`` — authenticate and cache a JWT token
- ``api_call`` — arbitrary HTTP request with automatic Bearer injection
- ``ui_click`` — browser interaction via Playwright async API
- ``create_data`` — create domain entities (project / task) via the REST API
- ``wait`` — pause execution (e.g. for logs to flush)

Template variables (``{project_id}``, ``{task_id}``) in API paths and bodies
are automatically resolved from session state.

Usage::

    from bug_factory.schema import Trigger, TriggerStep, load_recipe
    from bug_factory.trigger import TriggerRunner

    recipe = load_recipe("recipes/be_001_n_plus_1.yaml")
    runner = TriggerRunner(base_url="http://localhost:8000")
    result = await runner.run(recipe.trigger)
    print(result.success, result.session)
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog
from aiohttp import ClientSession, ClientTimeout

from bug_factory.schema import (
    StepResult,
    Trigger,
    TriggerError,
    TriggerResult,
)

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_TEMPLATE_VAR_RE = re.compile(r"\{(\w+)(?::(\d+))?\}")

_DEFAULT_TIMEOUT = ClientTimeout(total=30)

# Map entity names to their REST API paths for create_data action.
_CREATE_ROUTES: dict[str, str] = {
    "project": "/api/projects/",
    "task": "/api/projects/{project_id}/tasks",
}

# Default auth header key inserted by _action_login.
_AUTH_HEADER = "Authorization"

# Minimum and default wait for logs to flush after trigger completion.
_LOG_FLUSH_SECONDS = 3


class TriggerRunner:
    """Execute a :class:`Trigger` step sequence against a live demo-app.

    Maintains a session dict across steps that stores the JWT token and
    IDs of created entities so later steps can reference them via template
    variables (``{project_id}``, ``{task_id}``, etc.).

    Args:
        demo_app_base_url: Root URL of the demo-app backend (no trailing slash).
            Defaults to ``http://localhost:8000``.
        log_flush_seconds: How long to wait after the last step for logs to
            be ingested by Loki / Tempo.  Default 3 s.
    """

    def __init__(
        self,
        demo_app_base_url: str = "http://localhost:8000",
        log_flush_seconds: float = _LOG_FLUSH_SECONDS,
    ) -> None:
        self.base_url = demo_app_base_url.rstrip("/")
        self.log_flush_seconds = log_flush_seconds
        self.session: dict[str, Any] = {
            "token": None,
            "created_projects": [],
            "created_tasks": [],
        }
        logger.info(
            "TriggerRunner initialised",
            base_url=self.base_url,
            log_flush_seconds=self.log_flush_seconds,
        )

    # ── Public API ──────────────────────────────────────────────────

    async def run(self, trigger: Trigger) -> TriggerResult:
        """Execute every step in *trigger* sequentially.

        If any step fails the remaining steps are **skipped** and a
        :class:`TriggerResult` with ``success=False`` is returned.

        Args:
            trigger: The trigger definition from a :class:`BugRecipe`.

        Returns:
            A :class:`TriggerResult` summarising every step and the final
            session state.
        """
        logger.info(
            "Trigger run starting",
            trigger_type=trigger.type,
            step_count=len(trigger.steps),
        )
        steps: list[StepResult] = []
        overall_success = True
        error_msg: str | None = None

        try:
            for i, step in enumerate(trigger.steps):
                step_result = await self._execute_step(i, step)
                steps.append(step_result)

                if not step_result.success:
                    overall_success = False
                    error_msg = f"Step {i} ({step.action}) failed: {step_result.error}"
                    logger.error(
                        "Trigger step failed — aborting sequence",
                        step_index=i,
                        action=step.action,
                        error=step_result.error,
                    )
                    break
        except Exception as exc:
            overall_success = False
            error_msg = f"Unexpected error: {exc}"
            logger.exception("Trigger execution raised unexpected exception")

        # Wait for observability data to land in Loki / Tempo.
        if overall_success:
            logger.info(
                "All steps succeeded — waiting for log flush",
                seconds=self.log_flush_seconds,
            )
            await asyncio.sleep(self.log_flush_seconds)
        else:
            logger.warning("Trigger had failures — skipping log-flush wait")

        result = TriggerResult(
            success=overall_success,
            session=self.session,
            steps=steps,
            error=error_msg,
        )
        logger.info(
            "Trigger run complete",
            success=overall_success,
            step_count=len(steps),
        )
        return result

    # ── Action dispatcher ───────────────────────────────────────────

    _ACTION_HANDLERS: dict[str, str] = {
        "login": "_action_login",
        "api_call": "_action_api_call",
        "ui_click": "_action_ui_click",
        "create_data": "_action_create_data",
        "wait": "_action_wait",
    }

    async def _execute_step(self, index: int, step: object) -> StepResult:
        """Dispatch a single trigger step to its action handler.

        Args:
            index: Zero-based step index (for error reporting).
            step: A :class:`TriggerStep` instance.

        Returns:
            A :class:`StepResult` with timing, response, and error info.
        """
        # Import here to allow type-checking without circular imports.
        from bug_factory.schema import TriggerStep

        if not isinstance(step, TriggerStep):
            return StepResult(
                action="unknown",
                params={},
                success=False,
                elapsed_ms=0,
                error=f"Expected TriggerStep, got {type(step).__name__}",
            )

        handler_name = self._ACTION_HANDLERS.get(step.action)
        if handler_name is None:
            return StepResult(
                action=step.action,
                params=step.params,
                success=False,
                elapsed_ms=0,
                error=f"Unknown action type: {step.action}",
            )

        handler = getattr(self, handler_name)
        t0 = time.perf_counter()

        try:
            response = await handler(step.params)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug(
                "Step succeeded",
                index=index,
                action=step.action,
                elapsed_ms=round(elapsed_ms, 1),
            )
            return StepResult(
                action=step.action,
                params=step.params,
                success=True,
                elapsed_ms=round(elapsed_ms, 1),
                response=response,
            )
        except TriggerError as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning(
                "Step failed (TriggerError)",
                index=index,
                action=step.action,
                error=str(exc),
            )
            return StepResult(
                action=step.action,
                params=step.params,
                success=False,
                elapsed_ms=round(elapsed_ms, 1),
                error=str(exc),
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning(
                "Step failed",
                index=index,
                action=step.action,
                error=str(exc),
            )
            return StepResult(
                action=step.action,
                params=step.params,
                success=False,
                elapsed_ms=round(elapsed_ms, 1),
                error=str(exc),
            )

    # ── Action implementations ──────────────────────────────────────

    async def _action_login(self, params: dict[str, Any]) -> dict[str, Any]:
        """Authenticate against ``POST /api/auth/login`` and cache the token.

        Required *params*:
            email (str): User email.
            password (str): User password.

        Returns:
            The parsed JSON response (contains ``access_token``).
        """
        email = params.get("email")
        password = params.get("password")
        if not email or not password:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail="login action requires 'email' and 'password' params",
            )

        payload = {"email": email, "password": password}
        data = await self._http_post("/api/auth/login", json_data=payload, auth_required=False)

        token = data.get("access_token")
        if not token:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail="Login response did not contain 'access_token'",
            )

        self.session["token"] = token
        self.session["current_user"] = email
        logger.info("Login succeeded", email=email)
        return data

    async def _action_api_call(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Make an arbitrary HTTP request to the demo-app.

        Required *params*:
            method (str): HTTP method (GET, POST, PATCH, DELETE).
            path (str): URL path (e.g. ``/api/projects/{project_id}/tasks``).

        Optional *params*:
            body (dict): JSON request body.
            query (dict): Query string parameters.
            repeat (int): Repeat the call *repeat* times (for load generation).

        Template variables like ``{project_id}`` or ``{task_id}`` in *path*
        and *body* are resolved from session state before each request.
        """
        method = (params.get("method", "GET") or "GET").upper()
        path_template = params.get("path", "/")
        body_template = params.get("body")
        query = params.get("query")
        repeat = max(1, int(params.get("repeat", 1)))

        result: dict[str, Any] | None = None

        for _ in range(repeat):
            path = self._resolve_template(path_template)
            body = self._resolve_template(body_template) if body_template else None

            result = await self._http_request(
                method=method,
                path=path,
                json_data=body,
                params=query,
                auth_required=True,
            )

        return result

    async def _action_ui_click(self, params: dict[str, Any]) -> dict[str, Any]:
        """Click a UI element using Playwright async API.

        Required *params*:
            selector (str): CSS / data-testid selector for the target element.

        The action navigates to the frontend (default ``http://localhost:3000``)
        if a browser context has not been established yet.

        Note:
            Playwright must be installed: ``playwright install chromium``.
        """
        selector = params.get("selector")
        if not selector:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail="ui_click action requires 'selector' param",
            )

        frontend_url = params.get("frontend_url", "http://localhost:3000")

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail=(
                    "playwright is not installed. "
                    "Run: pip install playwright && playwright install chromium"
                ),
            ) from exc

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            try:
                await page.goto(frontend_url, wait_until="networkidle")

                # Log in via API first so the browser session has a token
                # stored in localStorage — replay the login via page.evaluate.
                token = self.session.get("token")
                if token:
                    await page.evaluate(
                        """(t) => {
                            localStorage.setItem('auth-storage', JSON.stringify({
                                state: { token: t }
                            }));
                        }""",
                        token,
                    )
                    await page.reload(wait_until="networkidle")

                # Click the target element.
                await page.click(selector)
                await asyncio.sleep(1)  # Let any navigation / animation settle.

            finally:
                await browser.close()

        logger.info("UI click executed", selector=selector)
        return {"clicked": selector}

    async def _action_create_data(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a domain entity via the REST API.

        Required *params*:
            entity (str): Entity type — ``"project"`` or ``"task"``.
            data (dict): Fields for the new entity.

        Optional *params*:
            repeat (int): Create *repeat* copies (for load generation).
            project_index (int): Index into ``session["created_projects"]``
                (used when ``entity="task"`` to set the project).

        Created entities are tracked in ``session["created_projects"]``
        and ``session["created_tasks"]`` for later template resolution.
        """
        entity = params.get("entity", "")
        data_template: dict[str, Any] = params.get("data", {})
        repeat = max(1, int(params.get("repeat", 1)))

        route = _CREATE_ROUTES.get(entity)
        if route is None:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail=f"Unknown entity type: {entity}. Supported: {list(_CREATE_ROUTES)}",
            )

        result: dict[str, Any] | None = None

        for _ in range(repeat):
            resolved_data = self._resolve_template(data_template) if data_template else {}

            # Resolve project for task creation.
            if entity == "task" and "project_id" not in resolved_data:
                proj_index = int(params.get("project_index", -1))
                projects = self.session.get("created_projects", [])
                if proj_index >= 0 and proj_index < len(projects):
                    resolved_data["project_id"] = projects[proj_index]["id"]
                elif projects:
                    resolved_data["project_id"] = projects[-1]["id"]
                else:
                    raise TriggerError(
                        recipe_id="<unknown>",
                        step_index=-1,
                        detail="Cannot create task: no project in session. "
                        "Create a project first, or provide project_id in data.",
                    )

            # Resolve the route path: use the resolved project_id if available,
            # otherwise fall back to generic template resolution.
            project_id = resolved_data.get("project_id")
            if project_id and "{project_id}" in route:
                resolved_path = route.replace("{project_id}", str(project_id))
            else:
                resolved_path = self._resolve_template(route)
            result = await self._http_post(
                resolved_path, json_data=resolved_data, auth_required=True
            )

            # Track the created entity.
            if entity == "project":
                self.session.setdefault("created_projects", []).append(result)
            elif entity == "task":
                self.session.setdefault("created_tasks", []).append(result)

        return result or {}

    async def _action_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pause execution for a specified duration.

        Required *params*:
            seconds (int | float): How long to wait.
        """
        seconds = float(params.get("seconds", 1))
        logger.info("Waiting", seconds=seconds)
        await asyncio.sleep(seconds)
        return {"waited": seconds}

    # ── HTTP helpers ─────────────────────────────────────────────────

    async def _http_request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth_required: bool = True,
    ) -> dict[str, Any] | None:
        """Make an HTTP request to the demo-app backend.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE).
            path: URL path (leading ``/`` is added if missing).
            json_data: Optional JSON body.
            params: Optional query string parameters.
            auth_required: If True, inject the Bearer token from session.

        Returns:
            Parsed JSON response, or None for 204 No Content.

        Raises:
            TriggerError: On non-2xx status or network failure.
        """
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers: dict[str, str] = {}

        if auth_required:
            token = self.session.get("token")
            if not token:
                raise TriggerError(
                    recipe_id="<unknown>",
                    step_index=-1,
                    detail="Auth required but no token in session. Run a login step first.",
                )
            headers[_AUTH_HEADER] = f"Bearer {token}"

        try:
            async with ClientSession(timeout=_DEFAULT_TIMEOUT) as session:  # noqa: SIM117
                async with session.request(
                    method=method,
                    url=url,
                    json=json_data,
                    params=params,
                    headers=headers,
                ) as resp:
                    if 200 <= resp.status < 300:
                        if resp.status == 204 or resp.content_type == "":
                            return None
                        try:
                            result: dict[str, Any] = await resp.json()
                            return result
                        except Exception:
                            return {"status": resp.status, "body": await resp.text()}
                    else:
                        body = await resp.text()
                        raise TriggerError(
                            recipe_id="<unknown>",
                            step_index=-1,
                            detail=(f"HTTP {resp.status} on {method} {path}: {body[:500]}"),
                        )
        except TriggerError:
            raise
        except Exception as exc:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail=f"HTTP request failed: {method} {path} — {exc}",
            ) from exc

    async def _http_post(
        self,
        path: str,
        json_data: dict[str, Any],
        auth_required: bool = True,
    ) -> dict[str, Any]:
        """Convenience wrapper for POST requests (always returns a dict)."""
        result = await self._http_request(
            method="POST",
            path=path,
            json_data=json_data,
            auth_required=auth_required,
        )
        if result is None:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail=f"POST {path} returned empty response",
            )
        return result

    # ── Template resolution ─────────────────────────────────────────

    def _resolve_template(self, value: Any) -> Any:
        """Recursively resolve template variables in *value*.

        Supported patterns:
        - ``{project_id}`` → last created project's ``id``
        - ``{project_id:0}`` → first created project's ``id``
        - ``{task_id}`` → last created task's ``id``
        - ``{task_id:1}`` → second created task's ``id``

        Works on strings, dicts, and lists.
        """
        if isinstance(value, str):
            return self._resolve_string(value)
        elif isinstance(value, dict):
            return {k: self._resolve_template(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._resolve_template(item) for item in value]
        else:
            return value

    def _resolve_string(self, text: str) -> str:
        """Replace template variables in a single string."""

        def _replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            index_str = match.group(2)

            if var_name == "project_id":
                projects = self.session.get("created_projects", [])
                idx = int(index_str) if index_str else -1
                if 0 <= idx < len(projects):
                    return str(projects[idx].get("id", ""))
                elif projects:
                    return str(projects[-1].get("id", ""))
                return match.group(0)  # Keep as-is if unresolvable.

            if var_name == "task_id":
                tasks = self.session.get("created_tasks", [])
                idx = int(index_str) if index_str else -1
                if 0 <= idx < len(tasks):
                    return str(tasks[idx].get("id", ""))
                elif tasks:
                    return str(tasks[-1].get("id", ""))
                return match.group(0)

            # Unknown variable — keep as-is.
            return match.group(0)

        return _TEMPLATE_VAR_RE.sub(_replacer, text)
