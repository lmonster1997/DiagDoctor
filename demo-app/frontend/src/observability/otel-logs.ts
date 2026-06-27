/**
 * OTel-JS Logs channel — browser-side logs → OTLP → Loki.
 *
 * Pushes breadcrumb-style logs (route changes, key clicks, console.warn)
 * directly to the OTel collector via fetch() as OTLP/JSON.
 *
 * Uses raw fetch() instead of @opentelemetry/sdk-logs because the
 * experimental 0.x SDK's OTLPLogExporter is a non-functional stub.
 *
 * See: D5 Task 5.2 / from-scratch §13.1.1
 */

import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

// ── Constants ──────────────────────────────────────────────────────

const LOG_EXPORTER_URL = "http://localhost:4318/v1/logs";
const SERVICE_NAME = "demo-frontend";

// ── Batching ───────────────────────────────────────────────────────

interface LogRecord {
  timestamp: number; // epoch ms
  severityText: string;
  body: string;
  attributes: Record<string, string>;
}

let _buffer: LogRecord[] = [];
let _flushTimer: ReturnType<typeof setInterval> | null = null;

function buildOtlpPayload(records: LogRecord[]): string {
  const nowNs = String(Date.now() * 1_000_000);
  const attrs = (r: LogRecord) =>
    Object.entries(r.attributes).map(([k, v]) => ({
      key: k,
      value: { stringValue: v },
    }));

  return JSON.stringify({
    resourceLogs: [
      {
        resource: {
          attributes: [
            { key: ATTR_SERVICE_NAME, value: { stringValue: SERVICE_NAME } },
          ],
        },
        scopeLogs: [
          {
            scope: { name: SERVICE_NAME },
            logRecords: records.map((r) => ({
              timeUnixNano: String(r.timestamp * 1_000_000),
              severityText: r.severityText,
              body: { stringValue: r.body },
              attributes: attrs(r),
            })),
          },
        ],
      },
    ],
  });
}

async function flushBuffer(): Promise<void> {
  if (_buffer.length === 0) return;
  const payload = buildOtlpPayload(_buffer.splice(0));
  try {
    await fetch(LOG_EXPORTER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
    });
  } catch {
    // Collector may be down — silently drop on the floor
  }
}

// ── Public API ─────────────────────────────────────────────────────

let _initialized = false;

/**
 * Initialise the logs channel.
 *
 * Call this ONCE, early in main.tsx, before any React rendering.
 */
export function initFrontendLogs(): void {
  if (_initialized) return;
  _initialized = true;

  // Flush every 3 seconds
  _flushTimer = setInterval(flushBuffer, 3000);

  // Also flush on page unload
  window.addEventListener("beforeunload", () => flushBuffer());

  console.log(
    `[OTEL-LOGS] Direct OTLP logs channel ready (service=${SERVICE_NAME}, exporter=${LOG_EXPORTER_URL})`,
  );
}

/**
 * Emit a breadcrumb-style log via OTel OTLP.
 *
 * Safe to call before initFrontendLogs() — records are buffered.
 */
export function emitOtelLog(
  level: "info" | "warn" | "error",
  body: string,
  attributes?: Record<string, string>,
): void {
  _buffer.push({
    timestamp: Date.now(),
    severityText: level.toUpperCase(),
    body,
    attributes: attributes || {},
  });
}
