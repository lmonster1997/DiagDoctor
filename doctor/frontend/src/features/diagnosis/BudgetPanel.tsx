/**
 * BudgetPanel — real-time token/cost/tool-call dashboard (Phase 1).
 *
 * Reads budget_ticks (incremental snapshots) and budget (final state)
 * from CopilotKit's useCoAgent state sync.
 */

import { Activity, Coins, Wrench, Clock, AlertTriangle } from "lucide-react";
import type { BudgetState, BudgetTick } from "@/api/types";

interface BudgetPanelProps {
  tick: BudgetTick | null;
  budget: BudgetState | null;
}

export function BudgetPanel({ tick, budget }: BudgetPanelProps) {
  const iteration = tick?.model_call_count ?? 0;
  const maxIter = 12;
  const toolCalls = tick?.tool_calls ?? budget?.tool_calls ?? 0;
  const totalTokens = tick?.total_tokens ?? budget?.total_tokens ?? 0;
  const maxTokens = 100_000;
  const cost = tick?.total_cost_usd ?? budget?.total_cost_usd ?? 0;
  const elapsed = tick?.elapsed_seconds ?? budget?.elapsed_seconds ?? 0;
  const exhausted = tick?.budget_exhausted ?? false;

  const tokenPct = maxTokens > 0 ? Math.min((totalTokens / maxTokens) * 100, 100) : 0;
  const iterPct = maxIter > 0 ? Math.min((iteration / maxIter) * 100, 100) : 0;

  return (
    <div className="border-b border-border p-4">
      <h3 className="mb-3 text-sm font-semibold text-foreground">
        诊断进度
      </h3>

      {/* Iteration bar */}
      <div className="mb-3">
        <div className="mb-1 flex items-center justify-between text-xs">
          <span className="flex items-center gap-1 text-muted-foreground">
            <Activity className="size-3" />
            迭代
          </span>
          <span className="tabular-nums text-foreground">
            {iteration}/{maxIter}
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-all duration-300"
            style={{ width: `${iterPct}%` }}
          />
        </div>
      </div>

      {/* Token bar */}
      <div className="mb-3">
        <div className="mb-1 flex items-center justify-between text-xs">
          <span className="flex items-center gap-1 text-muted-foreground">
            <Coins className="size-3" />
            Token
          </span>
          <span className="tabular-nums text-foreground">
            {(totalTokens / 1000).toFixed(1)}k / {(maxTokens / 1000).toFixed(0)}k
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-chart-2 transition-all duration-300"
            style={{ width: `${tokenPct}%` }}
          />
        </div>
      </div>

      {/* Stats grid */}
      <div className="mb-2 grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-md bg-muted/50 px-2 py-1.5">
          <div className="flex items-center gap-1 text-muted-foreground">
            <Wrench className="size-3" />
            工具调用
          </div>
          <div className="mt-0.5 font-mono text-foreground tabular-nums">
            {toolCalls}
          </div>
        </div>
        <div className="rounded-md bg-muted/50 px-2 py-1.5">
          <div className="flex items-center gap-1 text-muted-foreground">
            <Clock className="size-3" />
            耗时
          </div>
          <div className="mt-0.5 font-mono text-foreground tabular-nums">
            {elapsed.toFixed(1)}s
          </div>
        </div>
        <div className="rounded-md bg-muted/50 px-2 py-1.5">
          <div className="flex items-center gap-1 text-muted-foreground">
            $
            费用
          </div>
          <div className="mt-0.5 font-mono text-foreground tabular-nums">
            ${cost.toFixed(4)}
          </div>
        </div>
        <div className="rounded-md bg-muted/50 px-2 py-1.5">
          <div className="flex items-center gap-1 text-muted-foreground">
            %
            用量
          </div>
          <div className="mt-0.5 font-mono text-foreground tabular-nums">
            {tokenPct > 0 ? `${tokenPct.toFixed(0)}%` : "—"}
          </div>
        </div>
      </div>

      {/* Early stop warning */}
      {exhausted && (
        <div className="flex items-center gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
          <AlertTriangle className="size-3" />
          预算耗尽，诊断提前终止
        </div>
      )}
    </div>
  );
}
