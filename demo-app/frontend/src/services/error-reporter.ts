/**
 * Client-side error reporter — bridges browser crashes to Loki.
 *
 * Provides:
 * - reportClientError() — called by ErrorBoundary to report React render errors
 * - initErrorReporting()  — called by main.tsx to install global onerror /
 *   unhandledrejection hooks + breadcrumb tracking
 *
 * Errors are sent via navigator.sendBeacon (fire-and-forget) to
 * POST /api/log/client-error, which the backend funnels into Loki.
 *
 * Each error payload now includes the active OTel trace_id and span_id
 * so that the Doctor agent can correlate frontend crashes with backend
 * API calls under the same distributed trace (cross-tier diagnosis).
 */

import { trace } from "@opentelemetry/api";

// ── Types (mirrors backend ClientErrorPayload) ──

interface BreadcrumbEntry {
  category: "click" | "navigation" | "network" | "input" | "lifecycle" | "custom";
  message: string;
  timestamp: string; // ISO-8601
  data?: Record<string, unknown>;
}

interface ClientErrorPayload {
  error: string;
  stack: string | null;
  componentStack: string | null;
  url: string | null;
  timestamp: string | null;
  trace_id: string | null;
  span_id: string | null;
  breadcrumbs: BreadcrumbEntry[];
}

// ── Breadcrumb ring buffer ──

const MAX_BREADCRUMBS = 30;
let _breadcrumbs: BreadcrumbEntry[] = [];

function addBreadcrumb(
  category: BreadcrumbEntry["category"],
  message: string,
  data?: Record<string, unknown>,
): void {
  _breadcrumbs.push({
    category,
    message,
    timestamp: new Date().toISOString(),
    data,
  });
  if (_breadcrumbs.length > MAX_BREADCRUMBS) {
    _breadcrumbs = _breadcrumbs.slice(-MAX_BREADCRUMBS);
  }
}

/** Snapshot current breadcrumbs (shallow copy so later actions don't mutate the report). */
function snapshotBreadcrumbs(): BreadcrumbEntry[] {
  return _breadcrumbs.map((b) => ({ ...b }));
}

// ── Send to backend ──

const CLIENT_ERROR_ENDPOINT = "/api/log/client-error";

function sendErrorPayload(payload: ClientErrorPayload): void {
  // sendBeacon sends text/plain for strings — wrap in Blob to force application/json
  // so FastAPI / Pydantic can parse the body correctly.
  const body = JSON.stringify(payload);
  const blob = new Blob([body], { type: "application/json" });

  const sent = navigator.sendBeacon(CLIENT_ERROR_ENDPOINT, blob);

  if (!sent) {
    // Fallback: fetch with keepalive (best effort during unload).
    fetch(CLIENT_ERROR_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {
      // Silently ignore — we already console.error'd above.
    });
  }
}

// ── Public API ──

export interface ReportClientErrorParams {
  error: string;
  stack?: string | null;
  componentStack?: string | null;
  /** Semantic tag e.g. "react_render", "global_onerror", "unhandledrejection" */
  type?: string;
}

/**
 * Report a client-side error to the backend → Loki.
 *
 * Called by ErrorBoundary.componentDidCatch and global error hooks.
 * Logs to console.error for E2E test visibility (Playwright can capture it).
 *
 * The active OTel trace_id and span_id are embedded so that
 * bug-factory's Playwright trigger can capture them into
 * browser_errors.json, enabling cross-tier trace correlation
 * (see from-scratch §13.1.2 / §13.1.4).
 */
export function reportClientError(params: ReportClientErrorParams): void {
  // ── Extract active OTel trace context ──
  // When called from ErrorBoundary the fetch span may already be ended,
  // so fall back to window.__otelLastTraceId (set by SpanProcessor on
  // every span start — see observability/otel.ts).
  const activeSpan = trace.getActiveSpan();
  const spanCtx = activeSpan?.spanContext();
  const _win = window as unknown as Record<string, string>;
  const _traceId = spanCtx?.traceId || _win.__otelLastTraceId || null;
  const _spanId = spanCtx?.spanId || _win.__otelLastSpanId || null;

  const payload: ClientErrorPayload = {
    error: params.type ? `[${params.type}] ${params.error}` : params.error,
    stack: params.stack ?? null,
    componentStack: params.componentStack ?? null,
    url: window.location.href,
    timestamp: new Date().toISOString(),
    trace_id: _traceId,
    span_id: _spanId,
    breadcrumbs: snapshotBreadcrumbs(),
  };

  // Visible in browser console + captured by Playwright E2E tests.
  // Structured prefix lets bug-factory's trigger.py extract trace_id,
  // span_id, component_stack, and breadcrumb count via regex without
  // evaluating JS arguments (see from-scratch §13.1.2).
  const _compStackFirstLine = (params.componentStack ?? "")
    .split("\n")
    .find((l) => l.trim().startsWith("at "))?.trim()
    .slice(0, 120) ?? "";
  const _breadcrumbCount = snapshotBreadcrumbs().length;

  console.error(
    `[CLIENT_ERROR] trace_id=${_traceId ?? ""} span_id=${_spanId ?? ""}` +
      ` comp=${_compStackFirstLine}` +
      ` crumbs=${_breadcrumbCount}`,
    payload,
  );

  sendErrorPayload(payload);
}

/**
 * Initialise global error hooks and breadcrumb tracking.
 *
 * Called once before React renders (see main.tsx).
 * Installs:
 * - window.onerror          — catches uncaught synchronous errors
 * - unhandledrejection       — catches unhandled Promise rejections
 * - click breadcrumbs        — tracks user actions leading up to errors
 * - navigation breadcrumbs   — tracks page navigations
 *
 * NOTE: React render errors are caught by ErrorBoundary (→ reportClientError),
 * NOT by these hooks. These hooks cover errors OUTSIDE React's tree —
 * event handlers, async callbacks, third-party code, etc.
 */
export function initErrorReporting(): void {
  // ── Breadcrumb: user clicks ──
  document.addEventListener("click", (e: MouseEvent) => {
    const el = e.target as HTMLElement;
    const tag = el.tagName.toLowerCase();
    const text = (el.textContent ?? "").trim().slice(0, 50);
    addBreadcrumb("click", `${tag}${text ? ` "${text}"` : ""}`);
  });

  // ── Breadcrumb: programmatic navigation ──
  const _pushState = history.pushState.bind(history);
  history.pushState = function pushState(...args: Parameters<typeof _pushState>) {
    addBreadcrumb("navigation", `pushState → ${String(args[2] ?? "")}`);
    return _pushState(...args);
  };

  // ── Global onerror ──
  // Covers errors NOT caught by React ErrorBoundary:
  // event handlers, setTimeout callbacks, third-party scripts, etc.
  window.onerror = (message, _source, _lineno, _colno, error) => {
    reportClientError({
      error: String(message),
      stack: error?.stack ?? null,
      type: "global_onerror",
    });
  };

  // ── Unhandled promise rejections ──
  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    reportClientError({
      error: `Unhandled Promise Rejection: ${String(reason)}`,
      stack: reason?.stack ?? null,
      type: "unhandledrejection",
    });
  });
}
