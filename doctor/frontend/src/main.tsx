import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";
import "./index.css";
import App from "./App.tsx";
import { toolCallRenderers } from "@/features/diagnosis/ToolCallCard";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <CopilotKit
          runtimeUrl="/api/copilotkit"
          useSingleEndpoint={false}
          renderToolCalls={toolCallRenderers}
          showDevConsole={import.meta.env.DEV}
        >
          <App />
        </CopilotKit>
      </QueryClientProvider>
    </BrowserRouter>
  </StrictMode>,
);
