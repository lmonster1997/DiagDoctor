/**
 * DiagAssistantMessage - 自定义 CopilotChat assistant 消息渲染。
 *
 * 为什么存在：诊断报告以 JSON 字符串存在于最终 AIMessage.content（后端
 * parse_diagnosis_report 与前端 parseAgentState 都依赖此契约，不能改）。
 * CopilotChat 默认把 content 当 markdown 文本渲染 -> 左侧聊天区出现一坨
 * 裸 JSON，很丑（forced_call 的 model_dump_json(indent=2) 尤其显眼）。
 *
 * 这里拦截：content 含诊断报告 JSON 时，渲染简洁卡片；其余消息委托回
 * 默认 AssistantMessage，保持 markdown 渲染与 controls 不变。
 *
 * 取舍：左侧卡片只给结论（根因 + 类别 + 置信度），完整分析（修复建议 /
 * 证据链 / 历史参考）仍在右侧 ReportPanel -- 避免左右重复。👍/👎 保留，
 * 因为这是 run 级反馈入口（DiagnosePage 转发到 /api/feedback/{id}/
 * {up,down}vote），右侧只有 case 级反馈，丢了就没了反馈通道。
 *
 * 拦截条件：extractJsonFromText 能从 content 提取出报告字段（root_cause /
 * primary_category / fix_suggestion）即拦截，支持纯 JSON / ```json fence``` /
 * 简要分析+JSON 三种形态（system prompt 允许 agent "先简要分析推理过程，
 * 最终结论以 JSON 给出"，所以正常结束的最终消息常带自然语言前缀或 fence，
 * 不能只认 `{` 开头）。含报告字段才拦截，避免误伤 finding/hypothesis 块
 * 或含 JSON 片段的推理文本。streaming 中未闭合的 JSON 解析失败 -> 走默认
 * 渲染（瞬时半截，forced_call 非流式一次性返回完整 JSON，不受影响）。
 */
import type { ComponentProps } from "react";
import { AssistantMessage as DefaultAssistantMessage } from "@copilotkit/react-ui";
import { FileText, ThumbsUp, ThumbsDown } from "lucide-react";
import { extractJsonFromText } from "./parseAgentState";

type AssistantMessageProps = ComponentProps<typeof DefaultAssistantMessage>;

/**
 * 从 content 提取诊断报告 JSON。支持纯 JSON / ```json fence``` / 简要分析
 * +JSON（extractJsonFromText 内部按 fence -> brace-depth -> whole 顺序尝试）。
 * 含报告字段（root_cause / primary_category / fix_suggestion）才返回，避免
 * 误伤 finding（summary/hypothesis）块或日志片段 JSON。
 */
function extractReport(
  content: string,
): Record<string, unknown> | null {
  const data = extractJsonFromText(content);
  if (!data) return null;
  if (
    "root_cause" in data ||
    "primary_category" in data ||
    "fix_suggestion" in data
  ) {
    return data;
  }
  return null;
}

/** CopilotKit message.content 可能是 string 或 content-block 数组，统一取文本。 */
function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((p) =>
        typeof p === "string" ? p : (p as { text?: string })?.text ?? "",
      )
      .join("\n");
  }
  return "";
}

/** 类别 pill 配色（与 ReportPanel.categoryColor 一致）。 */
function categoryColor(cat: string): string {
  const lower = cat.toLowerCase();
  if (lower.includes("error") || lower.includes("crash")) return "#ef4444";
  if (lower.includes("perf") || lower.includes("slow")) return "#f59e0b";
  if (lower.includes("data") || lower.includes("config")) return "#a855f7";
  return "#3b82f6";
}

export function DiagAssistantMessage(props: AssistantMessageProps) {
  const content = extractText(props.message?.content);
  const report = extractReport(content);

  // 非诊断报告 -> 委托默认渲染（markdown + 完整 controls）
  if (!report) {
    return <DefaultAssistantMessage {...props} />;
  }

  const rootCause = String(report.root_cause ?? "（未识别）");
  const primaryCategory = String(report.primary_category ?? "未分类");
  const confidence = Math.round(
    Math.max(0, Math.min(1, Number(report.confidence ?? 0.5))) * 100,
  );
  const catColor = categoryColor(primaryCategory);

  return (
    <div className="copilotKitMessage copilotKitAssistantMessage">
      <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
        {/* 标题 + 置信度 */}
        <div className="mb-2 flex items-center gap-1.5">
          <FileText className="size-3.5 text-[#3b82f6]" />
          <span className="text-[11px] font-medium text-[#3b82f6]">
            诊断报告
          </span>
          <span className="ml-auto text-[10px] tabular-nums text-[#5c6070]">
            置信度 {confidence}%
          </span>
        </div>

        {/* 根因（截断 3 行，完整内容在右侧） */}
        <p className="mb-2 line-clamp-3 text-sm leading-relaxed text-[#e4e4ef]">
          {rootCause}
        </p>

        {/* 类别 pill */}
        <span
          className="inline-flex items-center rounded-md px-2 py-0.5 text-[10px] font-medium"
          style={{ backgroundColor: `${catColor}18`, color: catColor }}
        >
          {primaryCategory}
        </span>

        {/* 引导到右侧详情 */}
        <p className="mt-2 text-[10px] text-[#5c6070]">
          完整分析见右侧「初步分析」
        </p>
      </div>

      {/* 👍/👎 run 级反馈：复用默认 controls 的 class 保持样式一致 */}
      {!props.isLoading && (props.onThumbsUp || props.onThumbsDown) && (
        <div className="copilotKitMessageControls">
          {props.onThumbsUp && (
            <button
              type="button"
              className={`copilotKitMessageControlButton ${
                props.feedback === "thumbsUp" ? "active" : ""
              }`}
              onClick={() => props.message && props.onThumbsUp?.(props.message)}
              aria-label="有帮助"
              title="有帮助"
            >
              <ThumbsUp className="size-3.5" />
            </button>
          )}
          {props.onThumbsDown && (
            <button
              type="button"
              className={`copilotKitMessageControlButton ${
                props.feedback === "thumbsDown" ? "active" : ""
              }`}
              onClick={() =>
                props.message && props.onThumbsDown?.(props.message)
              }
              aria-label="没帮助"
              title="没帮助"
            >
              <ThumbsDown className="size-3.5" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
