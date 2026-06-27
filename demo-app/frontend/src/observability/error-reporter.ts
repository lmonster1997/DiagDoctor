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

import {
  trace,
  context,
  SpanStatusCode,
  TraceFlags,
  ROOT_CONTEXT,
} from "@opentelemetry/api";
import type { SpanContext } from "@opentelemetry/api";

// ── Helpers ─────────────────────────────────────────────────────────

/** Read the last-known trace/span IDs captured by the otel.ts processor. */
function _lastKnownContext(): SpanContext | null {
  const win = window as unknown as Record<string, string>;
  const tid = win.__otelLastTraceId;
  const sid = win.__otelLastSpanId;
  if (tid && sid) {
    return { traceId: tid, spanId: sid, traceFlags: TraceFlags.SAMPLED };
  }
  return null;
}

/**
 * Get the best available parent context for the client_error span.
 *
 * Priority:
 *  1. Active span from the current OTel context (fetch still in-flight)
 *  2. Last-known span from __otelLast* window globals
 *     (the API call that just completed and whose response triggered the crash)
 *  3. Root context (fallback — creates a new trace_id)
 *
 * Uses ``trace.setSpanContext()`` (NOT ``trace.wrapSpanContext()``) to
 * create a proper remote parent context so the child span inherits the
 * same ``trace_id`` and sets ``parent_span_id`` correctly.  Using
 * ``wrapSpanContext`` creates a NonRecordingSpan that does not propagate
 * trace context correctly in all SDK versions.
 *
 * This ensures the client_error span shares the same trace_id as the
 * API call that caused the crash, enabling cross-tier correlation in
 * the Doctor ingest pipeline.
 */
function _getErrorParentContext(): import("@opentelemetry/api").Context {
  // 1. Try the currently active context (span still in-flight)
  const activeCtx = context.active();
  const activeSpan = trace.getSpan(activeCtx);
  if (activeSpan) {
    const ctx = activeSpan.spanContext();
    if (ctx.traceId && ctx.spanId) {
      return activeCtx;
    }
  }

  // 2. Fallback to last-known span context (fetch just completed)
  const lastCtx = _lastKnownContext();
  if (lastCtx) {
    // trace.setSpanContext creates a context with a remote parent
    // that properly propagates trace_id to child spans.
    return trace.setSpanContext(ROOT_CONTEXT, lastCtx);
  }

  // 3. No parent — new trace (isolated client_error)
  return ROOT_CONTEXT;
}

/**
 * Trigger an immediate flush of batched OTel spans.
 *
 * Uses the global force-flush handle exposed by otel.ts so that
 * client_error spans are exported to the collector ASAP, before
 * the Playwright trigger advances or the page refreshes.
 */
function _forceFlushAfterError(): void {
  const win = window as unknown as Record<string, () => Promise<void>>;
  const flush = win.__otelForceFlush;
  if (typeof flush === "function") {
    // Fire-and-forget: we don't want to block the error handler.
    flush().catch(() => {});
  }
}

function _reportAsSpan(
  error: string,
  stack: string | null,
  extra: Record<string, string>,
): void {
  const tracer = trace.getTracer("demo-frontend");
  const parentCtx = _getErrorParentContext();
  const span = tracer.startSpan("client_error", {}, parentCtx);
  span.setAttribute("error.message", error);
  if (stack) span.setAttribute("error.stack", stack.slice(0, 4000));
  for (const [k, v] of Object.entries(extra)) {
    if (v) span.setAttribute(k, v);
  }
  span.setStatus({ code: SpanStatusCode.ERROR, message: error.slice(0, 256) });
  span.end();

  // Force-flush so the error span reaches Tempo before the page
  // potentially refreshes or the Playwright trigger moves on.
  _forceFlushAfterError();
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
