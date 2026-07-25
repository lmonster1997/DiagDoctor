/**
 * DiagnosePage — "协同诊断室" 主诊断页
 *
 * §1 设计约束：
 *   - CopilotChat 永远可见且可输入（左侧 flex-1，始终挂载）
 *   - 右侧 420px 面板：进度 | 证据链 | 初步分析（胶囊标签）
 *   - 新报告 → "初步分析" 标签出现蓝色圆点徽章（脉冲一次后静止），不自动跳转
 *   - 状态指示灯：灰→青脉动(分析中)→蓝静止(有初步分析)，永不绿色
 */
import { useMemo, useState, useEffect, useRef, useCallback } from "react";
import { CopilotChat } from "@copilotkit/react-ui";
import { useCoAgent, useCopilotContext, useCopilotMessagesContext, useLangGraphInterrupt } from "@copilotkit/react-core";
import { Network, FileText, Activity, Sparkles, Copy, Check, History } from "lucide-react";

import { BudgetPanel } from "@/features/diagnosis/BudgetPanel";
import { EvidenceChainGraph } from "@/features/diagnosis/EvidenceChainGraph";
import { ReportPanel } from "@/features/diagnosis/ReportPanel";
import { GuidanceCard, type HitlPayload } from "@/features/diagnosis/GuidanceCard";
import { HistoryPanel } from "@/features/diagnosis/HistoryPanel";
import { parseAgentState, type RawAgentState } from "@/features/diagnosis/parseAgentState";
import { apiFetch } from "@/api/client";
import type { BudgetState, BudgetTick } from "@/api/types";

type Tab = "budget" | "graph" | "report" | "history";

interface AgentState extends RawAgentState {
  budget?: BudgetState | null;
  budget_ticks?: BudgetTick[];
}

// ── Follow-up prompts ─────────────────────────────────────────────
const FOLLOWUP_PROMPTS = [
  "能帮我深入分析这个根因吗？",
  "还有其他可能的原因吗？",
  "帮我检查一下相关的代码逻辑",
];

