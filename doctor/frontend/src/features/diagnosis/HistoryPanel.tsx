/**
 * HistoryPanel - 历史诊断列表 (#5 F3).
 *
 * 消费 GET /api/diagnose/threads(paused 置顶)。每条展示状态 / thread_id(可复制)
 * / findings_count / early_stopped。
 *
 * 恢复暂停诊断:
 *   - v1(本组件):点「切换恢复」-> useCopilotContext().setThreadId(thread_id)
 *     把 CopilotKit 聊天切到该暂停线程。后续交互时 prepare_stream 检测到 active
 *     interrupt 且无 resume -> 发 OnInterrupt -> F1 引导卡浮现(见 plan §1.1/§1.3.1)。
 *   - v2(待办):切换后自动调 useCoAgent().start() 立即浮现引导卡,免去手动交互。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCopilotContext } from "@copilotkit/react-core";
import { RefreshCw, Copy, Check, ArrowRight, History as HistoryIcon } from "lucide-react";
import { listThreads, type DiagnosisThread } from "@/api/client";

interface HistoryPanelProps {
  /** 切换线程后回调(如切回证据链 tab)。 */
  onResumed?: () => void;
}

export function HistoryPanel({ onResumed }: HistoryPanelProps) {
  const { setThreadId } = useCopilotContext();
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["diagnosis-threads"],
    queryFn: () => listThreads(),
    staleTime: 10_000,
  });

  const threads = data?.threads ?? [];

  const handleCopy = async (tid: string) => {
    await navigator.clipboard.writeText(tid);
    setCopiedId(tid);
    setTimeout(() => setCopiedId(null), 1500);
  };

  const handleResume = (tid: string) => {
    setThreadId(tid);
    onResumed?.();
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header: count + refresh */}
      <div className="flex shrink-0 items-center justify-between border-b border-white/[0.06] px-3 py-2">
        <span className="text-[10px] font-medium uppercase tracking-wider text-[#5c6070]">
          {threads.length > 0 ? `${threads.length} 条诊断` : "历史诊断"}
        </span>
        <button
          type="button"
          onClick={() => refetch()}
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[10px] text-[#5c6070] transition-all hover:bg-white/[0.04] hover:text-[#8a8fa3]"
        >
          <RefreshCw className={`size-3 ${isFetching ? "animate-spin" : ""}`} />
          刷新
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <EmptyRow text="加载历史诊断…" />
        ) : error ? (
          <EmptyRow text="加载失败" sub={(error as Error).message} />
        ) : threads.length === 0 ? (
          <EmptyRow text="暂无历史诊断" sub="完成或暂停的诊断会出现在这里" />
        ) : (
          threads.map((t) => (
            <ThreadRow
              key={t.thread_id}
              thread={t}
              copied={copiedId === t.thread_id}
              onCopy={handleCopy}
              onResume={handleResume}
            />
          ))
        )}
      </div>
    </div>
  );
}

function ThreadRow({
  thread,
  copied,
  onCopy,
  onResume,
}: {
  thread: DiagnosisThread;
  copied: boolean;
  onCopy: (tid: string) => void;
  onResume: (tid: string) => void;
}) {
  const isPaused = thread.status === "paused";
  const shortId = thread.thread_id.slice(0, 8);

  return (
    <div
      className={`flex flex-col gap-1.5 border-b border-white/[0.04] px-3 py-2.5 transition-colors ${
        isPaused ? "bg-amber-500/[0.04]" : "hover:bg-white/[0.02]"
      }`}
    >
      <div className="flex items-center gap-2">
        <StatusBadge status={thread.status} />
        <code className="flex-1 truncate font-mono text-[11px] text-[#8a8fa3]" title={thread.thread_id}>
          {shortId}
        </code>
        <button
          type="button"
          onClick={() => onCopy(thread.thread_id)}
          className="shrink-0 text-[#5c6070] transition-colors hover:text-[#8a8fa3]"
          title="复制 thread_id"
        >
          {copied ? <Check className="size-3 text-green-400" /> : <Copy className="size-3" />}
        </button>
      </div>

      <div className="flex items-center gap-2 text-[10px] text-[#5c6070]">
        <span>{thread.findings_count} 条发现</span>
        {thread.early_stopped && (
          <span className="rounded bg-amber-500/10 px-1 py-0.5 text-amber-400/80">预算耗尽</span>
        )}
        {thread.hitl_resumed && (
          <span className="rounded bg-cyan-500/10 px-1 py-0.5 text-cyan-400/80">已续查</span>
        )}
        {thread.has_report && <span className="text-blue-400/60">有报告</span>}
      </div>

      {isPaused && (
        <button
          type="button"
          onClick={() => onResume(thread.thread_id)}
          className="flex items-center justify-center gap-1 rounded-md bg-amber-500/15 px-2 py-1 text-[11px] font-medium text-amber-300 transition-all hover:bg-amber-500/25"
        >
          切换并恢复
          <ArrowRight className="size-3" />
        </button>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: DiagnosisThread["status"] }) {
  if (status === "paused") {
    return (
      <span className="flex shrink-0 items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-amber-300">
        <span className="size-1.5 rounded-full bg-amber-400 animate-breathe" />
        暂停
      </span>
    );
  }
  if (status === "completed") {
    return (
      <span className="shrink-0 rounded bg-blue-500/15 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-blue-300">
        完成
      </span>
    );
  }
  return (
    <span className="shrink-0 rounded bg-white/[0.04] px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-[#5c6070]">
      空
    </span>
  );
}

function EmptyRow({ text, sub }: { text: string; sub?: string }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
      <HistoryIcon className="size-7 text-[#5c6070] opacity-40" />
      <p className="text-sm text-[#8a8fa3]">{text}</p>
      {sub && <p className="text-xs text-[#5c6070]">{sub}</p>}
    </div>
  );
}
