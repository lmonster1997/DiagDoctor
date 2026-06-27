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

  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: "demo-frontend",
    }),
    spanProcessors: [new BatchSpanProcessor(exporter)],
  });

  provider.register({
    contextManager: new ZoneContextManager(),
  });

  console.log(
    `[OTEL] Trace provider registered (service=demo-frontend, exporter=${TRACE_EXPORTER_URL})`,
  );
}
