import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import * as Sentry from "@sentry/react";
import { initErrorReporting } from "@/services/error-reporter";
import "./index.css";
import App from "./App.tsx";

// ── Initialize client-side error reporting (MUST run before React renders) ──
// Installs window.onerror, unhandledrejection hooks, and breadcrumb tracking.
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
