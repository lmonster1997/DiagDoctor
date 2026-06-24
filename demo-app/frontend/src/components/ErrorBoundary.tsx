import React from "react";
import * as Sentry from "@sentry/react";

interface ErrorBoundaryProps {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundaryClass extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error("[REACT_RENDER_ERROR]", {
      error: error.message,
      componentStack: errorInfo.componentStack,
    });

    // Report to Sentry if configured
    if (import.meta.env.VITE_SENTRY_DSN) {
      Sentry.captureException(error, {
        contexts: {
          react: {
            componentStack: errorInfo.componentStack,
          },
        },
      });
    }

    // Report to backend → Loki (fire-and-forget; don't block the UI).
    const payload = JSON.stringify({
      error: error.message,
      stack: error.stack ?? null,
      componentStack: errorInfo.componentStack ?? null,
      url: window.location.href,
      timestamp: new Date().toISOString(),
    });
    const endpoint = `${window.location.origin}/api/log/client-error`;
    if (navigator.sendBeacon) {
      navigator.sendBeacon(endpoint, new Blob([payload], { type: "application/json" }));
    } else {
      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true,
      }).catch(() => {});
    }
  }

  render(): React.ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <div className="flex min-h-screen items-center justify-center bg-background p-8">
          <div className="max-w-md rounded-xl border border-destructive/30 bg-card p-8 text-center shadow-lg">
            <div className="mb-4 text-4xl">⚠️</div>
            <h1 className="mb-2 text-xl font-semibold text-foreground">
              页面出现错误
            </h1>
            <p className="mb-4 text-sm text-muted-foreground">
              {this.state.error?.message || "发生了未知错误，请刷新页面重试。"}
            </p>
            <button
              onClick={() => {
                this.setState({ hasError: false, error: null });
                window.location.reload();
              }}
              className="inline-flex h-8 items-center justify-center rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/80 transition-colors"
            >
              刷新页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

/** ErrorBoundary wrapping children; uses Sentry if DSN is configured. */
export function ErrorBoundary({ children, fallback }: ErrorBoundaryProps) {
  const dsn = import.meta.env.VITE_SENTRY_DSN;

  if (dsn) {
    return (
      <Sentry.ErrorBoundary
        fallback={({ error, resetError }) => (
          <FallbackUI
            message={
              error instanceof Error ? error.message : "发生了未知错误，请刷新页面重试。"
            }
            onReset={resetError}
          />
        )}
        beforeCapture={(scope) => {
          scope.setTag("source", "ErrorBoundary");
        }}
      >
        {children}
      </Sentry.ErrorBoundary>
    );
  }

  return (
    <ErrorBoundaryClass fallback={fallback}>
      {children}
    </ErrorBoundaryClass>
  );
}

function FallbackUI({
  message,
  onReset,
}: {
  message: string;
  onReset?: () => void;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-8">
      <div className="max-w-md rounded-xl border border-destructive/30 bg-card p-8 text-center shadow-lg">
        <div className="mb-4 text-4xl">⚠️</div>
        <h1 className="mb-2 text-xl font-semibold text-foreground">
          页面出现错误
        </h1>
        <p className="mb-4 text-sm text-muted-foreground">{message}</p>
        <button
          onClick={() => (onReset ? onReset() : window.location.reload())}
          className="inline-flex h-8 items-center justify-center rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/80 transition-colors"
        >
          刷新页面
        </button>
      </div>
    </div>
  );
}
