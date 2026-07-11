/**
 * EvidenceChainGraph — directed graph visualising the diagnosis evidence chain.
 *
 * Columns (left → right):
 *   1. golden_signals  — one node per Signal, coloured by `source`
 *   2. correlations    — nodes linking multiple signals (edges signal → correlation)
 *   3. findings        — Finding nodes with confidence (edges signal → finding via evidence_ref)
 *   4. report          — terminal node with root_cause + highlighted evidence_chain path
 *
 * Edges are derived from `evidence_ref` strings (the shared reference namespace
 * between Signal.evidence_ref, Finding.evidence_refs and DiagnosisReport.evidence_chain).
 * `raw_refs` is used to resolve any refs that don't directly match a signal's
 * evidence_ref (defensive fallback).
 *
 * Highlighting: nodes/edges on the report's `evidence_chain` path are always
 * highlighted; an external `highlightedRefs` set (from ReportPanel clicks)
 * adds focus to specific signal nodes.
 */
import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  type Edge,
  type Node,
  type NodeProps,
  Position,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";

import type {
  Correlation,
  DiagnosisReport,
  Finding,
  NormalizedEvidence,
  Signal,
  SignalSource,
} from "@/api/types";

// ── Column x-offsets ──────────────────────────────────────────────
const COL_SIGNAL = 0;
const COL_CORRELATION = 320;
const COL_FINDING = 640;
const COL_REPORT = 960;
const ROW_HEIGHT = 88;
const NODE_WIDTH = 240;

// ── Source colour map ─────────────────────────────────────────────
const SOURCE_COLOR: Record<SignalSource, string> = {
  log: "#f59e0b", // amber
  trace: "#3b82f6", // blue
  browser_error: "#a855f7", // purple
  api_response: "#22c55e", // green
  user_report: "#64748b", // slate
};

const SOURCE_LABEL: Record<SignalSource, string> = {
  log: "Log",
  trace: "Trace",
  browser_error: "Browser",
  api_response: "API",
  user_report: "User",
};

// ── Node data types ───────────────────────────────────────────────
interface SignalNodeData {
  signal: Signal;
  highlighted: boolean;
  faded: boolean;
  [key: string]: unknown;
}
interface CorrelationNodeData {
  correlation: Correlation;
  highlighted: boolean;
  faded: boolean;
  [key: string]: unknown;
}
interface FindingNodeData {
  finding: Finding;
  index: number;
  highlighted: boolean;
  faded: boolean;
  [key: string]: unknown;
}
interface ReportNodeData {
  report: DiagnosisReport;
  [key: string]: unknown;
}

// ── Custom node renderers ─────────────────────────────────────────
function SignalNode({ data }: NodeProps<SignalNodeData>) {
  const { signal, highlighted, faded } = data;
  const color = SOURCE_COLOR[signal.source] ?? "#64748b";
  return (
    <div
      className="rounded-md border bg-card px-3 py-2 text-left shadow-sm transition-opacity"
      style={{
        width: NODE_WIDTH,
        borderColor: color,
        borderWidth: highlighted ? 2 : 1,
        opacity: faded ? 0.35 : 1,
        boxShadow: highlighted ? `0 0 0 2px ${color}55` : undefined,
      }}
    >
      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
        <span
          className="inline-block size-2 rounded-full"
          style={{ backgroundColor: color }}
        />
        <span className="truncate">{signal.signal_id || "signal"}</span>
        <span className="ml-auto rounded bg-muted px-1 text-[10px] text-muted-foreground">
          {SOURCE_LABEL[signal.source]}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
        {signal.summary || signal.signal_type}
      </p>
    </div>
  );
}

function CorrelationNode({ data }: NodeProps<CorrelationNodeData>) {
  const { correlation, highlighted, faded } = data;
  return (
    <div
      className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-left transition-opacity"
      style={{
        width: NODE_WIDTH,
        opacity: faded ? 0.35 : 1,
        borderColor: highlighted ? "#0ea5e9" : undefined,
        borderWidth: highlighted ? 2 : 1,
      }}
    >
      <div className="text-[11px] font-semibold text-foreground">
        {correlation.correlation_id || "correlation"}
      </div>
      <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
        {correlation.description}
      </p>
      <div className="mt-1 text-[10px] tabular-nums text-muted-foreground">
        conf {(correlation.confidence * 100).toFixed(0)}%
      </div>
    </div>
  );
}

