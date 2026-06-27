/**
 * OTel-JS Logs channel — browser-side logs → OTLP → Loki.
 *
 * Pushes breadcrumb-style logs (route changes, key clicks, console.warn)
 * to the OTel collector as structured log records.  Loki 3.x receives them
 * via the otlphttp exporter on the collector side.
 *
 * This is the **independent logs channel** for demo-frontend
 * (service_name = "demo-frontend").  It runs alongside the trace channel
 * (otel.ts) but does NOT depend on it.
 *
 * See: D5 Task 5.2 / from-scratch §13.1.1
 */

import {
  LoggerProvider,
  BatchLogRecordProcessor,
} from "@opentelemetry/sdk-logs";
import { OTLPLogExporter } from "@opentelemetry/exporter-logs-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

// ── Log exporter ────────────────────────────────────────────────────

const LOG_EXPORTER_URL = "http://localhost:4318/v1/logs";

let _loggerProvider: LoggerProvider | null = null;

/**
 * Initialise the OTel LoggerProvider with an OTLP exporter.
 *
 * Call this ONCE, early in main.tsx, before any React rendering.
 */
export function initFrontendLogs(): void {
  const exporter = new OTLPLogExporter({
    url: LOG_EXPORTER_URL,
  });

  _loggerProvider = new LoggerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: "demo-frontend",
    }),
  });
  _loggerProvider.addLogRecordProcessor(new BatchLogRecordProcessor(exporter));

  console.log(
    `[OTEL] Logs provider registered (service=demo-frontend, exporter=${LOG_EXPORTER_URL})`,
  );
}

/**
 * Get a logger instance for emitting OTel log records.
 *
 * Returns `null` if initFrontendLogs() has not been called yet.
 */
export function getLogger(name: string) {
  if (!_loggerProvider) return null;
  return _loggerProvider.getLogger(name);
}

/**
 * Emit a breadcrumb-style info log via OTel.
 *
 * Safe to call before initFrontendLogs() — silently no-ops.
 */
export function emitOtelLog(
  level: "info" | "warn" | "error",
  body: string,
  attributes?: Record<string, string>,
): void {
  const logger = getLogger("demo-frontend");
  if (!logger) return;
  logger.emit({
    severityText: level.toUpperCase(),
    body,
    attributes,
  });
}
