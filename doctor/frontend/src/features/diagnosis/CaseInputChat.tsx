/**
 * CaseInputChat — wraps CopilotChat with welcome message and suggestions.
 *
 * Injects suggested prompts into the CopilotChat so new users can try
 * pre-built scenarios (e.g. "try a backend 500 case").
 */

import { CopilotChat } from "@copilotkit/react-ui";

export function CaseInputChat() {
  return (
    <CopilotChat
      labels={{
        title: "🔬 DiagDoctor 诊断助手",
        initial:
          "你好！我是 DiagDoctor，一个 AI Bug 诊断助手。\n\n" +
          "请描述你遇到的 Bug：\n" +
          "• 什么操作触发了错误？\n" +
          "• 有没有错误日志或 Trace ID？\n" +
          "• 浏览器 Console 有报错吗？\n\n" +
          "我会自动查询可观测性数据（Loki 日志 + Tempo Trace）来帮你定位根因。",
        placeholder: "描述 Bug 现象，或粘贴错误日志 / Trace ID ...",
      }}
      suggestions={[
        {
          title: "后端 500 错误",
          message:
            "用户在下单时遇到 500 Internal Server Error。请帮我诊断：\n" +
            "错误日志: 2025-01-15T10:23:45 ERROR app.order_service - Order creation failed: division by zero\n" +
            "Trace ID: abc123def456",
        },
        {
          title: "前端页面崩溃",
          message:
            "用户在商品列表页点击「加载更多」后页面白屏。\n" +
            "Console 报错: Uncaught TypeError: Cannot read properties of undefined (reading 'map')\n" +
            "浏览器: Chrome 120",
        },
        {
          title: "性能变慢",
          message:
            "最近一次部署后，项目看板页面加载时间从 200ms 变成 5s。\n" +
            "怀疑是数据库查询变慢。请帮我排查性能退化原因。",
        },
      ]}
      className="h-full"
    />
  );
}
