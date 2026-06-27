/**
 * OTel error reporter — global error hooks → client_error span events.
 *
 * Installs window.onerror and unhandledrejection handlers that create
 * dedicated ``client_error`` span events on the active OTel trace.
 * These spans are exported to Tempo alongside the normal trace spans
 * so the Doctor agent can correlate frontend crashes with backend
 * API calls under the same ``trace_id``.
 *
 * Also provides ``reportClientErrorSpan()`` for ErrorBoundary integration.
 *
 * NOTE: This file works alongside services/error-reporter.ts which
 * handles the sendBeacon → Loki direct-push path.  The two channels
 * are independent and complementary.
 *
 * See: D5 Task 5.2 / from-scratch §13.1.2
 */

import { trace, SpanStatusCode } from "@opentelemetry/api";

// ── Helpers ─────────────────────────────────────────────────────────

function _currentTraceId(): string {
  const span = trace.getActiveSpan();
  if (!span) return "";
  return span.spanContext().traceId;
}

function _reportAsSpan(
  error: string,
  stack: string | null,
  extra: Record<string, string>,
): void {
  const tracer = trace.getTracer("demo-frontend");
  const span = tracer.startSpan("client_error");
  span.setAttribute("error.message", error);
  if (stack) span.setAttribute("error.stack", stack.slice(0, 4000));
  for (const [k, v] of Object.entries(extra)) {
    if (v) span.setAttribute(k, v);
  }
  span.setStatus({ code: SpanStatusCode.ERROR, message: error.slice(0, 256) });
  span.end();
}

// ── Public API ──────────────────────────────────────────────────────

export interface OTelErrorParams {
  error: string;
  stack?: string | null;
  componentStack?: string | null;
  type?: string;
}

/**
 * Create a ``client_error`` span event for an error caught by
 * ErrorBoundary or another React-level handler.
 *
 * Call this from ErrorBoundary.componentDidCatch to link the
 * render error into the active OTel trace.
 */
export function reportClientErrorSpan(params: OTelErrorParams): void {
  _reportAsSpan(params.error, params.stack ?? null, {
    "error.type": params.type ?? "react_render",
    "error.component_stack": params.componentStack ?? "",
    "page.url": window.location.href,
  });
}

/**
 * Install global error hooks that create client_error spans via OTel.
 *
 * These hooks complement (not replace) the sendBeacon path in
 * services/error-reporter.ts.  Both fire on the same events.
 *
 * Call this ONCE, early in main.tsx, after initOtel().
 */
export function installGlobalErrorHooks(): void {
  // ── window.onerror ─────────────────────────────────────────
  window.addEventListener("error", (event: ErrorEvent) => {
    _reportAsSpan(
      event.message || "Unknown error",
      event.error?.stack ?? null,
      {
        "error.type": "global_onerror",
        "page.url": window.location.href,
        "source.filename": event.filename ?? "",
        "source.lineno": String(event.lineno ?? ""),
      },
    );
  });

  // ── unhandledrejection ─────────────────────────────────────
  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    _reportAsSpan(
      `Unhandled Promise Rejection: ${String(reason)}`,
      reason?.stack ?? null,
      {
        "error.type": "unhandledrejection",
        "page.url": window.location.href,
      },
    );
  });

  console.log("[OTEL] Global error hooks installed (client_error spans)");
}
