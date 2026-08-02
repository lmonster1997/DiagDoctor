/**
 * ToolCallCard — generative UI for tool invocations ("AI 诊断室" 执行步骤插片).
 *
 * Compatible with CopilotKit React 1.62.x via ``renderToolCalls`` prop.
 * Each tool call appears as a numbered execution step in the message flow:
 *   icon + tool name · param summary + status (spinner/✓/⚠️/skipped).
 * Click to expand full JSON args + result.
 */
import { useState, useRef } from "react";
import {
  ChevronDown,
  ChevronRight,
  Wrench,
  Check,
  SkipForward,
  Loader2,
  Search,
  Database,
  Settings,
  RefreshCw,
  Bug,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ── Tool icon map ──────────────────────────────────────────────────
const TOOL_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  search_observability: Search,
  search_logs: Search,
  search_traces: Search,
  query_database: Database,
  code_search: Search,
  get_trace: Search,
  get_logs: Search,
  buginfo: Bug,
};
const DEFAULT_TOOL_ICON = Wrench;

function toolIcon(name: string): React.ComponentType<{ className?: string }> {
  const key = name.toLowerCase().replace(/[^a-z_]/g, "");
  return TOOL_ICON[key] ?? DEFAULT_TOOL_ICON;
}

/** Human-readable tool name */
function toolLabel(name: string): string {
  const map: Record<string, string> = {
    search_observability: "查询可观测性数据",
    search_logs: "搜索日志",
    search_traces: "搜索 Trace",
    query_database: "查询数据库",
    code_search: "代码搜索",
    get_trace: "获取 Trace 详情",
    get_logs: "获取日志详情",
    buginfo: "提取 Bug 信息",
  };
  const key = name.toLowerCase().replace(/[^a-z_]/g, "");
  return map[key] ?? name;
}

/** Friendly contextual hint — "正在…" style, replaces raw function name */
function friendlyHint(name: string, args: Record<string, unknown>): string {
  const key = name.toLowerCase().replace(/[^a-z_]/g, "");

  // Extract common params for contextual hints
  const query = typeof args.query === "string" ? args.query : null;
  const timeRange = typeof args.time_range === "string" ? args.time_range : null;
  const service = typeof args.service === "string" ? args.service : null;
  const file = typeof args.file === "string" ? args.file : null;

  const timeHint = timeRange
    ? `最近 ${timeRange} 的`
    : "最近的";

  switch (key) {
    case "search_observability":
      if (query) return `正在检索 ${timeHint}可观测性信号："${query.slice(0, 40)}"…`;
      return `正在采集 ${timeHint}可观测性数据…`;
    case "search_logs":
      if (service) return `正在搜索 ${service} ${timeHint}日志…`;
      if (query) return `正在搜索日志："${query.slice(0, 40)}"…`;
      return `正在检索 ${timeHint}日志记录…`;
    case "search_traces":
      if (service) return `正在分析 ${service} ${timeHint}Trace 链路…`;
      return `正在查询 ${timeHint}分布式 Trace…`;
    case "query_database":
      if (query) return `正在执行数据库查询…`;
      return `正在查询数据库…`;
    case "code_search":
      if (file) return `正在搜索代码库：${file}…`;
      if (query) return `正在代码库中搜索："${query.slice(0, 40)}"…`;
      return `正在扫描相关源代码…`;
    case "get_trace":
      return `正在获取 Trace 详情…`;
    case "get_logs":
      return `正在拉取日志详情…`;
    case "buginfo":
      return `正在从描述中提取 Bug 信息…`;
    default:
      return `正在执行 ${toolLabel(name)}…`;
  }
}

/** Short param summary (auto-ellipsis) */
function paramSummary(args: Record<string, unknown>): string {
  const keys = Object.keys(args);
  if (keys.length === 0) return "无参数";
  const first = keys[0];
  const val = args[first];
  const valStr = typeof val === "string" ? val : JSON.stringify(val);
  const short = valStr.length > 40 ? valStr.slice(0, 40) + "…" : valStr;
  if (keys.length === 1) return `${first}: ${short}`;
  return `${first}: ${short} +${keys.length - 1}`;
}

// ── CopilotKit 1.62.x render props ─────────────────────────────────
interface ToolCallRenderProps {
  name: string;
  toolCallId: string;
  args: Record<string, unknown>;
  status: "inProgress" | "executing" | "complete";
  result: string | undefined;
}

