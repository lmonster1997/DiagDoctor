/**
 * DiagnosePage - "协同诊断室" 主诊断页
 *
 * §1 设计约束：
 *   - CopilotChat 永远可见且可输入（左侧 flex-1，始终挂载）
 *   - 右侧 420px 面板：初步分析 | 历史
 *   - 新报告 -> "初步分析" 标签出现蓝色圆点徽章（脉冲一次后静止），不自动跳转
 *   - 状态指示灯：灰->青脉动(分析中)->蓝静止(有初步分析)，永不绿色
 *
 * CopilotKit v2 迁移说明（from v1）:
 *   - useCoAgent -> useAgent({agentId, updates[]})；state=agent.state、running=agent.isRunning
 *   - useCopilotMessagesContext().messages -> agent.messages（v2 即 <CopilotChat> 渲染源）
 *   - useCopilotChatHeadless_c().sendMessage -> agent.addMessage + copilotkit.runAgent
 *   - useCopilotContext().{threadId,setThreadId} -> 本地 threadId state + <CopilotChat threadId>
 *   - useLangGraphInterrupt -> useInterrupt（v2 event.value 是 ag_ui_langgraph dump_json_safe
 *     的 JSON 字符串，需 parseInterruptValue 解析；v1 useLangGraphInterrupt 内部即调 useInterrupt）
 *   - in-chat 历史回放：v1 setMessages 走错 store（聊天空），v2 agent.messages 即渲染源 ->
 *     setThreadId(tid) + getThreadMessages + agent.setMessages 回填（GATE C）。
 */
import { useMemo, useState, useEffect, useRef, useCallback } from "react";
import {
  CopilotChat,
  useAgent,
  useInterrupt,
  useCopilotKit,
  UseAgentUpdate,
} from "@copilotkit/react-core/v2";
import { FileText, Sparkles, Check, History, Send } from "lucide-react";

import { ReportPanel } from "@/features/diagnosis/ReportPanel";
import { DiagMarkdownRenderer } from "@/features/diagnosis/DiagAssistantMessage";
import { GuidanceCard, type HitlPayload } from "@/features/diagnosis/GuidanceCard";
import { HistoryPanel } from "@/features/diagnosis/HistoryPanel";
import { parseAgentState, type RawAgentState } from "@/features/diagnosis/parseAgentState";
import { apiFetch, getThreadMessages } from "@/api/client";

type Tab = "report" | "history";

type AgentState = RawAgentState;

// ── Follow-up prompts ─────────────────────────────────────────────
const FOLLOWUP_PROMPTS = [
  "能帮我深入分析这个根因吗？",
  "还有其他可能的原因吗？",
  "帮我检查一下相关的代码逻辑",
];

/**
 * v2 useInterrupt 的 event.value 是 ag_ui_langgraph emit 的 CustomEvent value，
 * 后端用 dump_json_safe(interrupt.value) 序列化成 JSON **字符串**。v1 的
 * useLangGraphInterrupt 内部 toV1Event 会 JSON.parse 回对象；v2 原生 useInterrupt
 * 不 parse，这里手动还原成 HitlPayload 对象（否则 event.value?.type 恒 undefined）。
 */
function parseInterruptValue(value: unknown): HitlPayload | null {
  if (value == null) return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value) as HitlPayload;
    } catch {
      return null;
    }
  }
  return value as HitlPayload;
}

