/**
 * DiagnosePage — diagnostic chat main page (Phase 1 + Phase 2).
 *
 * Layout: left CopilotChat (streaming + tool calls), right tabbed panel:
 *   - 证据链 (EvidenceChainGraph)
 *   - 报告 (ReportPanel)
 *   - 进度 (BudgetPanel)
 *
 * Clicking an evidence_ref in ReportPanel switches to the 证据链 tab and
 * highlights the corresponding signal node.
 */
import { useMemo, useState } from "react";
import { CopilotChat } from "@copilotkit/react-ui";
import { useCoAgent, useCopilotMessagesContext } from "@copilotkit/react-core";
import { Network, FileText, Activity } from "lucide-react";

import { BudgetPanel } from "@/features/diagnosis/BudgetPanel";
import { EvidenceChainGraph } from "@/features/diagnosis/EvidenceChainGraph";
import { ReportPanel } from "@/features/diagnosis/ReportPanel";
import { parseAgentState, type RawAgentState } from "@/features/diagnosis/parseAgentState";
import type { BudgetState, BudgetTick } from "@/api/types";

type Tab = "graph" | "report" | "budget";

interface AgentState extends RawAgentState {
  budget?: BudgetState | null;
  budget_ticks?: BudgetTick[];
}

export default function DiagnosePage() {
  const { state } = useCoAgent<AgentState>({ name: "default" });
  const { messages: chatMessages } = useCopilotMessagesContext();

  // The CopilotKit runtime invokes the inner create_agent directly, so the
  // report lives in the final assistant chat message as JSON. We parse it
  // from the chat messages (reliable) and fall back to direct state fields
  // / state.messages if present (outer-graph flow).
  const { report, findings, evidence } = useMemo(
    () => parseAgentState(state, chatMessages),
    [state, chatMessages],
  );

  const latestTick = state.budget_ticks?.at(-1) ?? null;
  const budget = state.budget ?? null;

  const [tab, setTab] = useState<Tab>("graph");
  const [highlightedRef, setHighlightedRef] = useState<string | null>(null);

  const highlightedRefs = useMemo(
    () => (highlightedRef ? new Set([highlightedRef]) : new Set<string>()),
    [highlightedRef],
  );

  const handleHighlightRef = (ref: string) => {
    setHighlightedRef(ref);
    setTab("graph");
  };

  // Auto-switch to report tab when a report arrives and user hasn't navigated.
  // (Kept minimal: we leave the user on whatever tab they chose.)

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

      {/* Right: Tabbed side panel */}
      <div className="flex w-[560px] shrink-0 flex-col border-l border-border bg-card">
        {/* Tab bar */}
        <div className="flex shrink-0 border-b border-border">
          <TabButton
            active={tab === "graph"}
            onClick={() => setTab("graph")}
            icon={<Network className="size-3.5" />}
            label="证据链"
          />
          <TabButton
            active={tab === "report"}
            onClick={() => setTab("report")}
            icon={<FileText className="size-3.5" />}
            label="报告"
            badge={report ? "1" : undefined}
          />
          <TabButton
            active={tab === "budget"}
            onClick={() => setTab("budget")}
            icon={<Activity className="size-3.5" />}
            label="进度"
          />
        </div>

        {/* Tab content */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {tab === "graph" && (
            <EvidenceChainGraph
              evidence={evidence}
              findings={findings}
              report={report}
              highlightedRefs={highlightedRefs}
            />
          )}
          {tab === "report" && (
            <div className="flex flex-1 flex-col overflow-y-auto">
              {report ? (
                <ReportPanel
                  report={report}
                  onHighlightRef={handleHighlightRef}
                  highlightedRef={highlightedRef}
                />
              ) : (
                <EmptyState text="诊断完成后，报告将显示在这里" />
              )}
            </div>
          )}
          {tab === "budget" && (
            <div className="flex flex-1 flex-col overflow-y-auto">
              <BudgetPanel tick={latestTick} budget={budget} />
              <div className="flex flex-1 items-center justify-center p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  实时预算与 token 用量
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  badge?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex flex-1 items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors ${
        active
          ? "border-b-2 border-primary text-foreground"
          : "border-b-2 border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {icon}
      {label}
      {badge && (
        <span className="rounded-full bg-primary px-1.5 text-[10px] text-primary-foreground">
          {badge}
        </span>
      )}
    </button>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex flex-1 items-center justify-center p-8 text-center">
      <p className="text-sm text-muted-foreground">{text}</p>
    </div>
  );
}
