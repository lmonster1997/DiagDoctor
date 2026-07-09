/**
 * ToolCallCard — generative UI for tool invocations (Phase 1).
 *
 * Registered globally via useRenderToolCall. Displays tool name, args
 * (expandable), result summary, latency, and dedup-skip markers.
 */
import { useState } from "react";
import { useRenderToolCall } from "@copilotkit/react-core";
import { ChevronDown, ChevronRight, Wrench, Check, SkipForward, Clock } from "lucide-react";
import { cn } from "@/lib/utils";

interface ToolCallArgs {
  name: string;
  args: Record<string, unknown>;
  result?: unknown;
  status: "executing" | "complete" | "error";
}

export function registerToolCallCards() {
  useRenderToolCall<ToolCallArgs>({
    render: ({ name, args, result, status }) => {
      return (
        <ToolCallCardContent
          name={name}
          args={args}
          result={result}
          status={status ?? "executing"}
        />
      );
    },
  });
}

function ToolCallCardContent({
  name,
  args,
  result,
  status,
}: {
  name: string;
  args: Record<string, unknown>;
  result: unknown;
  status: string;
}) {
  const [expanded, setExpanded] = useState(false);

  // Dedup detection: if result has "skipped" marker
  const isSkipped =
    typeof result === "string" &&
    result.includes("duplicate") &&
    result.includes("skipped");

  // Format result for display
  const resultPreview =
    result != null
      ? typeof result === "string"
        ? result.length > 200
          ? result.slice(0, 200) + "…"
          : result
        : JSON.stringify(result).slice(0, 200)
      : null;

  return (
    <div
      className={cn(
        "my-2 rounded-lg border text-sm transition-colors",
        isSkipped
          ? "border-muted bg-muted/30 opacity-60"
          : "border-border bg-card",
        status === "executing" && "border-primary/50 bg-primary/5 animate-pulse",
        status === "error" && "border-destructive/50 bg-destructive/5",
      )}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/30 transition-colors"
      >
        <span className="flex items-center gap-1.5 text-muted-foreground">
          {expanded ? (
            <ChevronDown className="size-3.5" />
          ) : (
            <ChevronRight className="size-3.5" />
          )}
          <Wrench className="size-3.5" />
        </span>
        <span className="flex-1 font-medium text-foreground">{name}</span>
        <span className="flex items-center gap-1">
          {isSkipped ? (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <SkipForward className="size-3" />
              跳过
            </span>
          ) : status === "executing" ? (
            <span className="text-xs text-primary">执行中…</span>
          ) : status === "error" ? (
            <span className="text-xs text-destructive">失败</span>
          ) : (
            <span className="flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
              <Check className="size-3" />
              完成
            </span>
          )}
        </span>
      </button>

      {/* Expanded body */}
      {expanded && (
        <div className="border-t border-border px-3 py-2 space-y-2">
          {/* Args */}
          <div>
            <div className="mb-1 text-xs text-muted-foreground">参数</div>
            <pre className="max-h-40 overflow-auto rounded bg-muted p-2 text-xs text-foreground whitespace-pre-wrap break-all">
              {JSON.stringify(args, null, 2)}
            </pre>
          </div>

          {/* Result */}
          {resultPreview && (
            <div>
              <div className="mb-1 text-xs text-muted-foreground">结果</div>
              <pre className="max-h-40 overflow-auto rounded bg-muted p-2 text-xs text-foreground whitespace-pre-wrap break-all">
                {resultPreview}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
