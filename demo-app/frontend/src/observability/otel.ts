/**
 * OTel-JS Trace provider — WebTracerProvider + OTLPTraceExporter.
 *
 * Creates the browser-side tracing pipeline:
 *   Browser spans → OTLPTraceExporter → http://localhost:4318/v1/traces
 *
 * This is the **independent trace channel** for demo-frontend
 * (service.name = "demo-frontend").  The FetchInstrumentation (registered
 * in instruments.ts) injects the W3C traceparent header so backend spans
 * appear under the same trace_id.
 *
 * See: D5 Task 5.2 / from-scratch §13.1
 */

import { WebTracerProvider, BatchSpanProcessor } from "@opentelemetry/sdk-trace-web";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { ZoneContextManager } from "@opentelemetry/context-zone";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import { trace } from "@opentelemetry/api";

// ── Trace exporter ──────────────────────────────────────────────────

const TRACE_EXPORTER_URL = "http://localhost:4318/v1/traces";

/**
 * Initialise the OTel WebTracerProvider and register it globally.
 *
 * Call this ONCE, early in main.tsx, before any React rendering.
 */
export function initOtel(): void {
  const exporter = new OTLPTraceExporter({
    url: TRACE_EXPORTER_URL,
  });

  // ── Expose last-known trace_id for Playwright E2E capture ──
  // When React ErrorBoundary fires, the fetch span may already be ended,
  // so trace.getActiveSpan() returns null.  This fallback lets the
  // Playwright trigger (page.evaluate) read the last trace_id seen by
  // any instrumentation, preserving the cross-tier link for bug-factory
  // evidence collection (see from-scratch §13.1.2 / §13.1.4).
  const win = window as unknown as Record<string, unknown>;
  win.__otelLastTraceId = "";
  win.__otelLastSpanId = "";

  // Custom processor: update __otelLastTraceId / __otelLastSpanId on every span start.
  const _traceIdUpdater = {
    forceFlush: () => Promise.resolve(),
    shutdown: () => Promise.resolve(),
    onStart(span: { spanContext: () => { traceId: string; spanId: string } }) {
      try {
        const ctx = span.spanContext();
        win.__otelLastTraceId = ctx.traceId;
        win.__otelLastSpanId = ctx.spanId;
      } catch { /* ignore */ }
    },
    onEnd() { /* noop */ },
  };

  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: "demo-frontend",
    }),
    spanProcessors: [_traceIdUpdater, new BatchSpanProcessor(exporter)],
  });

  provider.register({
    contextManager: new ZoneContextManager(),
  });

  console.log(
    `[OTEL] Trace provider registered (service=demo-frontend, exporter=${TRACE_EXPORTER_URL})`,
  );
}
