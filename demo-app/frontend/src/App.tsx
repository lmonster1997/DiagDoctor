import { useEffect, useRef } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { emitOtelLog } from "@/observability/otel-logs";
import LoginPage from "@/pages/LoginPage";
import RegisterPage from "@/pages/RegisterPage";
import ProjectsPage from "@/pages/ProjectsPage";
import TaskBoardPage from "@/pages/TaskBoardPage";
import TaskDetailPage from "@/pages/TaskDetailPage";

/** Log every route change as a breadcrumb for diagnostics. */
function RouteLogger() {
  const location = useLocation();
  const prevRef = useRef<string | null>(null);

  useEffect(() => {
    if (prevRef.current !== null) {
      emitOtelLog("info", `[ROUTE] ${prevRef.current} → ${location.pathname}`, {
        "route.from": prevRef.current,
        "route.to": location.pathname,
      });
    }
    prevRef.current = location.pathname;
  }, [location.pathname]);

  return null;
}

function App() {
  return (
    <ErrorBoundary>
      <RouteLogger />
      <Routes>
        {/* Public routes */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />

        {/* Protected routes */}
        <Route
          path="/projects"
          element={
            <ProtectedRoute>
              <ProjectsPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/projects/:id"
          element={
            <ProtectedRoute>
              <TaskBoardPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/tasks/:id"
          element={
            <ProtectedRoute>
              <TaskDetailPage />
            </ProtectedRoute>
          }
        />

        {/* Default redirect */}
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Routes>
    </ErrorBoundary>
  );
}

export default App;
