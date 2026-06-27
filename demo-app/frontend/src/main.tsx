import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import * as Sentry from "@sentry/react";
import { initErrorReporting } from "@/services/error-reporter";
// OTel-JS observability channel (D5 Task 5.2)
import { initOtel } from "@/observability/otel";
import { initFrontendLogs } from "@/observability/otel-logs";
import { installGlobalErrorHooks } from "@/observability/error-reporter";
import { initInstruments } from "@/observability/instruments";
import "./index.css";
import App from "./App.tsx";

// ── OTel-JS: trace channel (WebTracerProvider → :4318/v1/traces) ──
// MUST be called first so the provider is ready before any span is created.
initOtel();

// ── OTel-JS: logs channel (LoggerProvider → :4318/v1/logs) ──
initFrontendLogs();

// ── OTel-JS: global error hooks → client_error spans ──
installGlobalErrorHooks();

// ── OTel-JS: auto-instrumentations (fetch / document-load / user-interaction) ──
initInstruments();

// ── Existing client error reporting (sendBeacon → Loki direct-push) ──
initErrorReporting();

// Initialize Sentry if DSN is configured
const sentryDsn = import.meta.env.VITE_SENTRY_DSN;
if (sentryDsn) {
  Sentry.init({
    dsn: sentryDsn,
    integrations: [
      Sentry.browserTracingIntegration(),
      Sentry.replayIntegration(),
    ],
    tracesSampleRate: 0.1,
    replaysSessionSampleRate: 0.1,
    replaysOnErrorSampleRate: 1.0,
    environment: import.meta.env.VITE_ENV || "development",
  });
  console.log("[SENTRY_INIT]", { environment: import.meta.env.VITE_ENV || "development" });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>
);
