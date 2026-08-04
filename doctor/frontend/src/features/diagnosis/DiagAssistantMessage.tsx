/**
 * DiagAssistantMessage - 自定义 CopilotChat assistant 消息渲染(CopilotKit v2)。
 *
 * 为什么存在：诊断报告以 JSON 字符串存在于最终 AIMessage.content（后端
 * parse_diagnosis_report 与前端 parseAgentState 都依赖此契约，不能改）。
 * CopilotChat 默认把 content 当 markdown 文本渲染 -> 左侧聊天区出现一坨
 * 裸 JSON，很丑（forced_call 的 model_dump_json(indent=2) 尤其显眼）。
 *
 * v2 接入方式：v1 的 ``<CopilotChat AssistantMessage={...}>`` 顶层 prop 在 v2 已删。
 * v2 的 ``assistantMessage`` 是 ``CopilotChatMessageView`` 的一个 slot,且 SlotValue 允许
 * ``Partial<ComponentProps<C>>`` 形式 -- 只覆盖某个子 slot / prop、保留默认组件结构。
 * 这里只覆盖 ``markdownRenderer`` 子 slot(报告 JSON -> 卡片,其余委托默认 Streamdown)
 * + ``onThumbsUp``/``onThumbsDown`` prop(转发到 /api/feedback),不替换整个组件
 * (替换需带 CopilotChatAssistantMessage 的静态命名空间成员,脆且易雷)。
 *
 * 接入(在 DiagnosePage):
 *   ``<CopilotChat chatView={{ messageView: { assistantMessage: {
 *       markdownRenderer: DiagMarkdownRenderer,
 *       onThumbsUp, onThumbsDown,
 *   } } }} />``
 *
 * 取舍：左侧卡片只给结论（根因 + 类别 + 置信度），完整分析（修复建议 /
 * 证据链 / 历史参考）仍在右侧 ReportPanel -- 避免左右重复。👍/👎 保留,
 * 因为这是 run 级反馈入口（DiagnosePage 转发到 /api/feedback/{id}/
 * {up,down}vote），右侧只有 case 级反馈，丢了就没了反馈通道。v2 下 👍/👎
 * 走默认 CopilotChatAssistantMessage 工具栏(不再内嵌卡片),样式随 v2 默认。
 *
 * 拦截条件：extractJsonFromText 能从 content 提取出报告字段（root_cause /
 * primary_category / fix_suggestion）即拦截。含报告字段才拦截，避免误伤
 * finding/hypothesis 块或含 JSON 片段的推理文本。
 */
import { CopilotChatAssistantMessage } from "@copilotkit/react-core/v2";
import { FileText } from "lucide-react";
import { extractJsonFromText } from "./parseAgentState";

/** markdownRenderer slot 的 props 类型(从默认组件推断,避免直接引 Streamdown)。 */
type MarkdownRendererProps = React.ComponentProps<
  typeof CopilotChatAssistantMessage.MarkdownRenderer
>;

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

/** 类别 pill 配色（与 ReportPanel.categoryColor 一致）。 */
function categoryColor(cat: string): string {
  const lower = cat.toLowerCase();
  if (lower.includes("error") || lower.includes("crash")) return "#ef4444";
  if (lower.includes("perf") || lower.includes("slow")) return "#f59e0b";
  if (lower.includes("data") || lower.includes("config")) return "#a855f7";
  return "#3b82f6";
}

/**
 * ``markdownRenderer`` slot:content 含诊断报告 JSON -> 渲染简洁卡片；
 * 否则委托回默认 ``CopilotChatAssistantMessage.MarkdownRenderer``(Streamdown)。
 */
export function DiagMarkdownRenderer(props: MarkdownRendererProps) {
  const content = props.content ?? "";
  const report = extractReport(content);

  if (!report) {
    // 非报告消息 -> 默认 markdown 渲染(markdown + 代码块 + 完整 controls)
    return <CopilotChatAssistantMessage.MarkdownRenderer {...props} />;
  }

  const rootCause = String(report.root_cause ?? "（未识别）");
  const primaryCategory = String(report.primary_category ?? "未分类");
  const confidence = Math.round(
    Math.max(0, Math.min(1, Number(report.confidence ?? 0.5))) * 100,
  );
  const catColor = categoryColor(primaryCategory);

  return (
    <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
      {/* 标题 + 置信度 */}
      <div className="mb-2 flex items-center gap-1.5">
        <FileText className="size-3.5 text-[#3b82f6]" />
        <span className="text-[11px] font-medium text-[#3b82f6]">诊断报告</span>
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
  );
}