function FindingNode({ data }: NodeProps<FindingNodeData>) {
  const { finding, index, highlighted, faded } = data;
  const confPct = Math.round(finding.confidence * 100);
  return (
    <div
      className="rounded-md border bg-card px-3 py-2 text-left shadow-sm transition-opacity"
      style={{
        width: NODE_WIDTH,
        borderColor: highlighted ? "#16a34a" : undefined,
        borderWidth: highlighted ? 2 : 1,
        opacity: faded ? 0.35 : 1,
        boxShadow: highlighted ? "0 0 0 2px #16a34a44" : undefined,
      }}
    >
      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
        <span className="truncate">F{index + 1} · {finding.agent || "finding"}</span>
        {finding.cross_layer && (
          <span className="ml-auto rounded bg-purple-500/15 px-1 text-[10px] text-purple-500">
            cross
          </span>
        )}
      </div>
      <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
        {finding.summary}
      </p>
      <div className="mt-1 h-1 rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-green-500"
          style={{ width: `${confPct}%` }}
        />
      </div>
    </div>
  );
}

function ReportNode({ data }: NodeProps<ReportNodeData>) {
  const { report } = data;
  const confPct = Math.round(report.confidence * 100);
  return (
    <div
      className="rounded-lg border-2 border-primary bg-primary/5 px-3 py-2 text-left shadow-md"
      style={{ width: NODE_WIDTH }}
    >
      <div className="text-[12px] font-bold text-foreground">诊断报告</div>
      <div className="mt-1 text-[10px] text-muted-foreground">
        {report.primary_category || "未分类"}
      </div>
      <p className="mt-1 line-clamp-3 text-[11px] leading-snug text-foreground">
        {report.root_cause || "（未识别）"}
      </p>
      <div className="mt-1.5 h-1.5 rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary"
          style={{ width: `${confPct}%` }}
        />
      </div>
      <div className="mt-1 text-[10px] tabular-nums text-muted-foreground">
        置信度 {confPct}%
      </div>
    </div>
  );
}

const NODE_TYPES = {
  signal: SignalNode,
  correlation: CorrelationNode,
  finding: FindingNode,
  report: ReportNode,
};

// ── Props ─────────────────────────────────────────────────────────
interface EvidenceChainGraphProps {
  evidence: NormalizedEvidence | null;
  findings: Finding[];
  report: DiagnosisReport | null;
  /** Extra evidence_refs to focus (e.g. from ReportPanel clicks). */
  highlightedRefs?: Set<string>;
}

