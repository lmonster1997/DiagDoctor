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
import type { SpanProcessor, Span } from "@opentelemetry/sdk-trace-web";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { ZoneContextManager } from "@opentelemetry/context-zone";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

// ── Trace exporter ──────────────────────────────────────────────────

const TRACE_EXPORTER_URL = "http://localhost:4318/v1/traces";

/** Global reference to the BatchSpanProcessor so error handlers can force-flush. */
let _batchProcessor: BatchSpanProcessor | null = null;

/**
 * Force-flush pending OTel spans immediately.
 *
 * Call this after creating a ``client_error`` span so it is exported
 * to the collector before the page potentially refreshes or the
 * Playwright trigger moves on.
 */
export async function forceFlushOtel(): Promise<void> {
  if (_batchProcessor) {
    await _batchProcessor.forceFlush();
  }
}

/**
 * SpanProcessor that captures the last-known trace_id / span_id on
 * every span start so that error reporters can link client_error spans
 * to the originating API call even after the fetch span has ended.
 */
class LastTraceIdProcessor implements SpanProcessor {
  private _win: Record<string, unknown>;

  constructor(win: Record<string, unknown>) {
    this._win = win;
    win.__otelLastTraceId = "";
    win.__otelLastSpanId = "";
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }

  onStart(span: Span, _parentContext: import("@opentelemetry/api").Context): void {
    try {
      const ctx = span.spanContext();
      this._win.__otelLastTraceId = ctx.traceId;
      this._win.__otelLastSpanId = ctx.spanId;
    } catch {
      /* ignore */
    }
  }

  onEnd(_span: Span): void {
    /* noop */
  }
}

/**
 * Initialise the OTel WebTracerProvider and register it globally.
 *
 * Call this ONCE, early in main.tsx, before any React rendering.
 */
export function initOtel(): void {
  const exporter = new OTLPTraceExporter({
    url: TRACE_EXPORTER_URL,
  });

  const win = window as unknown as Record<string, unknown>;
  win.__otelLastTraceId = "";
  win.__otelLastSpanId = "";

  _batchProcessor = new BatchSpanProcessor(exporter, {
    // Reduce the batch delay so client_error spans are exported quickly.
    // Default is 5000ms; 1500ms gives enough time for batching without
    // losing spans before the Playwright trigger moves on.
    scheduledDelayMillis: 1500,
    // Reduce max export batch size so rare spans (errors) flush sooner.
    maxExportBatchSize: 64,
  });

  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: "demo-frontend",
    }),
    spanProcessors: [new LastTraceIdProcessor(win), _batchProcessor],
  });

  // Expose for global access (force-flush from error handlers).
  win.__otelForceFlush = () => forceFlushOtel();

  provider.register({
    contextManager: new ZoneContextManager(),
  });

  // ── Flush pending spans on page unload ───────────────────────
  window.addEventListener("beforeunload", () => {
    _batchProcessor?.forceFlush().catch(() => {});
  });

  console.log(
    `[OTEL] Trace provider registered (service=demo-frontend, exporter=${TRACE_EXPORTER_URL})`,
  );
}
