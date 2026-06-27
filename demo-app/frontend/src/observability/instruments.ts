/**
 * OTel-JS instrumentations — auto-instrumentation for the browser.
 *
 * Registers:
 * - FetchInstrumentation     — traces all fetch() calls (injects traceparent)
 * - XMLHttpRequestInstrumentation — traces XHR calls
 * - DocumentLoadInstrumentation   — traces page load
 * - UserInteractionInstrumentation — traces clicks / drag / drop / submit
 *
 * See: D5 Task 5.2 / from-scratch §13.1
 */

import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { XMLHttpRequestInstrumentation } from "@opentelemetry/instrumentation-xml-http-request";
import { DocumentLoadInstrumentation } from "@opentelemetry/instrumentation-document-load";
import { UserInteractionInstrumentation } from "@opentelemetry/instrumentation-user-interaction";

/**
 * Origins that should receive the W3C traceparent header so frontend
 * and backend spans share the same trace_id.
 *
 * In dev mode, Vite proxies /api → localhost:8000, so the browser sees
 * same-origin requests.  Same-origin fetch requests automatically get
 * traceparent injected by FetchInstrumentation.
 *
 * The explicit backend origins here serve as a belt-and-suspenders
 * safety net for scenarios where the request goes directly to the
 * backend (e.g. Docker Compose mode where nginx, not Vite, proxies).
 */
const BACKEND_ORIGINS = [
  /^http:\/\/localhost:8000/,   // demo-backend (Vite proxy target)
  /^http:\/\/127\.0\.0\.1:8000/,
];

/**
 * Register all auto-instrumentations.
 *
 * Must be called AFTER initOtel() has created the provider but BEFORE
 * the app triggers any fetch / user-interaction spans.
 */
export function initInstruments(): void {
  registerInstrumentations({
    instrumentations: [
      new FetchInstrumentation({
        // Inject traceparent into cross-origin requests to demo-backend.
        // Same-origin requests (through Vite proxy) get traceparent
        // automatically — no extra config needed.
        propagateTraceHeaderCorsUrls: BACKEND_ORIGINS,
      }),
      new XMLHttpRequestInstrumentation({
        // CRITICAL: The frontend uses axios (XHR), not fetch().  XHR
        // instrumentation must ALSO be told which cross-origin URLs
        // should receive the traceparent header so frontend XHR spans
        // and backend server spans share the same trace_id.
        propagateTraceHeaderCorsUrls: BACKEND_ORIGINS,
      }),
      new DocumentLoadInstrumentation(),
      new UserInteractionInstrumentation({
        // Only trace meaningful interactions (not every mousemove).
        eventNames: ["click", "dragstart", "drop", "submit"],
      }),
    ],
  });
}
