/**
 * ToolCallCard — generative UI for tool invocations (Phase 1).
 *
 * Compatible with CopilotKit React 1.62.x via ``renderToolCalls`` prop.
 * Displays tool name, args (expandable), result summary, and status badge.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight, Wrench, Check, SkipForward, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

// ── CopilotKit 1.62.x render props (from ReactToolCallRenderer) ──
// status: "inProgress" | "executing" | "complete"
// result: string | undefined (only present when status === "complete")
interface ToolCallRenderProps {
  name: string;
  toolCallId: string;
  args: Record<string, unknown>;
  status: "inProgress" | "executing" | "complete";
  result: string | undefined;
}

// ── Prop-based registration (for <CopilotKit renderToolCalls={...}>) ──

export const toolCallRenderers = [
  {
    /** Wildcard: match ALL tool calls (no schema filtering). */
    name: "*" as const,
    render: (props: ToolCallRenderProps) => (
      <ToolCallCardContent
        name={props.name}
        toolCallId={props.toolCallId}
        args={props.args ?? {}}
        result={props.result}
        status={props.status}
      />
    ),
  },
];

// ── Card component ──

export function ToolCallCardContent({
  name,
  toolCallId: _toolCallId,
  args,
  result,
  status,
}: {
  name: string;
  /** CopilotKit tool_call_id — unused in UI but required by the protocol. */
  toolCallId: string;
  args: Record<string, unknown>;
  result: string | undefined;
  status: "inProgress" | "executing" | "complete";
}) {
  const [expanded, setExpanded] = useState(status === "inProgress");

  // Dedup detection: if result has "skipped" marker
  const isSkipped =
    typeof result === "string" &&
    result.includes("duplicate") &&
    result.includes("skipped");

  // Format result for display (result is always string | undefined in 1.62.x)
  const resultPreview =
    result != null
      ? result.length > 200
        ? result.slice(0, 200) + "…"
        : result
      : null;

  return (
    <div
      className={cn(
        "my-2 rounded-lg border text-sm transition-colors",
        isSkipped
          ? "border-muted bg-muted/30 opacity-60"
          : "border-border bg-card",
        status === "inProgress" && "border-blue-400/50 bg-blue-50 dark:bg-blue-950/20",
        status === "executing" && "border-primary/50 bg-primary/5 animate-pulse",
        status === "complete" && !isSkipped && "border-green-500/30 bg-green-50 dark:bg-green-950/20",
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
          ) : status === "inProgress" ? (
            <span className="flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400">
              <Loader2 className="size-3 animate-spin" />
              准备中
            </span>
          ) : status === "executing" ? (
            <span className="flex items-center gap-1 text-xs text-primary">
              <Loader2 className="size-3 animate-spin" />
              执行中…
            </span>
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
          {args && Object.keys(args).length > 0 && (
            <div>
              <div className="mb-1 text-xs text-muted-foreground">参数</div>
              <pre className="max-h-40 overflow-auto rounded bg-muted p-2 text-xs text-foreground whitespace-pre-wrap break-all">
                {JSON.stringify(args, null, 2)}
              </pre>
            </div>
          )}

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
