/**
 * EvidenceChainGraph — "侦探墙上的线索板" 因果推理可视化.
 *
 * Layout reimagined: not a database ER diagram, but a detective's clue board.
 *   - Signal cards: colour-coded pushpin notes with slight rotation
 *   - Correlation notes: dashed yellow sticky-notes connecting signals
 *   - Finding cards: evidence dossiers with paperclip
 *   - Report card: glowing "CASE SOLVED" card with golden border
 *
 * Edges use smoothstep curves ("red string" metaphor), with correlation
 * coefficients as edge labels. Click an evidence_ref → smooth zoom to the
 * related nodes ("聚焦叙事").
 */
import { useMemo, useCallback, useEffect } from "react";
import ReactFlow, {
  Background,
  Controls,
  BackgroundVariant,
  ReactFlowProvider,
  useReactFlow,
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

// ── Layout constants ──────────────────────────────────────────────
const COL_SIGNAL = 0;
const COL_CORRELATION = 340;
const COL_FINDING = 680;
const COL_REPORT = 1020;
const ROW_HEIGHT = 120;
const NODE_WIDTH = 260;

// ── Source colour map ─────────────────────────────────────────────
const SOURCE_COLOR: Record<SignalSource, string> = {
  log: "#f59e0b",
  trace: "#3b82f6",
  browser_error: "#a855f7",
  api_response: "#22c55e",
  user_report: "#64748b",
};

const SOURCE_LABEL: Record<SignalSource, string> = {
  log: "Log",
  trace: "Trace",
  browser_error: "Browser",
  api_response: "API",
  user_report: "User",
};

const SOURCE_ICON: Record<SignalSource, string> = {
  log: "📜",
  trace: "🔗",
  browser_error: "🌐",
  api_response: "📡",
  user_report: "💬",
};

// ── Pseudo-random rotation for "pinned note" look ─────────────────
function pinRotation(index: number): number {
  // Deterministic slight rotation per card (-3° to +3°)
  const seed = (index * 137 + 42) % 7;
  return seed - 3;
}

// ── Node data types ───────────────────────────────────────────────
interface SignalNodeData {
  signal: Signal;
  index: number;
  highlighted: boolean;
  faded: boolean;
}
interface CorrelationNodeData {
  correlation: Correlation;
  highlighted: boolean;
  faded: boolean;
}
interface FindingNodeData {
  finding: Finding;
  index: number;
  highlighted: boolean;
  faded: boolean;
}
interface ReportNodeData {
  report: DiagnosisReport;
}

// ═══════════════════════════════════════════════════════════════════
// Custom Node Renderers — "Clue Board" style
// ═══════════════════════════════════════════════════════════════════

/** Signal card — pushpin note with colour-coded left tab */
function SignalNode({ data }: NodeProps<SignalNodeData>) {
  const { signal, index, highlighted, faded } = data;
  const color = SOURCE_COLOR[signal.source] ?? "#64748b";
  const rotation = pinRotation(index);

  return (
    <div
      className="relative transition-all duration-500 ease-out"
      style={{
        width: NODE_WIDTH,
        opacity: faded ? 0.2 : 1,
        transform: `rotate(${rotation}deg)`,
        filter: faded ? "grayscale(0.6)" : undefined,
      }}
    >
      {/* Pushpin */}
      <div
        className="absolute -top-2 left-1/2 z-10 -translate-x-1/2"
        style={{ filter: `drop-shadow(0 2px 2px rgba(0,0,0,0.5))` }}
      >
        <div
          className="flex size-7 items-center justify-center rounded-full text-xs shadow-md"
          style={{
            background: `radial-gradient(circle at 40% 35%, ${color}cc, ${color}66)`,
            border: `2px solid ${color}`,
          }}
        >
          📌
        </div>
      </div>

      {/* Card body */}
      <div
        className="rounded-lg border bg-[#1a1d25] p-3 pt-5 text-left shadow-lg transition-all duration-300"
        style={{
          borderColor: highlighted ? color : "rgba(255,255,255,0.08)",
          borderLeftWidth: 4,
          borderLeftColor: color,
          boxShadow: highlighted
            ? `0 0 18px ${color}22, 0 4px 12px rgba(0,0,0,0.4)`
            : "0 2px 8px rgba(0,0,0,0.3)",
        }}
      >
        {/* Source badge */}
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="text-xs">{SOURCE_ICON[signal.source]}</span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
            style={{ backgroundColor: `${color}20`, color }}
          >
            {SOURCE_LABEL[signal.source]}
          </span>
          {signal.severity === "error" && (
            <span className="ml-auto rounded bg-red-500/15 px-1.5 py-0.5 text-[9px] font-medium text-red-400">
              ERROR
            </span>
          )}
        </div>

        {/* Signal title */}
        <div className="text-[12px] font-semibold leading-snug text-[#e4e4ef]">
          {signal.signal_id || signal.signal_type}
        </div>

        {/* Summary */}
        <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-[#8a8fa3]">
          {signal.summary || "—"}
        </p>

        {/* Timestamp */}
        {signal.timestamp && (
          <div className="mt-1.5 font-mono text-[9px] text-[#5c6070]">
            {new Date(signal.timestamp).toLocaleTimeString("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/** Correlation note — yellow sticky, dashed border, with confidence */
function CorrelationNode({ data }: NodeProps<CorrelationNodeData>) {
  const { correlation, highlighted, faded } = data;
  const confPct = Math.round(correlation.confidence * 100);

  return (
    <div
      className="relative transition-all duration-500 ease-out"
      style={{
        width: NODE_WIDTH,
        opacity: faded ? 0.2 : 1,
        transform: `rotate(1.5deg)`,
      }}
    >
      {/* Paperclip */}
      <div className="absolute -right-1 -top-3 z-10 text-[#8a8fa3] opacity-60 text-lg rotate-12">
        📎
      </div>

      <div
        className="rounded-lg border-2 border-dashed bg-[#1e1b18] p-3 text-left shadow-md transition-all duration-300"
        style={{
          borderColor: highlighted ? "#f59e0b" : "rgba(245,158,11,0.15)",
          boxShadow: highlighted
            ? "0 0 16px rgba(245,158,11,0.12), 0 2px 8px rgba(0,0,0,0.4)"
            : "0 2px 6px rgba(0,0,0,0.3)",
        }}
      >
        {/* Header */}
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="text-xs">🧩</span>
          <span className="text-[11px] font-semibold text-[#f59e0b]">
            {correlation.correlation_id || "关联发现"}
          </span>
        </div>

        {/* Description */}
        <p className="line-clamp-2 text-[11px] leading-relaxed text-[#a8a29e]">
          {correlation.description || "跨层信号关联"}
        </p>

        {/* Confidence badge */}
        <div className="mt-2 flex items-center gap-2">
          <div className="h-1.5 flex-1 rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full bg-[#f59e0b] transition-all duration-500"
              style={{ width: `${confPct}%` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-[#8a8fa3]">
            {confPct}%
          </span>
        </div>
      </div>
    </div>
  );
}

/** Finding card — evidence dossier with paperclip */
function FindingNode({ data }: NodeProps<FindingNodeData>) {
  const { finding, index, highlighted, faded } = data;
  const confPct = Math.round(finding.confidence * 100);
  const rotation = pinRotation(index + 100); // different seed

  return (
    <div
      className="relative transition-all duration-500 ease-out"
      style={{
        width: NODE_WIDTH,
        opacity: faded ? 0.2 : 1,
        transform: `rotate(${rotation}deg)`,
      }}
    >
      <div
        className="rounded-lg border bg-[#131b17] p-3 text-left shadow-lg transition-all duration-300"
        style={{
          borderColor: highlighted ? "#22c55e" : "rgba(34,197,94,0.12)",
          borderLeftWidth: 4,
          borderLeftColor: "#22c55e",
          boxShadow: highlighted
            ? "0 0 20px rgba(34,197,94,0.18), 0 4px 12px rgba(0,0,0,0.4)"
            : "0 2px 8px rgba(0,0,0,0.3)",
        }}
      >
        {/* Header */}
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="text-xs">🔎</span>
          <span className="text-[11px] font-semibold text-[#e4e4ef]">
            发现 #{index + 1}
          </span>
          {finding.agent && (
            <span className="ml-auto rounded bg-white/[0.04] px-1.5 py-0.5 text-[9px] text-[#5c6070]">
              {finding.agent}
            </span>
          )}
          {finding.cross_layer && (
            <span className="rounded bg-purple-500/15 px-1.5 py-0.5 text-[9px] text-purple-400">
              跨层
            </span>
          )}
        </div>

        {/* Summary */}
        <p className="line-clamp-2 text-[11px] leading-relaxed text-[#8a8fa3]">
          {finding.summary}
        </p>

        {/* Confidence bar */}
        <div className="mt-2 flex items-center gap-2">
          <div className="h-1.5 flex-1 rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full bg-[#22c55e] transition-all duration-500"
              style={{ width: `${confPct}%` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-[#8a8fa3]">
            {confPct}%
          </span>
        </div>
      </div>
    </div>
  );
}

/** Report node — "CASE SOLVED" terminal card with golden glow */
function ReportNode({ data }: NodeProps<ReportNodeData>) {
  const { report } = data;
  const confPct = Math.round(report.confidence * 100);

  return (
    <div
      className="relative"
      style={{ width: NODE_WIDTH + 20 }}
    >
      {/* Glow aura */}
      <div
        className="absolute -inset-3 rounded-xl opacity-30 blur-xl transition-all"
        style={{ background: "radial-gradient(ellipse, #f59e0b44, transparent 70%)" }}
      />

      <div
        className="relative rounded-xl border-2 bg-[#1a1814] p-4 text-left shadow-2xl"
        style={{
          borderColor: "#f59e0b",
          boxShadow: "0 0 30px rgba(245,158,11,0.15), 0 0 8px rgba(245,158,11,0.2), 0 4px 16px rgba(0,0,0,0.5)",
        }}
      >
        {/* Status stamp — not "SOLVED" but an open investigation */}
        <div className="absolute -right-2 -top-3 rotate-12 rounded border-2 border-[#3b82f6]/40 bg-[#3b82f6]/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-[#3b82f6]">
          分析中
        </div>

        <div className="mb-2 flex items-center gap-2 text-[13px] font-bold text-[#f59e0b]">
          <span>🔍</span>
          初步分析
        </div>

        {/* Category */}
        <div className="mb-2 text-[10px] text-[#8a8fa3]">
          {report.primary_category || "未分类"}
        </div>

        {/* Root cause */}
        <p className="line-clamp-3 text-[12px] leading-snug text-[#e4e4ef]">
          {report.root_cause || "（未识别）"}
        </p>

        {/* Confidence */}
        <div className="mt-3 flex items-center gap-2">
          <div className="h-2 flex-1 rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full bg-gradient-to-r from-[#f59e0b] to-[#ef4444] transition-all duration-500"
              style={{ width: `${confPct}%` }}
            />
          </div>
          <span className="font-mono text-[10px] font-semibold tabular-nums text-[#f59e0b]">
            {confPct}%
          </span>
        </div>

        {/* Evidence chain count */}
        {report.evidence_chain.length > 0 && (
          <div className="mt-2 font-mono text-[9px] text-[#5c6070]">
            {report.evidence_chain.length} 条证据
          </div>
        )}
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
  highlightedRefs?: Set<string>;
}

// ═══════════════════════════════════════════════════════════════════
// Main Component (outer — provides ReactFlowProvider context)
// ═══════════════════════════════════════════════════════════════════
export function EvidenceChainGraph({
  evidence,
  findings,
  report,
  highlightedRefs,
}: EvidenceChainGraphProps) {
  return (
    <ReactFlowProvider>
      <EvidenceChainGraphInner
        evidence={evidence}
        findings={findings}
        report={report}
        highlightedRefs={highlightedRefs}
      />
    </ReactFlowProvider>
  );
}

// ═══════════════════════════════════════════════════════════════════
// Inner Component (child of ReactFlowProvider — can use useReactFlow)
// ═══════════════════════════════════════════════════════════════════
function EvidenceChainGraphInner({
  evidence,
  findings,
  report,
  highlightedRefs,
}: EvidenceChainGraphProps) {
  const reactFlowInstance = useReactFlow();

  const { nodes, edges } = useMemo(() => {
    const signals = evidence?.golden_signals ?? [];
    const correlations = evidence?.correlations ?? [];

    const chainRefs = new Set<string>(report?.evidence_chain ?? []);
    const focusRefs = highlightedRefs ?? new Set<string>();
    const allHighlighted = new Set<string>([...chainRefs, ...focusRefs]);

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

    // ── Column 1: Signal cards ───────────────────────────────
    signals.forEach((signal, i) => {
      const ref = signal.evidence_ref || signal.signal_id;
      const highlighted = isRefHighlighted(ref);
      newNodes.push({
        id: `sig:${signal.signal_id || i}`,
        type: "signal",
        position: { x: COL_SIGNAL, y: i * ROW_HEIGHT },
        data: { signal, index: i, highlighted, faded: isRefFaded(ref) },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      });
    });

    // ── Column 2: Correlation sticky-notes ───────────────────
    correlations.forEach((corr, i) => {
      const linked = [
        ...(corr.frontend_signals ?? []),
        ...(corr.backend_signals ?? []),
        ...(corr.db_signals ?? []),
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

      // Edges: signal → correlation ("red string" curves)
      for (const sid of linked) {
        const confLabel =
          corr.confidence != null ? `${Math.round(corr.confidence * 100)}%` : undefined;
        newEdges.push({
          id: `e:${sid}->${id}`,
          source: `sig:${sid}`,
          target: id,
          type: "smoothstep",
          animated: highlighted,
          label: confLabel,
          labelStyle: {
            fill: highlighted ? "#f59e0b" : "rgba(255,255,255,0.25)",
            fontSize: 9,
            fontFamily: "JetBrains Mono, monospace",
            fontWeight: 500,
          },
          labelBgStyle: { fill: "transparent" },
          style: {
            stroke: highlighted ? "#f59e0b" : "rgba(255,255,255,0.10)",
            strokeWidth: highlighted ? 2.5 : 1,
            strokeDasharray: highlighted ? undefined : "6 4",
          },
        });
      }
    });

    // ── Column 3: Finding dossiers ───────────────────────────
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

      // Edges: signal → finding (dashed or solid based on confidence)
      for (const ref of finding.evidence_refs) {
        const sigs = signalsByRef.get(ref) ?? [];
        for (const s of sigs) {
          const edgeHi = isRefHighlighted(ref);
          newEdges.push({
            id: `e:sig:${s.signal_id}->${id}`,
            source: `sig:${s.signal_id}`,
            target: id,
            type: "smoothstep",
            animated: edgeHi,
            style: {
              stroke: edgeHi ? "#22c55e" : "rgba(255,255,255,0.08)",
              strokeWidth: edgeHi ? 2 : 1,
              strokeDasharray: edgeHi ? undefined : "8 4",
            },
          });
        }
      }
    });

    // ── Column 4: Report (terminal) ──────────────────────────
    if (report) {
      // Place report vertically centered among findings
      const reportY = findings.length > 0
        ? ((findings.length - 1) * ROW_HEIGHT) / 2
        : 0;
      newNodes.push({
        id: "report",
        type: "report",
        position: { x: COL_REPORT, y: reportY },
        data: { report },
        targetPosition: Position.Left,
      });

      // Edges: finding → report (only chain-path findings highlighted)
      findings.forEach((finding, fi) => {
        const onChain = finding.evidence_refs.some((r) => chainRefs.has(r));
        newEdges.push({
          id: `e:find:${fi}->report`,
          source: `find:${fi}`,
          target: "report",
          type: "smoothstep",
          animated: onChain,
          style: {
            stroke: onChain ? "#f59e0b" : "rgba(255,255,255,0.06)",
            strokeWidth: onChain ? 2.5 : 1,
            strokeDasharray: onChain ? undefined : "6 4",
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: onChain ? "#f59e0b" : "rgba(255,255,255,0.2)",
            width: 14,
            height: 14,
          },
        });
      });
    }

    return { nodes: newNodes, edges: newEdges };
  }, [evidence, findings, report, highlightedRefs]);

  // ── Focus narrative: smooth zoom to highlighted nodes ───────
  const onInit = useCallback(() => {
    setTimeout(() => {
      reactFlowInstance?.fitView({ padding: 0.2, duration: 600 });
    }, 100);
  }, [reactFlowInstance]);

  // When highlightedRefs changes (user clicks evidence_ref in report),
  // zoom to the highlighted nodes for "聚焦叙事".
  useEffect(() => {
    const currentHighlightCount = highlightedRefs?.size ?? 0;
    if (currentHighlightCount > 0 && nodes.length > 0) {
      const highlightedNodeIds = nodes
        .filter((n) => {
          const d = n.data as Record<string, unknown>;
          return d.highlighted === true;
        })
        .map((n) => n.id);
      if (highlightedNodeIds.length > 0) {
        // Delay to let ReactFlow finish rendering fade transitions
        const timer = setTimeout(() => {
          reactFlowInstance?.fitView({
            nodes: highlightedNodeIds,
            padding: 0.4,
            duration: 800,
            maxZoom: 1.2,
          });
        }, 300);
        return () => clearTimeout(timer);
      }
    }
  }, [highlightedRefs, nodes, reactFlowInstance]);

  const isEmpty = nodes.length === 0;

  return (
    <div className="flex h-full flex-col bg-[#0d0f12]">
      {/* Legend bar */}
      <div className="flex shrink-0 items-center justify-between border-b border-white/[0.06] px-4 py-2">
        <h3 className="text-sm font-semibold text-[#e4e4ef]">
          🕵️ 证据链
        </h3>
        <div className="flex items-center gap-3 text-[10px] text-[#8a8fa3]">
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
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          <span className="text-3xl opacity-30">🔍</span>
          <p className="text-sm text-[#5c6070]">
            诊断开始后，证据节点将在此显示
          </p>
          <p className="text-[11px] text-[#5c6070]/60">
            信号 → 关联 → 发现 → 报告
          </p>
        </div>
      ) : (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onInit={onInit}
          fitView
          fitViewOptions={{ padding: 0.2, duration: 600 }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnScroll
          panOnDrag
          minZoom={0.25}
          maxZoom={1.8}
          proOptions={{ hideAttribution: true }}
          defaultEdgeOptions={{
            type: "smoothstep",
          }}
        >
          {/* Cork-board style dot grid */}
          <Background
            variant={BackgroundVariant.Dots}
            gap={18}
            size={1.2}
            color="rgba(255,255,255,0.045)"
          />
          <Controls
            showInteractive={false}
            className="[&>button]:!bg-[#13161b] [&>button]:!border-white/[0.06] [&>button]:!text-[#8a8fa3] [&>button:hover]:!bg-white/[0.06] [&>button>svg]:!fill-[#8a8fa3]"
          />
        </ReactFlow>
      )}
    </div>
  );
}