export default function DiagnosePage() {
  // v2: useAgent 取共享 agent（与 <CopilotChat agentId="default"> 同一实例）。
  // updates[] opt-in 反应式：必须含 OnStateChanged 否则 ReportPanel 不随 state 更新。
  const { agent } = useAgent({
    agentId: "default",
    updates: [
      UseAgentUpdate.OnStateChanged,
      UseAgentUpdate.OnRunStatusChanged,
      UseAgentUpdate.OnMessagesChanged,
    ],
  });
  const state = agent.state as AgentState;
  const running = agent.isRunning;
  const chatMessages = agent.messages;
  const { copilotkit } = useCopilotKit();

  const round = (state?.round as number | undefined) ?? 1;
  const roundsExhausted = Boolean(state?.rounds_exhausted);
  // tab state 上移到此(handleFollowup 需用 setTab,避免 TDZ)。
  const [tab, setTab] = useState<Tab>("report");

  // v2: threadId 改本地 state 控制(v2 不导出 useCopilotContext)。undefined=新对话
  // (CopilotChat 自 mint threadId)；置某 tid=切到该历史线程(hasExplicitThreadId=true,
  // 抑制 welcome + 不触发 v1 的切线程清空 effect)。setThreadId 由历史面板回填触发。
  const [threadId, setThreadId] = useState<string | undefined>(undefined);

  // P2 历史 case「追加诊断」/ 暂停 case「切换恢复」:切到该线程 + 回填历史消息。
  // v2: agent.messages 即 <CopilotChat> 渲染源 -> setMessages 回填可见(GATE C)。
  // 回填放 useEffect[threadId] 统一处理(followup + resume 共用)。
  const handleFollowup = useCallback(
    (tid: string) => {
      console.log("[followup] 追加诊断 clicked", { tid, currentThreadId: threadId });
      setThreadId(tid);
      setTab("report");
    },
    [threadId],
  );
  const handleResume = useCallback(
    (tid: string) => {
      console.log("[resume] 切换恢复 clicked", { tid, currentThreadId: threadId });
      setThreadId(tid);
      setTab("report");
    },
    [threadId],
  );

  // GATE C: threadId 变更(切历史线程)后,从后端 /threads/{tid}/messages 拉历史
  // AG-UI 消息回填到 agent.messages。v1 这步走错 store 聊天空,v2 走 agent.setMessages
  // 即 <CopilotChat> 渲染源 -> 历史对话浮现。无后端 Runner,数据源复用现有端点。
  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    getThreadMessages(threadId)
      .then((resp) => {
        if (cancelled) return;
        agent.setMessages(
          resp.messages as unknown as Parameters<typeof agent.setMessages>[0],
        );
      })
      .catch((err) =>
        console.error("[followup] history backfill failed", err),
      );
    return () => {
      cancelled = true;
    };
  }, [threadId, agent]);

  // §8.1 path 2: backend thread_id for case-level feedback. Same id source as
  // 👍/👎 below -- state.case_id is the backend LangGraph thread_id (execute
  // injected); CopilotKit threadId 与后端不同步(且 v2 本地 threadId 仅历史切换时
  // 有值) -> 优先 state.case_id。
  const runId = (state?.case_id as string | undefined) || threadId;

  // 👍/👎 -> §8.1 feedback loop: 转发到后端 /api/feedback/{id}/{upvote,downvote}
  // (索引新 case + 回填召回 case 的 effectiveness)。
  // 关键:用 state.case_id(后端 langgraph thread_id,execute 注入)。
  const onThumbsUp = async (message: unknown) => {
    const id = (state?.case_id as string | undefined) || threadId;
    console.log("[feedback] 👍 clicked, case_id=", state?.case_id, "threadId=", threadId, "-> use", id);
    void message;
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
  const onThumbsDown = async (message: unknown) => {
    const id = (state?.case_id as string | undefined) || threadId;
    console.log("[feedback] 👎 clicked, case_id=", state?.case_id, "threadId=", threadId, "-> use", id);
    void message;
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

  const { report, findings } = useMemo(
    () => parseAgentState(state, chatMessages as never),
    [state, chatMessages],
  );

  // #5 F1+F2 / P1: HITL 暂停标志。收到 hitl_guidance_request(预算耗尽)或
  // clarify(agent 主动提问)-> 置 true(状态灯变"等待引导" + 抑制续问卡);resolve
  // 包装清除,新 run 启动(running=true)时由下方 effect 清除。
  const [hitlPending, setHitlPending] = useState(false);

  // #5 F1 / P1: HITL 引导卡(调查中)。图在 human_input(#5 预算耗尽)或
  // clarify_input(P1 agent 主动提问)暂停时由 useInterrupt 渲染进 <CopilotChat>。
  // v2: useInterrupt 捕 ag_ui_langgraph 的 on_interrupt CustomEvent(与 v1
  // useLangGraphInterrupt 同路径,后者内部即调 useInterrupt);resolve 走 legacy
  // forwardedProps.command.resume(与后端 #5 F1 run_endpoint + prepare_stream 同协议)。
  // 注意:不传泛型(第一个泛型是 TResult 不是 TValue,传了会强制 handler 返回类型)。
  useInterrupt({
    enabled: (event) => {
      const v = parseInterruptValue(event.value);
      return v?.type === "hitl_guidance_request" || v?.type === "clarify";
    },
    handler: () => {
      setHitlPending(true);
    },
    render: ({ event, resolve }) => {
      const payload = parseInterruptValue(event.value);
      if (!payload) return <></>;
      return (
        <GuidanceCard
          payload={payload}
          findings={findings}
          onResolve={async (v) => {
            setHitlPending(false);
            resolve(v);
          }}
        />
      );
    },
  });

  // HITL 暂停在续查/采纳后,或新诊断 run 启动时清除。
  useEffect(() => {
    if (running) setHitlPending(false);
  }, [running]);

  const [reportSeen, setReportSeen] = useState(false);
  const [dotPulsing, setDotPulsing] = useState(false); // blue dot pulse-once
  const hadReportRef = useRef(false);

  // New report -> blue dot pulse once then static
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
  }, [report]);

  const handleTabChange = (newTab: Tab) => {
    if (newTab === "report" && report) {
      setReportSeen(true);
      setDotPulsing(false);
    }
    setTab(newTab);
  };

  // v2: 程序化发送 follow-up = agent.addMessage(userMsg) + copilotkit.runAgent({agent})
  // (v1 用 useCopilotChatHeadless_c().sendMessage;v2 无此 hook,AbstractAgent 也无
  // sendMessage 方法,按 v2 CopilotChat 内部 submit 流程组合)。
  // P2: 发送 follow-up -> 后端 bug_info 检测复诊轮(round+1 + 重置 round-scoped
  // flag)+ _diagnosis_agent_node 注入上轮 scratchpad(知情修订,非盲查)。
  const handleSendFollowup = useCallback(
    async (text: string) => {
      agent.addMessage({
        id: crypto.randomUUID(),
        role: "user",
        content: text,
      });
      await copilotkit.runAgent({ agent });
    },
    [agent, copilotkit],
  );

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
          agentId="default"
          threadId={threadId}
          labels={{
            modalHeaderTitle: "🔬 DiagDoctor 协同诊断",
            welcomeMessageText:
              "你好！请描述你遇到的 Bug：\n\n" +
              "- 什么操作触发了错误？\n" +
              "- 有没有错误日志或 Trace ID？\n" +
              "- 浏览器 Console 有报错吗？\n\n" +
              "我会自动查询可观测性数据来帮你定位根因。",
            chatInputPlaceholder: "描述 Bug 现象，或粘贴错误日志 / Trace ID ...",
          }}
          className="h-full"
          // v2: AssistantMessage 顶层 prop 已删,改深层 slot。用 Partial 形式只覆盖
          // markdownRenderer 子 slot(报告 JSON->卡片)+ onThumbsUp/onThumbsDown
          // (转发 /api/feedback),保留默认 CopilotChatAssistantMessage 结构。
          chatView={{
            messageView: {
              assistantMessage: {
                markdownRenderer: DiagMarkdownRenderer,
                onThumbsUp,
                onThumbsDown,
              },
            },
          }}
        />
      </div>

      {/* ── Right: Tabbed panel (420px per design doc) ─────────── */}
      <div className="relative flex w-[420px] shrink-0 flex-col bg-[#0f1117]">
        {/* Capsule tab bar */}
        <div className="flex shrink-0 items-center gap-1 border-b border-white/[0.06] px-3 py-2">
          {/* Status dot: grey -> cyan pulse -> blue static, NEVER green */}
          <span
            className={`mr-1.5 size-2 shrink-0 rounded-full transition-all duration-500 ${dotCls}`}
            title={dotTitle}
          />

          {/* Capsule tabs: 初步分析 | 历史 */}
          <div className="flex flex-1 items-center rounded-lg bg-white/[0.03] p-0.5">
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
          {tab === "report" && (
            <div key="report" className="flex flex-1 flex-col overflow-y-auto animate-fade-in-up">
              {report ? (
                <ReportPanel
                  report={report}
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
          {tab === "history" && (
            <div key="history" className="flex flex-1 flex-col animate-fade-in-up">
              <HistoryPanel
                onResumed={() => setTab("report")}
                onFollowup={handleFollowup}
                onResume={handleResume}
              />
            </div>
          )}
        </div>

        {/* F2 / P2: 诊断完成续问卡(收敛 / early_stopped 完成;HITL 暂停或复诊上限时隐藏) */}
        {report && !hitlPending && (
          <div className="shrink-0 border-t border-white/[0.06] p-2">
            <div
              className={`mb-1.5 flex items-center gap-1.5 px-1 text-[10px] font-medium uppercase tracking-wider ${
                report.early_stopped ? "text-amber-400" : "text-blue-400"
              }`}
            >
              <Check className="size-3" />
              {roundsExhausted
                ? `已达复诊上限 · 第 ${round} 轮`
                : report.early_stopped
                  ? "已达最佳努力结论 · 可继续提问"
                  : round > 1
                    ? `第 ${round} 轮复诊 · 可继续追加`
                    : "诊断完成 · 可继续提问"}
            </div>
            {!roundsExhausted && (
              <FollowUpCard prompts={FOLLOWUP_PROMPTS} onSend={handleSendFollowup} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════
// Sub-components
// ═══════════════════════════════════════════════════════════════════

/** Floating follow-up card in chat area - P2: click sends (triggers a 复诊 round). */
function FollowUpCard({
  prompts,
  onSend,
}: {
  prompts: string[];
  onSend: (text: string) => void;
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
            onClick={() => onSend(prompt)}
            className="group flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[11px] text-[#8a8fa3] transition-all hover:bg-white/[0.04] hover:text-[#e4e4ef]"
          >
            <span className="flex-1 truncate">{prompt}</span>
            <span className="shrink-0 text-[#5c6070] group-hover:text-cyan-400 transition-colors">
              <Send className="size-3" />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Capsule tab - supports blue dot badge for unseen report */
function CapsuleTab({
  active,
  onClick,
  icon,
  label,
  dotBadge,
  dotPulsing,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
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
      <span>{icon}</span>
      {label}
      {/* Blue dot badge for unseen report - 8px, pulse once then static */}
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
