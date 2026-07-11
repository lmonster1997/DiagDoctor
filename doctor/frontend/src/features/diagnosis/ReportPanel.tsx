/**
 * ReportPanel — structured diagnosis report display (Phase 2, full).
 */
import {
  FileCode,
  Lightbulb,
  Target,
  AlertTriangle,
  Layers,
  MapPin,
  StickyNote,
  Link2,
} from "lucide-react";
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

export function ReportPanel({ report, onHighlightRef, highlightedRef }: ReportPanelProps) {
  const confPct = Math.round(report.confidence * 100);
  const affectedRef = report.affected_file
    ? `${report.affected_file}${report.affected_line != null ? `:${report.affected_line}` : ""}`
    : null;

  return (
    <div className="flex flex-col gap-3 p-4">
      <h3 className="text-sm font-semibold text-foreground">诊断报告</h3>
      <div className="flex flex-col gap-3 text-sm">
        <div>
          <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <Target className="size-3" />
              置信度
            </span>
            <span className="tabular-nums">{confPct}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-muted">
            <div
              className={`h-full rounded-full transition-all ${
                confPct >= 80 ? "bg-green-500" : confPct >= 50 ? "bg-yellow-500" : "bg-red-500"
              }`}
              style={{ width: `${confPct}%` }}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="flex items-center gap-1 rounded-md bg-primary/10 px-2 py-0.5 font-medium text-primary">
            <Layers className="size-3" />
            {report.primary_category || "未分类"}
          </span>
          {report.categories
            .filter((c) => c !== report.primary_category)
            .map((c) => (
              <span key={c} className="rounded-md bg-muted px-2 py-0.5 text-muted-foreground">
                {c}
              </span>
            ))}
          <span className="ml-auto text-muted-foreground">
            症状 {TIER_LABEL[report.symptom_tier]} | 根因 {TIER_LABEL[report.root_cause_tier]}
          </span>
        </div>

        <div className="rounded-md bg-muted/50 p-3">
          <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
            <AlertTriangle className="size-3" />
            根因
          </div>
          <p className="whitespace-pre-wrap leading-relaxed text-foreground">
            {report.root_cause || "（未识别）"}
          </p>
        </div>

        {affectedRef && (
          <button
            type="button"
            onClick={() => onHighlightRef?.(affectedRef)}
            className="group flex items-start gap-2 rounded-md bg-muted/50 p-3 text-left transition-colors hover:bg-muted"
            title="点击在证据链中高亮相关节点"
          >
            <FileCode className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
            <div className="min-w-0">
              <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
                <MapPin className="size-3" />
                受影响文件
              </div>
              <code className="block break-all text-xs text-foreground group-hover:text-primary">
                {affectedRef}
              </code>
            </div>
          </button>
        )}

        {report.fix_suggestion && (
          <div className="rounded-md bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
              <Lightbulb className="size-3" />
              修复建议
            </div>
            <p className="whitespace-pre-wrap leading-relaxed text-foreground">
              {report.fix_suggestion}
            </p>
          </div>
        )}

        {report.evidence_chain.length > 0 && (
          <div className="rounded-md bg-muted/50 p-3">
            <div className="mb-1.5 flex items-center gap-1 text-xs text-muted-foreground">
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
                    className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                      active
                        ? "bg-primary text-primary-foreground"
                        : "bg-background text-muted-foreground hover:bg-primary/15 hover:text-primary"
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

        {report.notes && (
          <div className="rounded-md bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
              <StickyNote className="size-3" />
              备注
            </div>
            <p className="whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">
              {report.notes}
            </p>
          </div>
        )}

        {report.early_stopped && (
          <div className="flex items-center gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
            <AlertTriangle className="size-3" />
            预算耗尽，诊断提前终止
          </div>
        )}
      </div>
    </div>
  );
}
