/**
 * HistoryPanel - 历史诊断列表 (#5 F3 + P0 历史报告查看).
 *
 * 消费 GET /api/diagnose/threads(paused 置顶)。每条展示状态 / thread_id(可复制)
 * / findings_count / early_stopped。
 *
 * 恢复暂停诊断:
 *   - 点「切换恢复」-> onResume(tid) 上抛 DiagnosePage(v2 无 useCopilotContext,
 *     threadId 改 DiagnosePage 本地 state 控制 + <CopilotChat threadId>)。后续交互时
 *     prepare_stream 检测到 active interrupt 且无 resume -> 发 OnInterrupt ->
 *     useInterrupt 渲染 F1 引导卡(见 plan §1.1/§1.3.1)。切线程后历史消息由
 *     DiagnosePage 的 backfill effect 回填(agent.setMessages)。
 *
 * P0 历史报告查看:有报告的行点「查看报告」-> GET /api/diagnose/threads/{tid}
 * -> modal 只读渲染 ReportPanel(不传 runId -> 反馈按钮禁用,纯查看)。
 * 见 docs/hitl-evolution-plan.md §3。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  RefreshCw,
  Copy,
  Check,
  ArrowRight,
  History as HistoryIcon,
  FileText,
  Plus,
  X,
  Loader2,
} from "lucide-react";
import { listThreads, getThread, type DiagnosisThread } from "@/api/client";
import { ReportPanel } from "./ReportPanel";

interface HistoryPanelProps {
  /** 切换线程后回调(如切回证据链 tab)。 */
  onResumed?: () => void;
  /** P2: completed case「追加诊断」--由 DiagnosePage 处理(切线程 + 回填历史消息)。 */
  onFollowup?: (tid: string) => void;
  /** paused case「切换恢复」--由 DiagnosePage 处理(v2 无 useCopilotContext,
   *  threadId 改 DiagnosePage 本地 state 控制;切线程后历史消息由 backfill effect 回填)。 */
  onResume?: (tid: string) => void;
}

export function HistoryPanel({ onResumed, onFollowup, onResume }: HistoryPanelProps) {
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [viewingTid, setViewingTid] = useState<string | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["diagnosis-threads"],
    queryFn: () => listThreads(),
    staleTime: 10_000,
  });

  // P0: 历史报告详情(按需 fetch,点「查看报告」才触发)
  const { data: detail, isLoading: detailLoading, error: detailError } = useQuery({
    queryKey: ["diagnosis-thread", viewingTid],
    queryFn: () => getThread(viewingTid!),
    enabled: !!viewingTid,
  });

  const threads = data?.threads ?? [];

  const handleCopy = async (tid: string) => {
    await navigator.clipboard.writeText(tid);
    setCopiedId(tid);
    setTimeout(() => setCopiedId(null), 1500);
  };

  const handleResume = (tid: string) => {
    onResume?.(tid);
    onResumed?.();
  };

  // P2「追加诊断」:委托 DiagnosePage 的 onFollowup(切线程 + 回填历史消息 +
  // 切 tab)。v2 消息回填在 DiagnosePage 的 useEffect[threadId] 里走 agent.setMessages。
  const handleFollowup = (tid: string) => {
    onFollowup?.(tid);
  };

  const handleView = (tid: string) => setViewingTid(tid);
  const handleClose = () => setViewingTid(null);

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
              onFollowup={handleFollowup}
              onView={handleView}
            />
          ))
        )}
      </div>

      {/* P0: 历史报告 modal(只读) */}
      {viewingTid && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={handleClose}
        >
          <div
            className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg border border-white/[0.08] bg-[#13141c] shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-white/[0.06] bg-[#13141c] px-4 py-2.5">
              <span className="flex items-center gap-1.5 text-[11px] font-medium text-[#8a8fa3]">
                <FileText className="size-3.5" />
                历史诊断报告
                <code className="ml-1 font-mono text-[10px] text-[#5c6070]">{viewingTid.slice(0, 8)}</code>
              </span>
              <button
                type="button"
                onClick={handleClose}
                className="text-[#5c6070] transition-colors hover:text-[#8a8fa3]"
                title="关闭"
              >
                <X className="size-4" />
              </button>
            </div>
            <div className="p-4">
              {detailLoading ? (
                <div className="flex items-center justify-center gap-2 py-12 text-sm text-[#5c6070]">
                  <Loader2 className="size-4 animate-spin" />
                  加载报告…
                </div>
              ) : detailError ? (
                <div className="py-12 text-center text-sm text-red-400/80">
                  加载失败:{(detailError as Error).message}
                </div>
              ) : detail?.report ? (
                <ReportPanel report={detail.report} />
              ) : (
                <div className="py-12 text-center text-sm text-[#5c6070]">该诊断无报告</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ThreadRow({
  thread,
  copied,
  onCopy,
  onResume,
  onFollowup,
  onView,
}: {
  thread: DiagnosisThread;
  copied: boolean;
  onCopy: (tid: string) => void;
  onResume: (tid: string) => void;
  onFollowup: (tid: string) => void;
  onView: (tid: string) => void;
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
        {thread.round > 1 && (
          <span className="rounded bg-cyan-500/10 px-1 py-0.5 text-cyan-400/80">第 {thread.round} 轮</span>
        )}
        {thread.rounds_exhausted && (
          <span className="rounded bg-white/[0.06] px-1 py-0.5 text-[#8a8fa3]">复诊上限</span>
        )}
        {thread.has_report && <span className="text-blue-400/60">有报告</span>}
      </div>

      {/* 操作:paused -> 切换恢复;completed -> 追加诊断(P2);有报告 -> 查看报告(P0) */}
      {thread.has_report && (
        <div className="flex gap-1.5">
          {isPaused ? (
            <button
              type="button"
              onClick={() => onResume(thread.thread_id)}
              className="flex flex-1 items-center justify-center gap-1 rounded-md bg-amber-500/15 px-2 py-1 text-[11px] font-medium text-amber-300 transition-all hover:bg-amber-500/25"
            >
              切换并恢复
              <ArrowRight className="size-3" />
            </button>
          ) : !thread.rounds_exhausted ? (
            <button
              type="button"
              onClick={() => onFollowup(thread.thread_id)}
              className="flex flex-1 items-center justify-center gap-1 rounded-md bg-blue-500/15 px-2 py-1 text-[11px] font-medium text-blue-300 transition-all hover:bg-blue-500/25"
              title="切到该 case + 回填历史诊断对话,再追加信息 -> 开复诊轮(继承上轮诊断)"
            >
              <Plus className="size-3" />
              追加诊断
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onView(thread.thread_id)}
            className={`flex items-center justify-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-all ${
              isPaused || !thread.rounds_exhausted
                ? "bg-white/[0.04] text-[#8a8fa3] hover:bg-white/[0.08]"
                : "flex-1 bg-blue-500/15 text-blue-300 hover:bg-blue-500/25"
            }`}
          >
            <FileText className="size-3" />
            查看报告
          </button>
        </div>
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
