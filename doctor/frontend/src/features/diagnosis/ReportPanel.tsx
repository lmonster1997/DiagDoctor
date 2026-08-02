/**
 * ReportPanel — structured diagnosis report ("AI 诊断室" 临床报告样式).
 */
import {
  FileCode,
  Lightbulb,
  AlertTriangle,
  Layers,
  MapPin,
  StickyNote,
  Link2,
  Copy,
  CheckCircle,
  MessageSquarePlus,
  ArrowRight,
  Library,
  ThumbsUp,
  ThumbsDown,
  ChevronDown,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { DiagnosisReport, ServiceTier, RootCauseTier } from "@/api/types";
import { postCaseFeedback } from "@/api/client";

interface ReportPanelProps {
  report: DiagnosisReport;
  /** §8.1 path 2: backend thread_id (state.case_id) for POST /feedback/{run_id}/case. */
  runId?: string;
  /** §6.5 injection block (all retrieved cases w/ content + [id:...]) — shown
   *  collapsed so the user can see what the agent was given before marking. */
  similarCasesText?: string;
}

const TIER_LABEL: Record<ServiceTier | RootCauseTier, string> = {
  frontend: "前端",
  backend: "后端",
  data: "数据层",
};

/** Severity colour for categories */
function categoryColor(cat: string): string {
  const lower = cat.toLowerCase();
  if (lower.includes("error") || lower.includes("crash")) return "#ef4444";
  if (lower.includes("perf") || lower.includes("slow")) return "#f59e0b";
  if (lower.includes("data") || lower.includes("config")) return "#a855f7";
  return "#3b82f6";
}

export function ReportPanel({
  report,
  runId,
  similarCasesText,
}: ReportPanelProps) {
  const confPct = Math.round(report.confidence * 100);
  const [copied, setCopied] = useState(false);

  // §8.1 path 2: map case_id -> {Case N, 根因} parsed from the injection block,
  // so the feedback rows show "Case 1 · <根因>" instead of a raw UUID.
  const caseInfoMap = useMemo(
    () => new Map(parseSimilarCases(similarCasesText ?? "").map((c) => [c.caseId, c])),
    [similarCasesText],
  );

  const affectedRef = report.affected_file
    ? `${report.affected_file}${report.affected_line != null ? `:${report.affected_line}` : ""}`
    : null;

  const handleCopy = async () => {
    const text = [
      `# DiagDoctor 诊断报告`,
      `## 根因: ${report.root_cause || "（未识别）"}`,
      `## 主要类别: ${report.primary_category}`,
      `## 置信度: ${confPct}%`,
      report.affected_file ? `## 受影响文件: ${affectedRef}` : "",
      report.fix_suggestion ? `## 修复建议: ${report.fix_suggestion}` : "",
      `## 证据链: ${report.evidence_chain.join(", ")}`,
    ]
      .filter(Boolean)
      .join("\n\n");
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Confidence ring
  const ringRadius = 22;
  const ringCircumference = 2 * Math.PI * ringRadius;
  const ringOffset = ringCircumference - (confPct / 100) * ringCircumference;
  const ringColor =
    confPct >= 80 ? "#22c55e" : confPct >= 50 ? "#f59e0b" : "#ef4444";

  return (
    <div className="flex flex-col gap-4 p-4 animate-slide-up">
      {/* ── Header: Summary + Confidence ────────────────────────── */}
      <div className="flex items-start gap-3">
        {/* Confidence ring */}
        <div className="relative shrink-0">
          <svg width="56" height="56" viewBox="0 0 56 56">
            <circle
              cx="28" cy="28" r={ringRadius}
              fill="none"
              stroke="rgba(255,255,255,0.06)"
              strokeWidth="4"
            />
            <circle
              cx="28" cy="28" r={ringRadius}
              fill="none"
              stroke={ringColor}
              strokeWidth="4"
              strokeLinecap="round"
              strokeDasharray={ringCircumference}
              strokeDashoffset={ringOffset}
              transform="rotate(-90 28 28)"
              className="transition-all duration-700 ease-out"
            />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-xs font-bold tabular-nums text-[#e4e4ef]">
              {confPct}%
            </span>
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-[#e4e4ef]">初步分析</h3>
          {/* Category pills */}
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            <span
              className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-medium"
              style={{
                backgroundColor: `${categoryColor(report.primary_category)}18`,
                color: categoryColor(report.primary_category),
              }}
            >
              <Layers className="size-3" />
              {report.primary_category || "未分类"}
            </span>
            {report.categories
              .filter((c) => c !== report.primary_category)
              .slice(0, 3)
              .map((c) => (
                <span
                  key={c}
                  className="rounded-md bg-white/[0.04] px-2 py-0.5 text-[10px] text-[#8a8fa3]"
                >
                  {c}
                </span>
              ))}
            <span className="ml-auto text-[10px] text-[#5c6070]">
              症状 {TIER_LABEL[report.symptom_tier]} · 根因 {TIER_LABEL[report.root_cause_tier]}
            </span>
          </div>
        </div>

        {/* Copy button */}
        <button
          type="button"
          onClick={handleCopy}
          className="shrink-0 rounded-md p-1.5 text-[#5c6070] hover:bg-white/[0.06] hover:text-[#e4e4ef] transition-all"
          title="复制报告"
        >
          {copied ? <CheckCircle className="size-3.5 text-green-400" /> : <Copy className="size-3.5" />}
        </button>
      </div>

      {/* ── Root Cause card ─────────────────────────────────────── */}
      <div className="glass rounded-lg p-4">
        <div className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-[#ef4444]">
          <AlertTriangle className="size-3.5" />
          根因分析
        </div>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-[#e4e4ef]">
          {report.root_cause || "（未识别）"}
        </p>
      </div>

      {/* ── Affected File ───────────────────────────────────────── */}
      {affectedRef && (
        <div className="flex items-start gap-3 rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
          <FileCode className="mt-0.5 size-4 shrink-0 text-[#5c6070]" />
          <div className="min-w-0">
            <div className="mb-0.5 flex items-center gap-1.5 text-[10px] text-[#5c6070]">
              <MapPin className="size-3" />
              受影响文件
            </div>
            <code className="block break-all text-xs text-[#e4e4ef]">
              {affectedRef}
            </code>
          </div>
        </div>
      )}

      {/* ── Fix Suggestion — blockquote style ───────────────────── */}
      {report.fix_suggestion && (
        <div className="relative rounded-lg border-l-2 border-[#22c55e] bg-white/[0.02] p-4">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-[#22c55e]">
            <Lightbulb className="size-3.5" />
            修复建议
          </div>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-[#e4e4ef]">
            {report.fix_suggestion}
          </p>
        </div>
      )}

      {/* ── Evidence Chain ──────────────────────────────────────── */}
      {report.evidence_chain.length > 0 && (
        <div className="rounded-lg border border-white/[0.06] bg-white/[0.01] p-3">
          <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
            <Link2 className="size-3" />
            证据链 ({report.evidence_chain.length})
          </div>
          <div className="flex flex-wrap gap-1.5">
            {report.evidence_chain.map((ref, i) => (
              <span
                key={`${ref}-${i}`}
                className="rounded bg-white/[0.03] px-2 py-1 font-mono text-[10px] text-[#8a8fa3]"
              >
                {ref}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── Retrieved historical cases (§6.5 injection block) ──── */}
      {similarCasesText && <HistoryReferenceBlock text={similarCasesText} />}

      {/* ── Referenced historical cases (§8.1 path 2 feedback) ── */}
      {report.referenced_case_ids.length > 0 && (
        <ReferencedCases
          runId={runId}
          caseIds={report.referenced_case_ids}
          caseInfoMap={caseInfoMap}
        />
      )}

      {/* ── Notes ───────────────────────────────────────────────── */}
      {report.notes && (
        <div className="rounded-lg border border-white/[0.04] bg-white/[0.01] p-3">
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
            <StickyNote className="size-3" />
            备注
          </div>
          <p className="whitespace-pre-wrap text-xs leading-relaxed text-[#8a8fa3]">
            {report.notes}
          </p>
        </div>
      )}

      {/* ── Early Stop ──────────────────────────────────────────── */}
      {report.early_stopped && (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2.5 text-xs text-red-400">
          <AlertTriangle className="size-3.5 shrink-0" />
          预算耗尽，诊断提前终止
        </div>
      )}

      {/* ── 继续追问（人机协同入口）─────────────────────────────── */}
      <div className="rounded-xl border border-dashed border-cyan-500/25 bg-cyan-500/[0.03] p-4">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium text-cyan-400 uppercase tracking-wider">
          <MessageSquarePlus className="size-3" />
          继续深入？
        </div>
        <p className="mb-3 text-[11px] leading-relaxed text-[#8a8fa3]">
          以上是初步分析结果，你可以在左侧对话中继续补充信息或追问，AI 会基于已有上下文深入排查。
        </p>
        <div className="flex flex-col gap-1.5">
          {[
            "能帮我深入分析这个根因吗？",
            "还有其他可能的原因吗？",
            "帮我检查一下相关的代码逻辑",
          ].map((hint) => (
            <button
              key={hint}
              type="button"
              className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[11px] text-[#8a8fa3] transition-all hover:bg-white/[0.04] hover:text-[#e4e4ef]"
              onClick={() => {
                // Copy hint to clipboard so user can paste into chat
                navigator.clipboard.writeText(hint);
              }}
              title="点击复制追问到剪贴板"
            >
              <ArrowRight className="size-3 shrink-0 text-[#5c6070]" />
              {hint}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── §8.1 path 2: referenced historical cases + per-case feedback ──

type CaseFeedbackStatus = "loading" | "helpful" | "not-helpful" | "error";

/** A retrieved case parsed out of the §6.5 injection block (for friendly labels). */
interface CaseInfo {
  num: number;
  caseId: string;
  rootCause: string;
}

/**
 * Parse `similar_cases_text` into structured cases so the feedback section can
 * show "Case N · <根因摘要>" instead of a raw UUID. The block format is fixed
 * by the backend (`format_similar_cases`): `### Case N [id: xxx](综合分: ...)`
 * followed by `- 根因: ...`. Returns [] if the block is absent/unparseable
 * (caller falls back to a truncated id).
 */
function parseSimilarCases(text: string): CaseInfo[] {
  if (!text) return [];
  const cases: CaseInfo[] = [];
  let current: CaseInfo | null = null;
  const headerRe = /^### Case (\d+) \[id: ([^\]]+)\]/;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const m = line.match(headerRe);
    if (m) {
      if (current) cases.push(current);
      current = { num: Number(m[1]), caseId: m[2], rootCause: "" };
    } else if (current && line.startsWith("- 根因:")) {
      current.rootCause = line.slice("- 根因:".length).trim();
    }
  }
  if (current) cases.push(current);
  return cases;
}

function ReferencedCases({
  runId,
  caseIds,
  caseInfoMap,
}: {
  runId?: string;
  caseIds: string[];
  caseInfoMap?: Map<string, CaseInfo>;
}) {
  // Per-case status. undefined = not yet marked. Once "helpful"/"not-helpful",
  // the row locks (one mark per case; backend is non-idempotent +0.1).
  const [status, setStatus] = useState<Record<string, CaseFeedbackStatus>>({});

  const handle = async (caseId: string, helpful: boolean) => {
    if (!runId || status[caseId]) return;
    setStatus((s) => ({ ...s, [caseId]: "loading" }));
    try {
      await postCaseFeedback(runId, caseId, helpful);
      setStatus((s) => ({ ...s, [caseId]: helpful ? "helpful" : "not-helpful" }));
    } catch {
      setStatus((s) => ({ ...s, [caseId]: "error" }));
    }
  };

  return (
    <div className="rounded-lg border border-white/[0.06] bg-white/[0.01] p-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider">
        <ThumbsUp className="size-3" />
        本次实际引用 ({caseIds.length})
      </div>
      <p className="mb-2 text-[10px] leading-relaxed text-[#5c6070]">
        Agent 声明本次诊断参考了以上历史 case。哪个对你有帮助?反馈仅用于改进记忆检索质量。
      </p>
      <div className="flex flex-col gap-1.5">
        {caseIds.map((cid) => {
          const st = status[cid];
          const locked = st === "helpful" || st === "not-helpful";
          const info = caseInfoMap?.get(cid);
          const label = info ? `Case ${info.num}` : cid.slice(0, 8);
          return (
            <div
              key={cid}
              title={cid}
              className="flex items-center gap-2 rounded-md bg-white/[0.02] px-2 py-1.5"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-[10px] font-medium text-[#e4e4ef]">
                  {label}
                </div>
                {info?.rootCause && (
                  <div className="truncate text-[10px] text-[#5c6070]">
                    {info.rootCause}
                  </div>
                )}
              </div>
              {st === "error" ? (
                <span className="text-[10px] text-red-400">标记失败</span>
              ) : locked ? (
                <span className="flex items-center gap-1 text-[10px] text-green-400">
                  <CheckCircle className="size-3" />
                  {st === "helpful" ? "已标记有帮助" : "已记录"}
                </span>
              ) : (
                <div className="flex items-center gap-0.5">
                  <button
                    type="button"
                    disabled={!runId || st === "loading"}
                    onClick={() => handle(cid, true)}
                    className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-[#8a8fa3] transition-all hover:bg-green-500/10 hover:text-green-400 disabled:opacity-40"
                    title="这个历史参考有帮助"
                  >
                    <ThumbsUp className="size-3" />
                    有帮助
                  </button>
                  <button
                    type="button"
                    disabled={!runId || st === "loading"}
                    onClick={() => handle(cid, false)}
                    className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-[#8a8fa3] transition-all hover:bg-white/[0.06] hover:text-[#e4e4ef] disabled:opacity-40"
                    title="这个历史参考没帮助"
                  >
                    <ThumbsDown className="size-3" />
                    没帮助
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── §6.5 injection block (all retrieved cases) - collapsible context ──

function HistoryReferenceBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  return (
    <div className="rounded-lg border border-white/[0.06] bg-white/[0.01] p-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 text-[10px] font-medium text-[#5c6070] uppercase tracking-wider"
      >
        <Library className="size-3" />
        历史相似诊断(诊断前注入给 AI)
        <ChevronDown
          className={`ml-auto size-3 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-0.5">
          {lines.map((line, i) => {
            const t = line.trim();
            if (!t) return <div key={i} className="h-1" />;
            if (t.startsWith("### "))
              return (
                <div key={i} className="mt-1 text-[10px] font-semibold text-[#3b82f6]">
                  {t.slice(4)}
                </div>
              );
            if (t.startsWith("## "))
              return (
                <div key={i} className="mt-1 text-[11px] font-semibold text-[#e4e4ef]">
                  {t.slice(3)}
                </div>
              );
            if (t.startsWith("- "))
              return (
                <div key={i} className="pl-2 text-[10px] leading-relaxed text-[#8a8fa3]">
                  · {t.slice(2)}
                </div>
              );
            if (t.startsWith("⚠️"))
              return (
                <div key={i} className="mt-1 text-[10px] leading-relaxed text-amber-400">
                  {t}
                </div>
              );
            return (
              <div key={i} className="text-[10px] leading-relaxed text-[#8a8fa3]">
                {t}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
