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
 * Backend origin(s) that should receive the W3C traceparent header
 * so frontend and backend spans share the same trace_id.
 */
const BACKEND_ORIGINS = [
  /^http:\/\/localhost:8000/, // demo-backend (dev)
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
        // Inject traceparent into requests to demo-backend so the
        // browser span and the backend server span share a trace_id.
        propagateTraceHeaderCorsUrls: BACKEND_ORIGINS,
      }),
      new XMLHttpRequestInstrumentation(),
      new DocumentLoadInstrumentation(),
      new UserInteractionInstrumentation({
        // Only trace meaningful interactions (not every mousemove).
        eventNames: ["click", "dragstart", "drop", "submit"],
      }),
    ],
  });
}
