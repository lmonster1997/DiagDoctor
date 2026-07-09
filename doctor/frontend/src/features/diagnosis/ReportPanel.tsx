/**
 * ReportPanel — structured diagnosis report display (Phase 1, minimal).
 *
 * Shows root_cause, affected file, fix_suggestion, confidence.
 * Phase 2 will add EvidenceChainGraph integration and clickable evidence refs.
 */

import { FileCode, Lightbulb, Target, AlertTriangle } from "lucide-react";
import type { DiagnosisReport } from "@/api/types";

interface ReportPanelProps {
  report: DiagnosisReport;
}

export function ReportPanel({ report }: ReportPanelProps) {
  const confPct = Math.round(report.confidence * 100);

  return (
    <div className="p-4">
      <h3 className="mb-3 text-sm font-semibold text-foreground">
        诊断报告
      </h3>

      <div className="flex flex-col gap-3 text-sm">
        {/* Confidence bar */}
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
                confPct >= 80
                  ? "bg-green-500"
                  : confPct >= 50
                    ? "bg-yellow-500"
                    : "bg-red-500"
              }`}
              style={{ width: `${confPct}%` }}
            />
          </div>
        </div>

        {/* Root cause */}
        <div className="rounded-md bg-muted/50 p-3">
          <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
            <AlertTriangle className="size-3" />
            根因 ({report.primary_category || "未分类"})
          </div>
          <p className="text-foreground whitespace-pre-wrap leading-relaxed">
            {report.root_cause || "（未识别）"}
          </p>
        </div>

        {/* Affected file */}
        {report.affected_file && (
          <div className="rounded-md bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
              <FileCode className="size-3" />
              受影响文件
            </div>
            <code className="text-xs text-foreground">
              {report.affected_file}
              {report.affected_line != null && `:${report.affected_line}`}
            </code>
          </div>
        )}

        {/* Fix suggestion */}
        {report.fix_suggestion && (
          <div className="rounded-md bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1 text-xs text-muted-foreground">
              <Lightbulb className="size-3" />
              修复建议
            </div>
            <p className="text-foreground whitespace-pre-wrap leading-relaxed">
              {report.fix_suggestion}
            </p>
          </div>
        )}

        {/* Early stopped badge */}
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
