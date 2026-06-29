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
import json
import os
import re
import time
from datetime import UTC, datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import structlog
from aiohttp import ClientSession, ClientTimeout

from bug_factory.schema import (
    BrowserError,
    DiffEvidence,
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
    "comment": "/api/tasks/{task_id}/comments",
}

# Default auth header key inserted by _action_login.
_AUTH_HEADER = "Authorization"

# Default wait for logs to flush to Loki after trigger (must be > Loki
# batch-send interval of 5 s, plus buffer for network).  Evidence collector
# runs after this wait so logs are already in Loki at query time.
_LOG_FLUSH_SECONDS = 8


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
        frontend_url: str | None = None,
    ) -> None:
        self.base_url = demo_app_base_url.rstrip("/")
        self.log_flush_seconds = log_flush_seconds
        self.frontend_url = frontend_url  # None → use recipe's value
        self.session: dict[str, Any] = {
            "token": None,
            "created_projects": [],
            "created_tasks": [],
        }
        self.browser_errors: list[BrowserError] = []
        self.diff_evidence: list[DiffEvidence] = []
        logger.info(
            "TriggerRunner initialised",
            base_url=self.base_url,
            frontend_url=self.frontend_url,
            log_flush_seconds=self.log_flush_seconds,
        )

    async def _enrich_browser_errors_from_page(self, page: Any) -> None:
        """Fill null trace_id/span_id in recent browser_errors via page.evaluate.

        Reads ``window.__otelLastTraceId`` (set by the frontend OTel span
        processor) and patches any :class:`BrowserError` whose ``trace_id``
        is still ``None``.  Called just before ``browser.close()`` in UI
        action handlers.
        """
        if not self.browser_errors:
            return
        try:
            otel_ctx = await page.evaluate(
                """() => ({
                    trace_id: window.__otelLastTraceId || '',
                    span_id: window.__otelLastSpanId || '',
                })"""
            )
            _tid = (otel_ctx.get("trace_id") or "").strip()
            _sid = (otel_ctx.get("span_id") or "").strip()
            if not _tid and not _sid:
                return
            # Patch the last few errors that are still missing context.
            patched = 0
            for be in reversed(self.browser_errors):
                if be.trace_id is None and _tid:
                    be.trace_id = _tid
                    patched += 1
                if be.span_id is None and _sid:
                    be.span_id = _sid
                if patched >= 10:  # don't patch everything
                    break
            if patched:
                logger.info(
                    "Enriched browser errors with OTel context",
                    patched=patched,
                    trace_id=_tid,
                    span_id=_sid,
                )
        except Exception as exc:
            logger.debug("Failed to enrich browser errors from page", error=str(exc))

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
        _browser_established = False  # True after first ui_navigate / ui_click

        try:
            for i, step in enumerate(trigger.steps):
                # ── ui_reachable 门控 ──────────────────────────────────
                # When ui_reachable=True (the default), api_call steps are
                # rejected UNLESS a browser session has already been
                # established by a prior ui_navigate or ui_click step.
                # login / create_data / wait are always exempt — they are
                # data-setup steps, not the fault-triggering step.
                if (
                    trigger.ui_reachable
                    and step.action == "api_call"
                    and step.action not in self._UI_REACHABLE_SAFE_ACTIONS
                    and not _browser_established
                ):
                    step_result = StepResult(
                        action=step.action,
                        params=step.params,
                        success=False,
                        elapsed_ms=0,
                        error=(
                            "api_call is forbidden when ui_reachable=True. "
                            "Use ui_click or ui_navigate to trigger through "
                            "the real browser, or set ui_reachable=False on "
                            "the trigger for backend-only / unreachable scenarios."
                        ),
                    )
                    steps.append(step_result)
                    overall_success = False
                    error_msg = f"Step {i} ({step.action}) rejected by ui_reachable gate"
                    logger.error(
                        "Step rejected by ui_reachable gate",
                        step_index=i,
                        action=step.action,
                    )
                    break

                step_result = await self._execute_step(i, step)
                steps.append(step_result)

                # Track that a browser session has been established so
                # subsequent api_call steps are allowed.
                if step_result.success and step.action in ("ui_navigate", "ui_click"):
                    _browser_established = True

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
            browser_errors=self.browser_errors,
            diff_evidence=self.diff_evidence,
        )
        logger.info(
            "Trigger run complete",
            success=overall_success,
            step_count=len(steps),
            browser_error_count=len(self.browser_errors),
            diff_evidence_count=len(self.diff_evidence),
        )
        return result

    # ── Action dispatcher ───────────────────────────────────────────

    _ACTION_HANDLERS: dict[str, str] = {
        "login": "_action_login",
        "api_call": "_action_api_call",
        "ui_click": "_action_ui_click",
        "ui_navigate": "_action_ui_navigate",
        "create_data": "_action_create_data",
        "wait": "_action_wait",
        "collect_diff": "_action_collect_diff",
    }

    # Actions that are always allowed even when ui_reachable=True (data setup, not triggering).
    _UI_REACHABLE_SAFE_ACTIONS: frozenset[str] = frozenset(
        {"login", "create_data", "wait", "collect_diff"}
    )

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
            expected_status (list[int]): HTTP statuses that are treated as
                success rather than failure (e.g. [500] for expected 500s).

        Template variables like ``{project_id}`` or ``{task_id}`` in *path*
        and *body* are resolved from session state before each request.
        """
        method = (params.get("method", "GET") or "GET").upper()
        path_template = params.get("path", "/")
        body_template = params.get("body")
        query = params.get("query")
        repeat = max(1, int(params.get("repeat", 1)))
        expected_status: set[int] = set(params.get("expected_status", []))

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
                expected_status=expected_status,
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

        frontend_url = params.get("frontend_url", "http://localhost:5173")
        # Resolve template variables like {task_id}, {project_id} in the URL.
        frontend_url = self._resolve_template(frontend_url)

        # ── Apply --frontend-url CLI override (replaces origin only) ──
        if self.frontend_url:
            parsed = urlparse(frontend_url)
            override_parsed = urlparse(self.frontend_url)
            frontend_url = urlunparse(
                (
                    override_parsed.scheme or parsed.scheme or "http",
                    override_parsed.netloc or parsed.netloc or "localhost:5173",
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )

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
            # Use system Edge on Windows to avoid 182MB Chromium download.
            launch_kwargs: dict[str, Any] = {"headless": True}
            if os.name == "nt":
                launch_kwargs["channel"] = "msedge"
            browser = await pw.chromium.launch(**launch_kwargs)
            page = await browser.new_page()

            # ── Capture browser-side errors for FE evidence ─────────
            def _capture_console(msg: Any) -> None:
                """Handler for ALL console messages — capture errors/warnings."""
                logger.debug(
                    "Browser console message",
                    type=msg.type,
                    text=msg.text[:500] if msg.text else "",
                )
                if msg.type in ("error", "warning"):
                    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
                    loc = msg.location
                    text = msg.text[:2000] if msg.text else ""

                    # ── Extract trace_id / span_id / component_stack / breadcrumbs ──
                    _trace_id: str | None = None
                    _span_id: str | None = None
                    _comp_stack: str | None = None
                    _breadcrumbs: list[str] = []
                    _trace_match = re.search(r"trace_id=([a-f0-9]{32})", text)
                    if _trace_match:
                        _trace_id = _trace_match.group(1)
                    _span_match = re.search(r"span_id=([a-f0-9]{16})", text)
                    if _span_match:
                        _span_id = _span_match.group(1)
                    _comp_match = re.search(r"comp=(.*?)(?:\s+\w+=|$)", text)
                    if _comp_match and _comp_match.group(1).strip():
                        _comp_stack = _comp_match.group(1).strip()
                    _crumbs_match = re.search(r"crumbs=(\d+)", text)
                    if _crumbs_match:
                        _breadcrumbs = [f"breadcrumb_count={_crumbs_match.group(1)}"]

                    self.browser_errors.append(
                        BrowserError(
                            timestamp=now,
                            type=f"console_{msg.type}",
                            message=text,
                            url=loc.get("url") if loc else None,
                            line_number=loc.get("lineNumber") if loc else None,
                            trace_id=_trace_id,
                            span_id=_span_id,
                            component_stack=_comp_stack,
                            breadcrumbs=_breadcrumbs,
                        )
                    )
                    logger.info(
                        "Captured browser console error",
                        type=msg.type,
                        message=msg.text[:200] if msg.text else "",
                        trace_id=_trace_id,
                        span_id=_span_id,
                    )

            def _capture_pageerror(err: Exception) -> None:
                """Handler for uncaught JS exceptions (pageerror)."""
                now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
                self.browser_errors.append(
                    BrowserError(
                        timestamp=now,
                        type="pageerror",
                        message=str(err)[:2000],
                        stack=getattr(err, "stack", None),
                    )
                )
                logger.info(
                    "Captured browser page error",
                    message=str(err)[:200],
                )

            page.on("console", _capture_console)
            page.on("pageerror", _capture_pageerror)
            # ── End browser error capture ────────────────────────────

            try:
                # ── Phase 1: Set auth token BEFORE navigating to protected page ──
                # Navigate to login page first (always publicly accessible),
                # set localStorage with auth token, then reload to let Zustand
                # persist middleware rehydrate from localStorage.
                parsed_target = urlparse(frontend_url)
                base_origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
                await page.goto(
                    f"{base_origin}/login", wait_until="domcontentloaded", timeout=15000
                )

                # Set auth token in localStorage so ProtectedRoute allows access.
                token = self.session.get("token")
                if token:
                    await page.evaluate(
                        """(t) => {
                            localStorage.setItem('taskflow-auth', JSON.stringify({
                                state: { token: t, currentUser: null },
                                version: 0
                            }));
                        }""",
                        token,
                    )
                    # Reload the page so Zustand's persist middleware picks up
                    # the token from localStorage during store initialisation.
                    await page.reload(wait_until="networkidle", timeout=15000)
                    await asyncio.sleep(2)  # Let Zustand fully rehydrate

                # ── Phase 2: Navigate to the actual target (protected) URL ──
                await page.goto(frontend_url, wait_until="networkidle", timeout=15000)
                logger.info("Navigated to target", url=page.url, expected=frontend_url)

                # Zustand persist middleware may rehydrate AFTER first render,
                # causing ProtectedRoute to redirect to /login.  Retry if needed.
                for _attempt in range(3):
                    await asyncio.sleep(1)
                    current = page.url
                    if "/login" not in current:
                        logger.info("Successfully on target page", url=current)
                        break
                    logger.warning("Still on login — re-navigating", attempt=_attempt, url=current)
                    await page.goto(frontend_url, wait_until="networkidle", timeout=15000)

                # Wait for React to hydrate / render (client-side JS).
                await asyncio.sleep(2)

                # Click the target element.
                # For "body" selector (typical for FE render-crash bugs),
                # skip the click — the crash already happened on render.
                # For other selectors, use force=True to bypass actionability
                # checks (visible/enabled/stable) for robustness.
                if selector == "body":
                    await asyncio.sleep(1)
                else:
                    await page.click(selector, force=True)
                    await asyncio.sleep(1)  # Let any navigation / animation settle.

                # Additional wait to capture async console errors
                # (e.g. frontend timeout→retry cycles, cascade failures).
                post_wait_click = float(params.get("post_wait", 0))
                if post_wait_click > 0:
                    logger.info("Post-click wait for async errors", seconds=post_wait_click)
                    await asyncio.sleep(post_wait_click)

            finally:
                await self._enrich_browser_errors_from_page(page)
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

        for _rep_idx in range(repeat):
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

            # Resolve task for comment creation — cycle through created tasks.
            if entity == "comment":
                tasks = self.session.get("created_tasks", [])
                if not tasks:
                    raise TriggerError(
                        recipe_id="<unknown>",
                        step_index=-1,
                        detail="Cannot create comment: no task in session. "
                        "Create a task first, or provide task_id in data.",
                    )
                task = tasks[_rep_idx % len(tasks)]
                resolved_data["task_id"] = task["id"]

            # Resolve the route path: use resolved project_id / task_id if available.
            project_id = resolved_data.get("project_id")
            task_id = resolved_data.pop("task_id", None)
            if project_id and "{project_id}" in route:
                resolved_path = route.replace("{project_id}", str(project_id))
            elif task_id and "{task_id}" in route:
                resolved_path = route.replace("{task_id}", str(task_id))
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

    async def _action_ui_navigate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Navigate the browser to a frontend URL via Playwright.

        Unlike ``_action_ui_click``, this only navigates (``page.goto``) —
        no element click is performed.  The frontend naturally initiates
        fetch requests with ``traceparent`` headers, linking the browser
        span to the backend span in the same distributed trace.

        Required *params*:
            url (str): The frontend URL path or full URL to navigate to
                (e.g. ``/projects/{project_id}`` or
                ``http://localhost:5173/projects``).

        Optional *params*:
            wait_until (str): Playwright wait_until strategy.
                Default ``"networkidle"``.
            timeout (int): Navigation timeout in ms. Default 15000.
        """
        target = params.get("url", "")
        if not target:
            raise TriggerError(
                recipe_id="<unknown>",
                step_index=-1,
                detail="ui_navigate action requires 'url' param",
            )

        # Resolve template variables and apply frontend_url override.
        target = self._resolve_template(target)
        frontend_url = self.frontend_url or "http://localhost:5173"
        parsed_target = urlparse(target)
        # If target is a relative path, prepend frontend_url origin.
        if not parsed_target.netloc:
            target = frontend_url.rstrip("/") + ("/" + target.lstrip("/") if target else "")
        else:
            override_parsed = urlparse(frontend_url)
            target = urlunparse(
                (
                    override_parsed.scheme or parsed_target.scheme or "http",
                    override_parsed.netloc or parsed_target.netloc or "localhost:5173",
                    parsed_target.path,
                    parsed_target.params,
                    parsed_target.query,
                    parsed_target.fragment,
                )
            )

        wait_until = params.get("wait_until", "networkidle")
        timeout_ms = int(params.get("timeout", 15000))
        # post_wait: extra seconds to keep the browser open after navigation
        # so async console errors (retries, timeouts) can be captured.
        post_wait = float(params.get("post_wait", 0))

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
            launch_kwargs: dict[str, Any] = {"headless": True}
            if os.name == "nt":
                launch_kwargs["channel"] = "msedge"
            browser = await pw.chromium.launch(**launch_kwargs)
            page = await browser.new_page()

            # ── Capture browser-side errors for FE evidence ─────────
            def _capture_console(msg: Any) -> None:
                logger.debug(
                    "Browser console message",
                    type=msg.type,
                    text=msg.text[:500] if msg.text else "",
                )
                if msg.type in ("error", "warning"):
                    now = datetime.now(UTC).isoformat()
                    loc = msg.location
                    text = msg.text[:2000] if msg.text else ""

                    # ── Extract trace_id / span_id / component_stack / breadcrumbs ──
                    _trace_id: str | None = None
                    _span_id: str | None = None
                    _comp_stack: str | None = None
                    _breadcrumbs: list[str] = []
                    _trace_match = re.search(r"trace_id=([a-f0-9]{32})", text)
                    if _trace_match:
                        _trace_id = _trace_match.group(1)
                    _span_match = re.search(r"span_id=([a-f0-9]{16})", text)
                    if _span_match:
                        _span_id = _span_match.group(1)
                    _comp_match = re.search(r"comp=(.*?)(?:\s+\w+=|$)", text)
                    if _comp_match and _comp_match.group(1).strip():
                        _comp_stack = _comp_match.group(1).strip()
                    _crumbs_match = re.search(r"crumbs=(\d+)", text)
                    if _crumbs_match:
                        _breadcrumbs = [f"breadcrumb_count={_crumbs_match.group(1)}"]

                    self.browser_errors.append(
                        BrowserError(
                            timestamp=now,
                            type=f"console_{msg.type}",
                            message=text,
                            url=loc.get("url") if loc else None,
                            line_number=loc.get("lineNumber") if loc else None,
                            trace_id=_trace_id,
                            span_id=_span_id,
                            component_stack=_comp_stack,
                            breadcrumbs=_breadcrumbs,
                        )
                    )
                    logger.info(
                        "Captured browser console error",
                        type=msg.type,
                        message=msg.text[:200] if msg.text else "",
                        trace_id=_trace_id,
                        span_id=_span_id,
                    )

            def _capture_pageerror(err: Exception) -> None:
                now = datetime.now(UTC).isoformat()
                self.browser_errors.append(
                    BrowserError(
                        timestamp=now,
                        type="pageerror",
                        message=str(err)[:2000],
                        stack=getattr(err, "stack", None),
                    )
                )
                logger.info(
                    "Captured browser page error",
                    message=str(err)[:200],
                )

            page.on("console", _capture_console)
            page.on("pageerror", _capture_pageerror)
            # ── End browser error capture ────────────────────────────

            try:
                # Set auth token in localStorage before navigating to
                # protected pages so the frontend Zustand store can
                # rehydrate and ProtectedRoute won't redirect to /login.
                parsed_target = urlparse(target)
                base_origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
                login_url = f"{base_origin}/login"
                await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)

                token = self.session.get("token")
                if token:
                    await page.evaluate(
                        """(t) => {
                            localStorage.setItem('taskflow-auth', JSON.stringify({
                                state: { token: t, currentUser: null },
                                version: 0
                            }));
                        }""",
                        token,
                    )
                    await page.reload(wait_until="networkidle", timeout=timeout_ms)
                    await asyncio.sleep(2)

                # Navigate to the actual target URL.
                await page.goto(target, wait_until=wait_until, timeout=timeout_ms)
                logger.info("UI navigate completed", url=page.url, target=target)

                # Let React hydrate and fetch data (with traceparent).
                await asyncio.sleep(2)

                # Additional wait to capture async console errors
                # (e.g. frontend timeout→retry cycles, cascade failures).
                if post_wait > 0:
                    logger.info("Post-navigate wait for async errors", seconds=post_wait)
                    await asyncio.sleep(post_wait)

            finally:
                await self._enrich_browser_errors_from_page(page)
                await browser.close()

        logger.info("UI navigate executed", target=target)
        return {"navigated_to": target}

    async def _action_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pause execution for a specified duration.

        Required *params*:
            seconds (int | float): How long to wait.
        """
        seconds = float(params.get("seconds", 1))
        logger.info("Waiting", seconds=seconds)
        await asyncio.sleep(seconds)
        return {"waited": seconds}

    async def _action_collect_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        """Collect behavioural diff evidence for "smokeless" bugs.

        Unlike error-signal bugs, logic/data/config bugs produce normal
        HTTP responses and no error signals.  This action makes one or
        more follow-up API calls AFTER the bug has been triggered, then
        compares the actual behaviour against expected behaviour to
        produce explicit discrepancy evidence that a Doctor agent can use.

        Required *params*:
            diff_type (str): Semantic tag — one of:
                ``access_control_anomaly`` | ``silent_data_loss`` |
                ``data_invariant_broken`` | ``behavior_mismatch``
            description (str): Human-readable summary of what is being checked.
            steps (list[dict]): Sequence of API calls to collect comparison
                data.  Each step dict has:
                - method (str): HTTP method
                - path (str): URL path (supports template vars)
                - label (str): Human label for this data point
                - body (dict, optional): JSON request body
            expectation (str): What the correct behaviour SHOULD look like.
            discrepancy (str): One-sentence template describing the mismatch;
                ``{actual}`` and ``{expected}`` are replaced with collected data.

        Example (LOGIC-020 IDOR)::

            params:
              diff_type: access_control_anomaly
              description: "Verify that a user cannot see another user's project"
              steps:
                - method: GET
                  path: /api/projects/{project_id}
                  label: "admin accessing alice's project"
              expectation: "Should return 404 or 403 — admin does not own this project"
              discrepancy: "Returned 200 with full project data owned by another user"
        """
        diff_type = params.get("diff_type", "behavior_mismatch")
        description = params.get("description", "")
        steps_cfg: list[dict[str, Any]] = params.get("steps", [])
        expectation = params.get("expectation", "")
        discrepancy_tpl = params.get("discrepancy", "")

        observations: dict[str, Any] = {}
        for i, step_cfg in enumerate(steps_cfg):
            label = step_cfg.get("label", f"step_{i}")
            method = step_cfg.get("method", "GET").upper()
            path = self._resolve_template(step_cfg.get("path", "/"))
            body = self._resolve_template(step_cfg.get("body")) if step_cfg.get("body") else None
            try:
                result = await self._http_request(
                    method=method,
                    path=path,
                    json_data=body,
                    auth_required=True,
                )
                observations[label] = result
                logger.info("Collect diff step succeeded", label=label, method=method, path=path)
            except Exception as exc:
                observations[label] = {"error": str(exc)}
                logger.warning("Collect diff step failed", label=label, error=str(exc))

        # Build discrepancy text
        actual_summary = json.dumps(observations, ensure_ascii=False, default=str)
        discrepancy = discrepancy_tpl.replace("{actual}", actual_summary[:2000])

        diff = DiffEvidence(
            diff_type=diff_type,
            description=description,
            request_context={
                "current_user": self.session.get("current_user"),
                "steps": steps_cfg,
                "expectation": expectation,
            },
            observation=observations,
            discrepancy=discrepancy,
        )
        self.diff_evidence.append(diff)
        logger.info(
            "Diff evidence collected",
            diff_type=diff_type,
            description=description[:100],
        )
        return {"diff_type": diff_type, "observations": observations}

    # ── HTTP helpers ─────────────────────────────────────────────────

    async def _http_request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth_required: bool = True,
        expected_status: set[int] | None = None,
    ) -> dict[str, Any] | None:
        """Make an HTTP request to the demo-app backend.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE).
            path: URL path (leading ``/`` is added if missing).
            json_data: Optional JSON body.
            params: Optional query string parameters.
            auth_required: If True, inject the Bearer token from session.
            expected_status: HTTP statuses that are treated as success
                (e.g. ``{500}`` for an expected IntegrityError).

        Returns:
            Parsed JSON response, or None for 204 No Content.

        Raises:
            TriggerError: On non-2xx status (unless in *expected_status*)
                or network failure.
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
                    accept = expected_status or set()
                    if 200 <= resp.status < 300 or resp.status in accept:
                        if resp.status == 204 or resp.content_type == "":
                            return {"status": resp.status}
                        try:
                            result: dict[str, Any] = await resp.json()
                            # Include the status code so callers can inspect it.
                            result["_status"] = resp.status
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
        - ``{project_owner_id}`` → last created project's ``owner_id``
        - ``{project_owner_id:0}`` → first created project's ``owner_id``
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

            if var_name == "project_owner_id":
                projects = self.session.get("created_projects", [])
                idx = int(index_str) if index_str else -1
                if 0 <= idx < len(projects):
                    return str(projects[idx].get("owner_id", ""))
                elif projects:
                    return str(projects[-1].get("owner_id", ""))
                return match.group(0)

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
