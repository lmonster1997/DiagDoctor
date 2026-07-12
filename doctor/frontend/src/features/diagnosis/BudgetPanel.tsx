/**
 * BudgetPanel — "微型 Cockpit" 实时资源仪表盘.
 *
 * §3: 环形进度 + Token 双条 + 工具调用点阵 + 阶段指示器 + Cost 柱状图.
 */
import { Activity, Coins, Wrench, Clock, AlertTriangle, Zap } from "lucide-react";
import type { BudgetState, BudgetTick } from "@/api/types";

interface BudgetPanelProps {
  tick: BudgetTick | null;
  budget: BudgetState | null;
}

/** Phase config */
const PHASES = [
  { key: "signal", label: "获取信号", icon: "📡" },
  { key: "correlate", label: "关联分析", icon: "🔗" },
  { key: "conclude", label: "生成结论", icon: "🧠" },
  { key: "report", label: "报告", icon: "📋" },
] as const;

function phaseFromIteration(iter: number, maxIter: number): number {
  if (iter === 0) return -1;
  const pct = iter / maxIter;
  if (pct < 0.25) return 0;
  if (pct < 0.55) return 1;
  if (pct < 0.85) return 2;
  return 3;
}

/** Estimate input/output token split (typical LLM ratio: ~35% input, ~65% output). */
function estimateTokenSplit(total: number): { input: number; output: number } {
  if (total === 0) return { input: 0, output: 0 };
  const input = Math.round(total * 0.35);
  const output = total - input;
  return { input, output };
}

