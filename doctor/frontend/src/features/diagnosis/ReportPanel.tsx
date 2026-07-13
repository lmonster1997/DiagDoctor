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
} from "lucide-react";
import { useState } from "react";
import type { DiagnosisReport, ServiceTier, RootCauseTier } from "@/api/types";

interface ReportPanelProps {
  report: DiagnosisReport;
  onHighlightRef?: (ref: string) => void;
  highlightedRef?: string | null;
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

export function ReportPanel({ report, onHighlightRef, highlightedRef }: ReportPanelProps) {
  const confPct = Math.round(report.confidence * 100);
  const [copied, setCopied] = useState(false);

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
        <button
          type="button"
          onClick={() => onHighlightRef?.(affectedRef)}
          className="group flex items-start gap-3 rounded-lg border border-white/[0.06] bg-white/[0.02] p-3 text-left transition-all hover:border-white/[0.10] hover:bg-white/[0.04]"
          title="点击在证据链中高亮相关节点"
        >
          <FileCode className="mt-0.5 size-4 shrink-0 text-[#5c6070] group-hover:text-[#3b82f6]" />
          <div className="min-w-0">
            <div className="mb-0.5 flex items-center gap-1.5 text-[10px] text-[#5c6070]">
              <MapPin className="size-3" />
              受影响文件
            </div>
            <code className="block break-all text-xs text-[#e4e4ef] group-hover:text-[#3b82f6] transition-colors">
              {affectedRef}
            </code>
          </div>
        </button>
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
            {report.evidence_chain.map((ref, i) => {
              const active = highlightedRef === ref;
              return (
                <button
                  key={`${ref}-${i}`}
                  type="button"
                  onClick={() => onHighlightRef?.(ref)}
                  className={`rounded px-2 py-1 font-mono text-[10px] transition-all ${
                    active
                      ? "bg-[#3b82f6]/20 text-[#3b82f6] ring-1 ring-[#3b82f6]/30"
                      : "bg-white/[0.03] text-[#8a8fa3] hover:bg-white/[0.06] hover:text-[#e4e4ef]"
                  }`}
                  title="点击在证据链中高亮该证据"
                >
                  {ref}
                </button>
              );
            })}
          </div>
        </div>
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