export default function DiagnosePage() {
  const { state, running } = useCoAgent<AgentState>({ name: "default" });
  const { messages: chatMessages } = useCopilotMessagesContext();
  const { threadId } = useCopilotContext();

  // §8.1 path 2: backend thread_id for case-level feedback. Same id source as
  // 👍/👎 below -- state.case_id is the backend LangGraph thread_id (execute
  // injected); useCopilotContext().threadId is out of sync -> 404.
  const runId = (state?.case_id as string | undefined) || threadId;

  // 👍/👎 -> §8.1 feedback loop: CopilotKit 的 thumbs 按钮默认只更新本地高亮
  // 状态、不发请求,这里转发到后端 /api/feedback/{id}/{upvote,downvote}
  // (索引新 case + 回填召回 case 的 effectiveness)。
  // 关键:用 state.case_id(后端 langgraph thread_id,execute 注入),不用
  // useCopilotContext().threadId -- 后者与后端 thread_id 不同步,用它会 404。
  const onThumbsUp = async () => {
    const id = (state?.case_id as string | undefined) || threadId;
    console.log("[feedback] 👍 clicked, case_id=", state?.case_id, "threadId=", threadId, "-> use", id);
    if (!id) {
      console.warn("[feedback] no id -> skip upvote");
      return;
    }
    try {
      await apiFetch(`/api/feedback/${id}/upvote`, { method: "POST" });
      console.log("[feedback] upvote POST sent for", id);
    } catch (err) {
      console.error("[feedback] upvote failed", err);
    }
  };
  const onThumbsDown = async () => {
    const id = (state?.case_id as string | undefined) || threadId;
    console.log("[feedback] 👎 clicked, case_id=", state?.case_id, "threadId=", threadId, "-> use", id);
    if (!id) {
      console.warn("[feedback] no id -> skip downvote");
      return;
    }
    try {
      await apiFetch(`/api/feedback/${id}/downvote`, { method: "POST" });
      console.log("[feedback] downvote POST sent for", id);
    } catch (err) {
      console.error("[feedback] downvote failed", err);
    }
  };

  const { report, findings, evidence } = useMemo(
    () => parseAgentState(state, chatMessages),
    [state, chatMessages],
  );

  // #5 F1+F2: HITL 暂停标志。收到 hitl_guidance_request -> 置 true(状态灯变
  // "等待引导" + 抑制续问卡);resolve 包装清除,新 run 启动(running=true)时
  // 由下方 effect 清除。
  const [hitlPending, setHitlPending] = useState(false);

  // #5 F1: HITL 引导卡(调查中)。图在 human_input 暂停时由 useLangGraphInterrupt
  // 渲染进 <CopilotChat>。续查/采纳当前 -> CopilotKit resolve(value) ->
  // copilotkit.runAgent({forwardedProps:{command:{resume:value}}}) ->
  // POST /api/copilotkit/agent/default/run。调查 command.resume 为何不到后端:
  // 详见 docs/frontend-hitl-plan.md §9.3,后端断点见 .venv/.../ag_ui_langgraph/agent.py prepare_stream。
  useLangGraphInterrupt<HitlPayload>({
    enabled: ({ eventValue }) => eventValue?.type === "hitl_guidance_request",
    handler: () => {
      setHitlPending(true);
    },
    render: ({ event, resolve }) => (
      <GuidanceCard
        payload={event.value}
        findings={findings}
        onResolve={async (v) => {
          setHitlPending(false);
          resolve(v);
        }}
      />
    ),
  });

  const latestTick = state.budget_ticks?.at(-1) ?? null;
  const budget = state.budget ?? null;
  const isRunning = (latestTick?.model_call_count ?? 0) > 0 && !report;

  // HITL 暂停在续查/采纳后,或新诊断 run 启动时清除。
  useEffect(() => {
    if (running) setHitlPending(false);
  }, [running]);

  const [tab, setTab] = useState<Tab>("graph");
  const [highlightedRef, setHighlightedRef] = useState<string | null>(null);
  const [reportSeen, setReportSeen] = useState(false);
  const [dotPulsing, setDotPulsing] = useState(false); // blue dot pulse-once
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const hadReportRef = useRef(false);

  // New report → blue dot pulse once then static
  useEffect(() => {
    if (report && !hadReportRef.current) {
      hadReportRef.current = true;
      setReportSeen(false);
      setDotPulsing(true);
      const t = setTimeout(() => setDotPulsing(false), 1200);
      return () => clearTimeout(t);
    }
    if (!report) {
      hadReportRef.current = false;
      setReportSeen(true);
      setDotPulsing(false);
    }
    // Auto-switch to budget while running
    if (isRunning && !report) {
      setTab("budget");
    }
  }, [report, isRunning]);

  const handleTabChange = (newTab: Tab) => {
    if (newTab === "report" && report) {
      setReportSeen(true);
      setDotPulsing(false);
    }
    setTab(newTab);
  };

  const handleCopyPrompt = useCallback(async (text: string, idx: number) => {
    await navigator.clipboard.writeText(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 1500);
  }, []);

  const highlightedRefs = useMemo(
    () => (highlightedRef ? new Set([highlightedRef]) : new Set<string>()),
    [highlightedRef],
  );

  const handleHighlightRef = (ref: string) => {
    setHighlightedRef(ref);
    setTab("graph");
  };

  // 状态灯 5 态(永不绿色):amber-pulse=HITL 等待引导;blue=收敛完成;
  // amber-static=early_stopped 完成;cyan=分析中;grey=就绪。
  const dotCls = hitlPending
    ? "bg-amber-400 animate-breathe"
    : report
      ? report.early_stopped
        ? "bg-amber-400 shadow-[0_0_6px_rgba(245,158,11,0.5)]"
        : "bg-blue-400 shadow-[0_0_6px_rgba(59,130,246,0.5)]"
      : running
        ? "bg-cyan-400 animate-breathe"
        : "bg-[#5c6070]";
  const dotTitle = hitlPending
    ? "等待人工引导…"
    : report
      ? report.early_stopped
        ? "已达最佳努力结论(预算耗尽)"
        : "诊断完成"
      : running
        ? "分析中…"
        : "就绪";

  return (
    <div className="flex h-full gap-0">
      {/* ── Left: Chat area (always visible, always active) ────── */}
      <div className="flex flex-1 flex-col min-w-0 border-r border-white/[0.06]">
        <CopilotChat
          labels={{
            title: "🔬 DiagDoctor 协同诊断",
            initial:
              "你好！请描述你遇到的 Bug：\n\n" +
              "- 什么操作触发了错误？\n" +
              "- 有没有错误日志或 Trace ID？\n" +
              "- 浏览器 Console 有报错吗？\n\n" +
              "我会自动查询可观测性数据来帮你定位根因。",
            placeholder: "描述 Bug 现象，或粘贴错误日志 / Trace ID ...",
          }}
          onThumbsUp={onThumbsUp}
          onThumbsDown={onThumbsDown}
          className="h-full"
        />
      </div>

      {/* ── Right: Tabbed panel (420px per design doc) ─────────── */}
      <div className="relative flex w-[420px] shrink-0 flex-col bg-[#0f1117]">
        {/* Capsule tab bar */}
        <div className="flex shrink-0 items-center gap-1 border-b border-white/[0.06] px-3 py-2">
          {/* Status dot: grey → cyan pulse → blue static, NEVER green */}
          <span
            className={`mr-1.5 size-2 shrink-0 rounded-full transition-all duration-500 ${dotCls}`}
            title={dotTitle}
          />

          {/* Capsule tabs: 进度 | 证据链 | 初步分析 */}
          <div className="flex flex-1 items-center rounded-lg bg-white/[0.03] p-0.5">
            <CapsuleTab
              active={tab === "budget"}
              onClick={() => handleTabChange("budget")}
              icon={<Activity className="size-3.5" />}
              label="进度"
              pulse={isRunning}
            />
            <CapsuleTab
              active={tab === "graph"}
              onClick={() => handleTabChange("graph")}
              icon={<Network className="size-3.5" />}
              label="证据链"
            />
            <CapsuleTab
              active={tab === "report"}
              onClick={() => handleTabChange("report")}
              icon={<FileText className="size-3.5" />}
              label="初步分析"
              dotBadge={report && !reportSeen}
              dotPulsing={dotPulsing}
            />
            <CapsuleTab
              active={tab === "history"}
              onClick={() => handleTabChange("history")}
              icon={<History className="size-3.5" />}
              label="历史"
            />
          </div>
        </div>

        {/* Tab content */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {tab === "graph" && (
            <div key="graph" className="flex flex-1 flex-col animate-fade-in-up">
              <EvidenceChainGraph
                evidence={evidence}
                findings={findings}
                report={report}
                highlightedRefs={highlightedRefs}
              />
            </div>
          )}
          {tab === "report" && (
            <div key="report" className="flex flex-1 flex-col overflow-y-auto animate-fade-in-up">
              {report ? (
                <ReportPanel
                  report={report}
                  onHighlightRef={handleHighlightRef}
                  highlightedRef={highlightedRef}
                  runId={runId}
                  similarCasesText={state?.similar_cases_text}
                />
              ) : (
                <EmptyState
                  icon={<Sparkles className="size-8 text-[#5c6070]" />}
                  text="诊断完成后，初步分析将显示在这里"
                  sub="AI 正在分析日志、Trace 与信号关联…"
                />
              )}
            </div>
          )}
          {tab === "budget" && (
            <div key="budget" className="flex flex-1 flex-col overflow-y-auto animate-fade-in-up">
              <BudgetPanel tick={latestTick} budget={budget} />
            </div>
          )}
          {tab === "history" && (
            <div key="history" className="flex flex-1 flex-col animate-fade-in-up">
              <HistoryPanel onResumed={() => setTab("graph")} />
            </div>
          )}
        </div>

        {/* F2: 诊断完成续问卡(收敛 / early_stopped 完成;HITL 暂停时隐藏) */}
        {report && !hitlPending && (
          <div className="shrink-0 border-t border-white/[0.06] p-2">
            <div
              className={`mb-1.5 flex items-center gap-1.5 px-1 text-[10px] font-medium uppercase tracking-wider ${
                report.early_stopped ? "text-amber-400" : "text-blue-400"
              }`}
            >
              <Check className="size-3" />
              {report.early_stopped ? "已达最佳努力结论 · 可继续提问" : "诊断完成 · 可继续提问"}
            </div>
            <FollowUpCard
              prompts={FOLLOWUP_PROMPTS}
              copiedIdx={copiedIdx}
              onCopy={handleCopyPrompt}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════
// Sub-components
// ═══════════════════════════════════════════════════════════════════

/** Floating follow-up card in chat area */
function FollowUpCard({
  prompts,
  copiedIdx,
  onCopy,
}: {
  prompts: string[];
  copiedIdx: number | null;
  onCopy: (text: string, idx: number) => void;
}) {
  return (
    <div className="rounded-xl border border-dashed border-cyan-500/25 bg-[#0f1117]/90 backdrop-blur-xl p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium text-cyan-400 uppercase tracking-wider">
        <Sparkles className="size-3" />
        继续深入？
      </div>
      <div className="flex flex-col gap-1">
        {prompts.map((prompt, i) => (
          <button
            key={i}
            type="button"
            onClick={() => onCopy(prompt, i)}
            className="group flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[11px] text-[#8a8fa3] transition-all hover:bg-white/[0.04] hover:text-[#e4e4ef]"
          >
            <span className="flex-1 truncate">{prompt}</span>
            <span className="shrink-0 text-[#5c6070] group-hover:text-cyan-400 transition-colors">
              {copiedIdx === i ? (
                <Check className="size-3 text-green-400" />
              ) : (
                <Copy className="size-3" />
              )}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Capsule tab — supports blue dot badge for unseen report */
function CapsuleTab({
  active,
  onClick,
  icon,
  label,
  pulse,
  dotBadge,
  dotPulsing,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  pulse?: boolean;
  dotBadge?: boolean;
  dotPulsing?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-medium transition-all duration-200 ${
        active
          ? "bg-white/[0.08] text-[#e4e4ef] shadow-sm"
          : "text-[#5c6070] hover:text-[#8a8fa3]"
      }`}
    >
      <span className={pulse && active ? "animate-pulse-soft" : ""}>{icon}</span>
      {label}
      {/* Blue dot badge for unseen report — 8px, pulse once then static */}
      {dotBadge && (
        <span
          className={`ml-0.5 size-2 shrink-0 rounded-full bg-blue-400 shadow-[0_0_6px_rgba(59,130,246,0.6)] ${
            dotPulsing ? "animate-pulse-soft" : ""
          }`}
        />
      )}
    </button>
  );
}

/** Empty state with icon */
function EmptyState({
  icon,
  text,
  sub,
}: {
  icon?: React.ReactNode;
  text: string;
  sub?: string;
}) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
      {icon && <div className="opacity-40">{icon}</div>}
      <p className="text-sm text-[#8a8fa3]">{text}</p>
      {sub && <p className="text-xs text-[#5c6070]">{sub}</p>}
    </div>
  );
}
