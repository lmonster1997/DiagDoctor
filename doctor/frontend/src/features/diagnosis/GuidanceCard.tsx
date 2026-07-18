/**
 * GuidanceCard - HITL 引导卡 (#5 F1,调查中).
 *
 * 当诊断图在 ``human_input`` 节点暂停(预算耗尽、未收敛)时,由
 * ``useLangGraphInterrupt`` 渲染进 ``<CopilotChat>``。操作者二选一:
 *   -「续查」: onResolve(guidanceText) -> CopilotKit resolve -> copilotKit.runAgent
 *              ({forwardedProps:{command:{resume:text}}}) -> 后端 Command(resume=text)
 *              -> diagnosis_agent 知情二次调查(全新 ReAct + 新预算)。
 *   -「采纳当前」: onResolve("") -> Command(resume="") -> END,保留当前 early_stopped 报告。
 *
 * ⚠️ 调查中:1.62.2 + ag_ui_langgraph 下 resolve 的 command.resume 未到后端(无
 * human_input_resumed、卡片卡死)。详见 docs/frontend-hitl-plan.md §9.3。本卡暂时
 * 走 CopilotKit resolve 以便后端断点定位。
 *
 * payload.prompt 已内嵌 findings 摘要(后端拼 prior_summary[:500]),
 * state.findings 在此作结构化补充展示。
 */
import { useState } from "react";
import { AlertTriangle, Send, Check } from "lucide-react";
import type { Finding } from "@/api/types";

/** interrupt() payload emitted by human_input_node. */
export interface HitlPayload {
  type: string;
  prompt: string;
  prior_findings_count: number;
  early_stopped: boolean;
}

interface GuidanceCardProps {
  payload: HitlPayload;
  findings: Finding[];
  /** 非空字符串 -> 续查;空串 -> 采纳当前 early_stopped 报告。
   *  promise 成功 -> 外部卸载卡片;失败 -> 恢复可点供重试。 */
  onResolve: (value: string) => Promise<void>;
}

export function GuidanceCard({ payload, findings, onResolve }: GuidanceCardProps) {
  const [text, setText] = useState("");
  const [resolving, setResolving] = useState(false);

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
        <AlertTriangle className="size-3" />
        需要人工引导
      </div>

      <p className="mb-2 text-xs text-[#c7cad6] leading-relaxed">{payload.prompt}</p>

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
        placeholder="可疑方向 / 已知线索,如:疑似连接池耗尽、留意 X 服务的超时…"
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
          采纳当前
        </button>
        <button
          type="button"
          onClick={() => handleResolve(guidance)}
          disabled={resolving || !guidance}
          className="flex items-center gap-1 rounded-md bg-amber-500/90 px-2.5 py-1.5 text-[11px] font-medium text-[#1a1a1a] transition-all hover:bg-amber-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Send className="size-3" />
          续查
        </button>
      </div>
    </div>
  );
}
