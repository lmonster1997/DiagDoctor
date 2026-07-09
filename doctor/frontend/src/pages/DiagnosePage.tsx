/**
 * DiagnosePage — diagnostic chat main page (Phase 1).
 *
 * Layout: left 60% CopilotChat (streaming + tool calls), right 40% side panels
 * (BudgetPanel + ReportPanel).
 */
import { CopilotChat } from "@copilotkit/react-ui";
import { useCoAgent } from "@copilotkit/react-core";
import { BudgetPanel } from "@/features/diagnosis/BudgetPanel";
import { ReportPanel } from "@/features/diagnosis/ReportPanel";
import type { BudgetState, DiagnosisReport } from "@/api/types";

export default function DiagnosePage() {
  const { state } = useCoAgent<{
    budget: BudgetState;
    budget_ticks: Record<string, unknown>[];
    report: DiagnosisReport | null;
  }>({ name: "diagnosis" });

  const latestTick = state.budget_ticks?.at(-1);
  const budget = state.budget;
  const report = state.report;

  return (
    <div className="flex h-full gap-0">
      {/* Left: Chat */}
      <div className="flex flex-1 flex-col min-w-0">
        <CopilotChat
          labels={{
            title: "🔬 DiagDoctor 诊断助手",
            initial:
              "你好！请描述你遇到的 Bug：\n\n" +
              "- 什么操作触发了错误？\n" +
              "- 有没有错误日志或 Trace ID？\n" +
              "- 浏览器 Console 有报错吗？\n\n" +
              "我会自动查询可观测性数据来帮你定位根因。",
            placeholder: "描述 Bug 现象，或粘贴错误日志 / Trace ID ...",
          }}
          className="h-full"
        />
      </div>

      {/* Right: Side panels */}
      <div className="flex w-[360px] shrink-0 flex-col gap-0 border-l border-border bg-card overflow-y-auto">
        <BudgetPanel
          tick={latestTick ?? null}
          budget={budget ?? null}
        />
        {report && <ReportPanel report={report} />}
        {!report && (
          <div className="flex flex-1 items-center justify-center p-8 text-center">
            <p className="text-sm text-muted-foreground">
              诊断完成后，报告将显示在这里
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