export function BudgetPanel({ tick, budget }: BudgetPanelProps) {
  const iteration = tick?.model_call_count ?? 0;
  const maxIter = 12;
  const toolCalls = tick?.tool_calls ?? budget?.tool_calls ?? 0;
  const totalTokens = tick?.total_tokens ?? budget?.total_tokens ?? 0;
  const { input: estimatedInput, output: estimatedOutput } = estimateTokenSplit(totalTokens);
  const maxTokens = 100_000;
  const cost = tick?.total_cost_usd ?? budget?.total_cost_usd ?? 0;
  const elapsed = tick?.elapsed_seconds ?? budget?.elapsed_seconds ?? 0;
  const exhausted = tick?.budget_exhausted ?? false;

  const tokenPct = maxTokens > 0 ? Math.min((totalTokens / maxTokens) * 100, 100) : 0;
  const iterPct = maxIter > 0 ? Math.min((iteration / maxIter) * 100, 100) : 0;
  const activePhase = phaseFromIteration(iteration, maxIter);
  const modelCalls = tick?.model_call_count ?? 0;

  // ── Ring ────────────────────────────────────────────────────
  const ringRadius = 34;
  const ringCircumference = 2 * Math.PI * ringRadius;
  const ringOffset = ringCircumference - (iterPct / 100) * ringCircumference;
  const ringColor =
    iterPct >= 80 ? "#ef4444" : iterPct >= 50 ? "#f59e0b" : "#3b82f6";

  // ── Tool call dots ──────────────────────────────────────────
  const MAX_DOTS = 12;
  const toolCallDots = Array.from({ length: MAX_DOTS }, (_, i) => {
    if (i < toolCalls) return "executed" as const;
    return "empty" as const;
  });

  return (
    <div className="flex flex-col gap-4 p-4">
      {/* ═══════════════════════════════════════════════════════════
          EARLY STOP WARNING (top per design doc)
          ═══════════════════════════════════════════════════════════ */}
      {exhausted && (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2.5 text-xs text-red-400 animate-fade-in-up">
          <AlertTriangle className="size-3.5 shrink-0" />
          <div>
            <div className="font-medium">预算耗尽 — 诊断提前终止</div>
            <div className="text-[10px] text-red-400/70">
              已达迭代上限（{iteration}/{maxIter}）或 token 阈值
            </div>
          </div>
        </div>
      )}

      {/* ═══════════════════════════════════════════════════════════
          SECTION 1: Ring + Key Metrics
          ═══════════════════════════════════════════════════════════ */}
      <div className="flex items-start gap-4">
        {/* Ring progress */}
        <div className="relative shrink-0">
          <svg width="84" height="84" viewBox="0 0 84 84">
            <circle
              cx="42" cy="42" r={ringRadius}
              fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="5"
            />
            <circle
              cx="42" cy="42" r={ringRadius}
              fill="none" stroke={ringColor} strokeWidth="5"
              strokeLinecap="round"
              strokeDasharray={ringCircumference}
              strokeDashoffset={ringOffset}
              transform="rotate(-90 42 42)"
              className="transition-all duration-700 ease-out"
            />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-base font-bold tabular-nums text-[#e4e4ef]">
              {iteration}
            </span>
            <span className="text-[9px] text-[#5c6070]">/{maxIter}</span>
          </div>
          {/* Pulse ring when > 80% */}
          {iterPct >= 80 && (
            <svg width="84" height="84" viewBox="0 0 84 84"
              className="absolute inset-0 animate-pulse-soft">
              <circle cx="42" cy="42" r={ringRadius}
                fill="none" stroke={ringColor} strokeWidth="2" opacity="0.35" />
            </svg>
          )}
          {/* Warning icon when > 80% */}
          {iterPct >= 80 && (
            <div className="absolute -right-1 -top-1 flex size-5 items-center justify-center rounded-full bg-red-500/20 text-[10px] text-red-400">
              !
            </div>
          )}
        </div>

        {/* Key numbers grid */}
        <div className="flex flex-1 flex-col gap-2">
          {/* ── Token 双条 (input / output) ──────────────────── */}
          <div className="glass rounded-lg px-3 py-2">
            <div className="mb-1 flex items-center gap-1 text-[10px] text-[#5c6070]">
              <Coins className="size-3" />
              Token 用量
              <span className="ml-auto font-mono tabular-nums text-[#8a8fa3]">
                {tokenPct.toFixed(0)}%
              </span>
            </div>
            {/* Dual bar: input (blue) + output (purple) */}
            <div className="mb-1.5 flex h-3 gap-0.5 rounded-full overflow-hidden bg-white/[0.04]">
              <div
                className="h-full rounded-l-full bg-[#3b82f6]/70 transition-all duration-300"
                style={{ width: `${Math.min((estimatedInput / maxTokens) * 100, 100)}%` }}
                title={`输入 ~${(estimatedInput / 1000).toFixed(1)}k tokens`}
              />
              <div
                className="h-full rounded-r-full bg-[#a855f7]/70 transition-all duration-300"
                style={{ width: `${Math.min((estimatedOutput / maxTokens) * 100, 100)}%` }}
                title={`输出 ~${(estimatedOutput / 1000).toFixed(1)}k tokens`}
              />
            </div>
            {/* Labels */}
            <div className="flex items-center justify-between text-[9px]">
              <span className="flex items-center gap-1">
                <span className="inline-block size-1.5 rounded-full bg-[#3b82f6]" />
                <span className="text-[#5c6070]">输入 {(estimatedInput / 1000).toFixed(1)}k</span>
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block size-1.5 rounded-full bg-[#a855f7]" />
                <span className="text-[#5c6070]">输出 {(estimatedOutput / 1000).toFixed(1)}k</span>
              </span>
            </div>
          </div>

          {/* ── Stat cards ────────────────────────────────────── */}
          <div className="grid grid-cols-2 gap-1.5">
            <div className="glass rounded-lg px-2.5 py-1.5">
              <div className="flex items-center gap-1 text-[10px] text-[#5c6070]">
                <Wrench className="size-3" /> 工具调用
              </div>
              <div className="font-mono text-sm tabular-nums text-[#e4e4ef]">{toolCalls}</div>
            </div>
            <div className="glass rounded-lg px-2.5 py-1.5">
              <div className="flex items-center gap-1 text-[10px] text-[#5c6070]">
                <Clock className="size-3" /> 耗时
              </div>
              <div className="font-mono text-sm tabular-nums text-[#e4e4ef]">{elapsed.toFixed(1)}s</div>
            </div>
            <div className="glass rounded-lg px-2.5 py-1.5">
              <div className="flex items-center gap-1 text-[10px] text-[#5c6070]">
                <Zap className="size-3" /> LLM 调用
              </div>
              <div className="font-mono text-sm tabular-nums text-[#e4e4ef]">{modelCalls}</div>
            </div>
            <div className="glass rounded-lg px-2.5 py-1.5">
              <div className="flex items-center gap-1 text-[10px] text-[#5c6070]">
                $ 累计花费
              </div>
              <div className="font-mono text-sm tabular-nums text-[#e4e4ef]">${cost.toFixed(4)}</div>
            </div>
          </div>
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════════
          SECTION 2: Tool Call Dot Row
          ═══════════════════════════════════════════════════════════ */}
      {toolCalls > 0 && (
        <div className="glass rounded-lg p-3 animate-fade-in-up">
          <div className="mb-2 flex items-center justify-between text-[10px]">
            <span className="font-medium text-[#5c6070] uppercase tracking-wider">
              工具调用序列
            </span>
            <span className="font-mono tabular-nums text-[#8a8fa3]">
              {toolCalls}/{MAX_DOTS}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            {toolCallDots.map((status, i) => (
              <div key={i} className="flex flex-1 justify-center">
                <div
                  className={`size-2.5 rounded-full transition-all duration-300 ${
                    status === "executed"
                      ? i < toolCalls - 1
                        ? "bg-[#3b82f6] shadow-[0_0_4px_rgba(59,130,246,0.4)]"
                        : "bg-[#22c55e] shadow-[0_0_4px_rgba(34,197,94,0.4)] animate-pulse-soft"
                      : "bg-white/[0.04]"
                  }`}
                  title={
                    status === "executed"
                      ? `工具调用 #${i + 1} ${i === toolCalls - 1 ? "(进行中)" : "(已完成)"}`
                      : "待执行"
                  }
                />
              </div>
            ))}
          </div>
          <div className="mt-1 flex items-center justify-between text-[8px] text-[#5c6070]">
            <span>#1</span>
            <span>#{MAX_DOTS}</span>
          </div>
        </div>
      )}

      {/* ═══════════════════════════════════════════════════════════
          SECTION 3: Phase Indicator
          ═══════════════════════════════════════════════════════════ */}
      <div className="glass rounded-lg p-3">
        <div className="mb-2 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
          诊断阶段
        </div>
        <div className="flex items-center gap-1">
          {PHASES.map((phase, i) => {
            const isCompleted = i < activePhase;
            const isActive = i === activePhase;
            return (
              <div key={phase.key} className="flex flex-1 items-center gap-1">
                <div
                  className={`flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] transition-all duration-300 ${
                    isCompleted
                      ? "bg-green-500/20 text-green-400"
                      : isActive
                        ? "bg-blue-500/20 text-blue-400 animate-breathe"
                        : "bg-white/[0.04] text-[#5c6070]"
                  }`}
                  title={phase.label}
                >
                  {isCompleted ? "✓" : phase.icon}
                </div>
                {i < PHASES.length - 1 && (
                  <div className="h-px flex-1 bg-white/[0.06]">
                    <div
                      className="h-full bg-blue-500/40 transition-all duration-500"
                      style={{ width: isCompleted ? "100%" : isActive ? "50%" : "0%" }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-1.5 flex items-center gap-1">
          {PHASES.map((phase, i) => (
            <span
              key={phase.key}
              className={`flex-1 text-center text-[9px] transition-colors ${
                i <= activePhase ? "text-[#8a8fa3]" : "text-[#5c6070]"
              }`}
            >
              {phase.label}
            </span>
          ))}
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════════
          SECTION 4: Cost Mini Chart (with hover tooltips)
          ═══════════════════════════════════════════════════════════ */}
      {cost > 0 && (
        <div className="glass rounded-lg p-3">
          <div className="mb-2 flex items-center justify-between text-[10px]">
            <span className="font-medium text-[#5c6070] uppercase tracking-wider">
              成本分布
            </span>
            <span className="font-mono tabular-nums text-[#8a8fa3]">
              累计 ${cost.toFixed(4)}
            </span>
          </div>
          {/* Mini bars — each bar = one LLM call, hover shows cost */}
          <div className="flex h-5 items-end gap-px">
            {Array.from({ length: Math.min(modelCalls, 20) }).map((_, i) => {
              const barHeight = 25 + ((i + 1) / Math.min(modelCalls, 20)) * 75;
              const estimatedCallCost = cost > 0 && modelCalls > 0
                ? (cost / modelCalls).toFixed(4)
                : "—";
              return (
                <div
                  key={i}
                  className="flex-1 rounded-t-sm bg-gradient-to-t from-[#3b82f6]/30 to-[#a855f7]/50 transition-all hover:from-[#3b82f6]/60 hover:to-[#a855f7]/80"
                  style={{ height: `${barHeight}%` }}
                  title={`调用 #${i + 1} — 预估 $${estimatedCallCost}`}
                />
              );
            })}
            {modelCalls === 0 && (
              <div className="flex h-full flex-1 items-center justify-center text-[10px] text-[#5c6070]">
                尚无 LLM 调用
              </div>
            )}
          </div>
          <div className="mt-1.5 flex items-center justify-between text-[9px] text-[#5c6070]">
            <span>调用 #1</span>
            <span>调用 #{modelCalls || "—"}</span>
          </div>
          {/* Average cost line */}
          {modelCalls > 0 && (
            <div className="mt-1 text-[9px] text-[#5c6070]">
              平均每次: <span className="font-mono text-[#8a8fa3]">${(cost / modelCalls).toFixed(5)}</span>
            </div>
          )}
        </div>
      )}

      {/* ═══════════════════════════════════════════════════════════
          IDLE STATE
          ═══════════════════════════════════════════════════════════ */}
      {iteration === 0 && !exhausted && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 py-8 text-center">
          <Activity className="size-6 text-[#5c6070] opacity-40" />
          <p className="text-xs text-[#5c6070]">等待诊断开始…</p>
        </div>
      )}
    </div>
  );
}
