/**
 * GuidanceCard - HITL 引导卡 (#5 预算耗尽 + P1 主动澄清,调查中).
 *
 * 当诊断图在 ``human_input``(#5 预算耗尽)或 ``clarify_input``(P1 agent 主动
 * 提问)节点暂停时,由 ``useLangGraphInterrupt`` 渲染进 ``<CopilotChat>``。
 *
 * #5 预算耗尽(payload.type === "hitl_guidance_request"):
 *   -「续查」: onResolve(guidanceText) -> resolve -> Command(resume=text) ->
 *              diagnosis_agent 知情二次调查(全新 ReAct + 新预算)。
 *   -「采纳当前」: onResolve("") -> Command(resume="") -> END,保留 early_stopped 报告。
 *
 * P1 主动澄清(payload.type === "clarify",agent 主动问了用户一个问题):
 *   -「回答」: onResolve(answerText) -> resolve -> Command(resume=answer) ->
 *              diagnosis_agent 澄清续查(问题+回答注入,全新 ReAct + 新预算)。
 *   -「跳过」: onResolve("") -> Command(resume="") -> END,采纳当前 best-effort。
 *
 * ⚠️ 调查中:1.62.2 + ag_ui_langgraph 下 resolve 的 command.resume 未到后端(无
 * human_input_resumed、卡片卡死)。详见 docs/frontend-hitl-plan.md §9.3。本卡暂时
 * 走 CopilotKit resolve 以便后端断点定位。
 *
 * budget HITL: payload.prompt 已内嵌 findings 摘要(后端拼 prior_summary[:500])。
 * clarify: payload.question 是 agent 主动提的问题。state.findings 在此作结构化补充展示。
 */
import { useState } from "react";
import { AlertTriangle, Send, Check, HelpCircle } from "lucide-react";
import type { Finding } from "@/api/types";

/** interrupt() payload emitted by human_input_node (#5) or clarify_input_node (P1). */
export interface HitlPayload {
  type: string;
  /** #5 budget HITL: the prompt with prior-findings summary. */
  prompt?: string;
  /** P1 clarify: the agent's question to the user. */
  question?: string;
  prior_findings_count: number;
  early_stopped: boolean;
}

interface GuidanceCardProps {
  payload: HitlPayload;
  findings: Finding[];
  /** 非空字符串 -> 续查/回答;空串 -> 采纳当前/跳过(接受当前 best-effort)。
   *  promise 成功 -> 外部卸载卡片;失败 -> 恢复可点供重试。 */
  onResolve: (value: string) => Promise<void>;
}

export function GuidanceCard({ payload, findings, onResolve }: GuidanceCardProps) {
  const [text, setText] = useState("");
  const [resolving, setResolving] = useState(false);

  const isClarify = payload.type === "clarify";
  const message = isClarify ? payload.question : payload.prompt;
  const title = isClarify ? "需要澄清" : "需要人工引导";
  const placeholder = isClarify
    ? "回答 agent 的问题…"
    : "可疑方向 / 已知线索,如:疑似连接池耗尽、留意 X 服务的超时…";
  const primaryLabel = isClarify ? "回答" : "续查";
  const secondaryLabel = isClarify ? "跳过" : "采纳当前";

  const guidance = text.trim();
  const topFindings = findings.map((f) => f.summary).filter(Boolean).slice(0, 3);

  const handleResolve = async (value: string) => {
    if (resolving) return;
    setResolving(true);
    try {
      await onResolve(value);
      // 成功:外部 cancel() 卸载卡片,无需复位。
    } catch (err) {
      console.error("[GuidanceCard] resume 失败", err);
      setResolving(false); // 失败:恢复可点供重试
    }
  };

  return (
    <div className="my-2 rounded-xl border border-amber-500/30 bg-[#0f1117]/95 backdrop-blur-xl p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium text-amber-400 uppercase tracking-wider">
        {isClarify ? <HelpCircle className="size-3" /> : <AlertTriangle className="size-3" />}
        {title}
      </div>

      <p className="mb-2 text-xs text-[#c7cad6] leading-relaxed">{message}</p>

      {topFindings.length > 0 && (
        <ul className="mb-2 space-y-0.5">
          {topFindings.map((s, i) => (
            <li
              key={i}
              className="flex items-start gap-1.5 text-[11px] text-[#8a8fa3] leading-snug"
            >
              <span className="mt-0.5 size-1 shrink-0 rounded-full bg-amber-400/60" />
              <span className="line-clamp-2">{s}</span>
            </li>
          ))}
        </ul>
      )}

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        rows={2}
        disabled={resolving}
        className="w-full resize-none rounded-md border border-white/[0.06] bg-black/30 px-2.5 py-1.5 text-xs text-[#e4e4ef] placeholder:text-[#5c6070] focus:outline-none focus:border-amber-500/40 disabled:opacity-50"
      />

      <div className="mt-2 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={() => handleResolve("")}
          disabled={resolving}
          className="flex items-center gap-1 rounded-md border border-white/[0.08] bg-white/[0.03] px-2.5 py-1.5 text-[11px] text-[#8a8fa3] transition-all hover:bg-white/[0.06] hover:text-[#e4e4ef] disabled:opacity-50"
        >
          <Check className="size-3" />
          {secondaryLabel}
        </button>
        <button
          type="button"
          onClick={() => handleResolve(guidance)}
          disabled={resolving || !guidance}
          className="flex items-center gap-1 rounded-md bg-amber-500/90 px-2.5 py-1.5 text-[11px] font-medium text-[#1a1a1a] transition-all hover:bg-amber-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Send className="size-3" />
          {primaryLabel}
        </button>
      </div>
    </div>
  );
}
