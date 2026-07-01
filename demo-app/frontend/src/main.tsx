import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import * as Sentry from "@sentry/react";
import { initErrorReporting } from "@/services/error-reporter";
// OTel-JS observability channel (D5 Task 5.2)
import { initOtel } from "@/observability/otel";
import { initFrontendLogs, emitOtelLog } from "@/observability/otel-logs";
import { installGlobalErrorHooks } from "@/observability/error-reporter";
import { initInstruments } from "@/observability/instruments";
import "./index.css";
import App from "./App.tsx";

// ── OTel-JS: trace channel (WebTracerProvider → :4318/v1/traces) ──
// MUST be called first so the provider is ready before any span is created.
initOtel();

// ── OTel-JS: logs channel (LoggerProvider → :4318/v1/logs) ──
initFrontendLogs();

// ── Intercept console.warn → OTel logs channel (breadcrumb noise) ──
const _origWarn = console.warn.bind(console);
console.warn = (...args: unknown[]) => {
  _origWarn(...args);
  emitOtelLog("warn", `[CONSOLE_WARN] ${String(args[0])}`, {
    "console.severity": "warn",
  });
};

// ── Intercept console.error → OTel logs channel (frontend errors) ──
// 将 React 渲染期间的 console.error（如 "Cannot read properties of undefined"）
// 也发送到 Loki，确保 Doctor Agent 能通过 search_observability 查询到。
// 这与 error-reporter.ts 的 window.onerror / unhandledrejection 互补：
// - window.onerror → Tempo (client_error span)
// - console.error → Loki (ERROR 日志)
const _origError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  _origError(...args);
  const message = args
    .map((a) => (typeof a === "string" ? a : a instanceof Error ? a.message : JSON.stringify(a)))
    .join(" ");
  emitOtelLog("error", `[CONSOLE_ERROR] ${message}`, {
    "console.severity": "error",
  });
};

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