// ── Component ─────────────────────────────────────────────────────
export function EvidenceChainGraph({
  evidence,
  findings,
  report,
  highlightedRefs,
}: EvidenceChainGraphProps) {
  const { nodes, edges } = useMemo(() => {
    const signals = evidence?.golden_signals ?? [];
    const correlations = evidence?.correlations ?? [];

    // Refs on the report's evidence chain — the always-highlighted path.
    const chainRefs = new Set<string>(report?.evidence_chain ?? []);
    const focusRefs = highlightedRefs ?? new Set<string>();
    const allHighlighted = new Set<string>([...chainRefs, ...focusRefs]);

    // Index signals by evidence_ref for edge resolution.
    const signalsByRef = new Map<string, Signal[]>();
    for (const s of signals) {
      const key = s.evidence_ref || s.signal_id;
      const arr = signalsByRef.get(key);
      if (arr) arr.push(s);
      else signalsByRef.set(key, [s]);
    }

    const hasFocus = allHighlighted.size > 0;
    const isRefHighlighted = (ref: string | undefined): boolean =>
      ref != null && allHighlighted.has(ref);
    const isRefFaded = (ref: string | undefined): boolean =>
      hasFocus && !isRefHighlighted(ref);

    const newNodes: Node[] = [];
    const newEdges: Edge[] = [];

    // ── Column 1: signals ──
    signals.forEach((signal, i) => {
      const ref = signal.evidence_ref || signal.signal_id;
      const highlighted = isRefHighlighted(ref);
      newNodes.push({
        id: `sig:${signal.signal_id || i}`,
        type: "signal",
        position: { x: COL_SIGNAL, y: i * ROW_HEIGHT },
        data: { signal, highlighted, faded: isRefFaded(ref) },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      });
    });

    // ── Column 2: correlations ──
    correlations.forEach((corr, i) => {
      const linked = [
        ...corr.frontend_signals,
        ...corr.backend_signals,
        ...corr.db_signals,
      ];
      const highlighted = linked.some((sid) => {
        const sigs = signalsByRef.get(sid) ?? [];
        return sigs.some((s) => isRefHighlighted(s.evidence_ref));
      });
      const faded = hasFocus && !highlighted;
      const id = `corr:${corr.correlation_id || i}`;
      newNodes.push({
        id,
        type: "correlation",
        position: { x: COL_CORRELATION, y: i * ROW_HEIGHT },
        data: { correlation: corr, highlighted, faded },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      });
      // edges: signal → correlation
      for (const sid of linked) {
        newEdges.push({
          id: `e:${sid}->${id}`,
          source: `sig:${sid}`,
          target: id,
          animated: highlighted,
          style: { stroke: highlighted ? "#0ea5e9" : "#cbd5e1", strokeWidth: highlighted ? 2 : 1 },
        });
      }
    });

    // ── Column 3: findings ──
    findings.forEach((finding, fi) => {
      const highlighted = finding.evidence_refs.some((r) => isRefHighlighted(r));
      const faded = hasFocus && !highlighted;
      const id = `find:${fi}`;
      newNodes.push({
        id,
        type: "finding",
        position: { x: COL_FINDING, y: fi * ROW_HEIGHT },
        data: { finding, index: fi, highlighted, faded },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      });
      // edges: signal → finding (via shared evidence_ref)
      for (const ref of finding.evidence_refs) {
        const sigs = signalsByRef.get(ref) ?? [];
        for (const s of sigs) {
          const edgeHi = isRefHighlighted(ref);
          newEdges.push({
            id: `e:sig:${s.signal_id}->${id}`,
            source: `sig:${s.signal_id}`,
            target: id,
            animated: edgeHi,
            style: {
              stroke: edgeHi ? "#16a34a" : "#cbd5e1",
              strokeWidth: edgeHi ? 2 : 1,
            },
          });
        }
      }
    });

    // ── Column 4: report (terminal) ──
    if (report) {
      newNodes.push({
        id: "report",
        type: "report",
        position: { x: COL_REPORT, y: 0 },
        data: { report },
        targetPosition: Position.Left,
      });
      // edges: finding → report (highlighted if finding is on the chain)
      findings.forEach((finding, fi) => {
        const onChain = finding.evidence_refs.some((r) => chainRefs.has(r));
        newEdges.push({
          id: `e:find:${fi}->report`,
          source: `find:${fi}`,
          target: "report",
          animated: onChain,
          style: {
            stroke: onChain ? "#16a34a" : "#cbd5e1",
            strokeWidth: onChain ? 2 : 1,
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: onChain ? "#16a34a" : "#cbd5e1" },
        });
      });
    }

    return { nodes: newNodes, edges: newEdges };
  }, [evidence, findings, report, highlightedRefs]);

  const isEmpty = nodes.length === 0;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <h3 className="text-sm font-semibold text-foreground">证据链</h3>
        <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
          {(["log", "trace", "browser_error", "api_response", "user_report"] as SignalSource[]).map(
            (src) => (
              <span key={src} className="flex items-center gap-1">
                <span
                  className="inline-block size-2 rounded-full"
                  style={{ backgroundColor: SOURCE_COLOR[src] }}
                />
                {SOURCE_LABEL[src]}
              </span>
            ),
          )}
        </div>
      </div>
      {isEmpty ? (
        <div className="flex flex-1 items-center justify-center p-8 text-center">
          <p className="text-sm text-muted-foreground">
            诊断开始后，证据节点将在此显示
          </p>
        </div>
      ) : (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnScroll
          panOnDrag
          minZoom={0.3}
          maxZoom={1.5}
        >
          <Background gap={20} size={1} color="#e2e8f0" />
          <Controls showInteractive={false} />
        </ReactFlow>
      )}
    </div>
  );
}