// ── Wildcard registration ─────────────────────────────────────────
export const toolCallRenderers = [
  {
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

// ── Step counter ("诊断流水线") ─────────────────────────────────────
let globalStepCounter = 0;

/** Reset the step counter (call when a new diagnosis session starts). */
export function resetStepCounter(): void {
  globalStepCounter = 0;
}

/** Get and increment step number. */
function nextStepNumber(): number {
  globalStepCounter += 1;
  return globalStepCounter;
}

// ── Step component ─────────────────────────────────────────────────
export function ToolCallCardContent({
  name,
  toolCallId: _toolCallId,
  args,
  result,
  status,
}: {
  name: string;
  toolCallId: string;
  args: Record<string, unknown>;
  result: string | undefined;
  status: "inProgress" | "executing" | "complete";
}) {
  const [expanded, setExpanded] = useState(status === "inProgress");

  // Assign a stable step number on first render
  const stepNumberRef = useRef<number | null>(null);
  if (stepNumberRef.current === null) {
    stepNumberRef.current = nextStepNumber();
  }
  const stepNumber = stepNumberRef.current;

  const isSkipped =
    typeof result === "string" &&
    result.includes("duplicate") &&
    result.includes("skipped");

  const resultPreview =
    result != null
      ? result.length > 200
        ? result.slice(0, 200) + "…"
        : result
      : null;

  const Icon = toolIcon(name);
  const displayName = toolLabel(name);
  const summary = paramSummary(args);
  const hint = friendlyHint(name, args);

  const isPending = status === "inProgress" || status === "executing";
  const isDone = status === "complete" && !isSkipped;

  // Detect genuine tool failure (not just result containing "error" word)
  const isFailed =
    isDone &&
    typeof result === "string" &&
    (result.startsWith("Error:") ||
      result.startsWith("ERROR") ||
      result.includes("Traceback") ||
      result.includes("Exception:"));

  // Fill input with a suggestion to retry differently
  const handleRetryHint = () => {
    navigator.clipboard.writeText(
      `上一个工具调用（${displayName}）没有返回预期结果，请尝试其他路径或参数。`
    );
  };

  return (
    <div
      className={cn(
        "my-2 mx-4 overflow-hidden rounded-lg border transition-all duration-300",
        isSkipped && "border-white/[0.04] bg-white/[0.01] opacity-50",
        isPending && "border-l-2 border-l-cyan-500/30 border-r border-r-white/[0.06] border-t border-t-white/[0.06] border-b border-b-white/[0.06] bg-blue-500/[0.04]",
        isFailed && "border-l-2 border-l-amber-500/60 border-r border-r-white/[0.06] border-t border-t-white/[0.06] border-b border-b-white/[0.06] bg-amber-500/[0.03]",
        isDone && !isFailed && "border-l-2 border-l-cyan-500/30 border-r border-r-white/[0.06] border-t border-t-white/[0.06] border-b border-b-white/[0.06] bg-white/[0.02]",
      )}
    >
      {/* Step header — always visible */}
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-white/[0.03]"
      >
        {/* Step number badge */}
        <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-white/[0.04] text-[10px] font-mono font-medium text-[#5c6070]">
          {isSkipped ? "—" : stepNumber}
        </span>

        {/* Status icon */}
        <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-white/[0.04]">
          {isSkipped ? (
            <SkipForward className="size-3.5 text-[#5c6070]" />
          ) : isPending ? (
            <Loader2 className="size-3.5 animate-spin text-[#3b82f6]" />
          ) : (
            <Check className="size-3.5 text-[#22c55e]" />
          )}
        </span>

        {/* Tool name & hint */}
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <Icon className="size-3 shrink-0 text-[#8a8fa3]" />
            <span className="text-xs font-medium text-[#e4e4ef] truncate">
              {displayName}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[10px] text-[#5c6070]">
            {isPending ? hint : summary}
          </span>
        </span>

        {/* Status badge */}
        <span className="flex shrink-0 items-center gap-1">
          {isSkipped ? (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-white/[0.03] px-2 py-0.5 text-[10px] text-[#5c6070] line-through"
              title={`已在第 ${stepNumber} 步执行，跳过`}
            >
              已跳过
            </span>
          ) : isPending ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/[0.08] px-2 py-0.5 text-[10px] text-[#3b82f6]">
              {status === "inProgress" ? "准备中" : "执行中…"}
            </span>
          ) : isFailed ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/[0.08] px-2 py-0.5 text-[10px] text-amber-400">
              异常
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-full bg-green-500/[0.08] px-2 py-0.5 text-[10px] text-[#22c55e]">
              完成
            </span>
          )}
          <span className="text-[#5c6070]">
            {expanded ? (
              <ChevronDown className="size-3.5" />
            ) : (
              <ChevronRight className="size-3.5" />
            )}
          </span>
        </span>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-white/[0.04] bg-white/[0.01] px-3 py-2.5 space-y-2.5 animate-fade-in">
          {/* Args */}
          {args && Object.keys(args).length > 0 && (
            <div>
              <div className="mb-1 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
                参数
              </div>
              <pre className="max-h-40 overflow-auto rounded-md border border-white/[0.06] bg-[#0a0a0f] p-2.5 text-[11px] leading-relaxed text-[#8a8fa3] whitespace-pre-wrap break-all font-mono">
                {JSON.stringify(args, null, 2)}
              </pre>
            </div>
          )}

          {/* Result */}
          {resultPreview && (
            <div>
              <div className="mb-1 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
                结果
              </div>
              <pre className="max-h-40 overflow-auto rounded-md border border-white/[0.06] bg-[#0a0a0f] p-2.5 text-[11px] leading-relaxed text-[#8a8fa3] whitespace-pre-wrap break-all font-mono">
                {resultPreview}
              </pre>
            </div>
          )}

          {/* Retry hint for failed tools */}
          {isFailed && (
            <button
              type="button"
              onClick={handleRetryHint}
              className="flex w-full items-center gap-2 rounded-md bg-amber-500/[0.06] px-3 py-2 text-left text-[11px] text-amber-300/80 transition-colors hover:bg-amber-500/[0.10] hover:text-amber-300"
              title="点击复制追问到剪贴板，粘贴到聊天框引导 AI 换个思路"
            >
              <RefreshCw className="size-3 shrink-0" />
              <span className="flex-1">尝试其他方案？</span>
              <span className="text-[9px] text-amber-400/50">点击复制追问</span>
            </button>
          )}

        </div>
      )}
    </div>
  );
}
