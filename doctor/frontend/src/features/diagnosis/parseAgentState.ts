/**
 * Parse the diagnosis result out of the CopilotKit CoAgent state.
 *
 * Why this exists: the CopilotKit runtime invokes the *inner* `create_agent`
 * ReAct subgraph directly (see backend `main.py` `_DiagDoctorAgent`), which
 * bypasses the outer `ingest → diagnosis_agent → reporter` graph. That means
 * the synced `useCoAgent` state is the inner agent's `{messages}` state — it
 * does NOT contain the parsed `report` / `findings` / `evidence` fields that
 * `diagnosis_agent_node` and the reporter normally produce.
 *
 * The DiagnosisReport is emitted by the agent as JSON inside the final AI
 * message (per the system prompt). This module mirrors the backend parsing
 * logic (`parsing.py: parse_diagnosis_report` / `extract_findings`) so the
 * frontend can recover the report + findings from `state.messages`, and
 * synthesises a reduced `NormalizedEvidence` (golden_signals derived from
 * the union of `evidence_ref`s) so the EvidenceChainGraph has nodes to render.
 *
 * If the backend ever exposes the outer graph (with real `report`/`findings`/
 * `evidence` in state), this parser detects those fields directly and uses
 * them as-is, so both flows are supported.
 */
import type {
  DiagnosisReport,
  Finding,
  NormalizedEvidence,
  Signal,
} from "@/api/types";

// ── Types ─────────────────────────────────────────────────────────

interface AgentMessage {
  /** AG-UI / OpenAI style */
  role?: string;
  /** LangChain style */
  type?: string;
  /** Content may be a plain string or an array of content blocks. */
  content?: string | Array<{ text?: string; type?: string }>;
  /** LangChain / Python serialisation */
  tool_calls?: unknown[] | null | undefined;
  /** CopilotKit / AG-UI chat message serialisation (camelCase) */
  toolCalls?: unknown[] | null | undefined;
}

export interface RawAgentState {
  messages?: AgentMessage[];
  report?: DiagnosisReport | null;
  findings?: Finding[];
  evidence?: NormalizedEvidence | null;
  budget?: unknown;
  budget_ticks?: unknown[];
  /** §6.5 injection block (all retrieved cases w/ content + [id:...]) - synced
   *  from DoctorState, surfaced in the UI so the user can see referenced cases. */
  similar_cases_text?: string;
  [key: string]: unknown;
}

export interface ParsedDiagnosis {
  report: DiagnosisReport | null;
  findings: Finding[];
  evidence: NormalizedEvidence | null;
}

// ── Message helpers ───────────────────────────────────────────────

function messageText(msg: AgentMessage): string {
  const c = msg.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
      .join("\n");
  }
  return "";
}

function isAIMessage(msg: AgentMessage): boolean {
  const role = msg.role?.toLowerCase();
  const type = msg.type?.toLowerCase();
  return role === "assistant" || type === "ai" || type === "aimessage";
}

function hasToolCalls(msg: AgentMessage): boolean {
  const tc = msg.tool_calls ?? msg.toolCalls;
  return Array.isArray(tc) && tc.length > 0;
}

// ── JSON extraction (ports parsing.py _extract_json_from_text) ────

const FENCE_RE = /```(?:json)?\s*\n?([\s\S]*?)\n?```/g;

export function extractJsonFromText(text: string): Record<string, unknown> | null {
  if (!text) return null;

  // 1. Markdown code fences
  const fences = [...text.matchAll(FENCE_RE)];
  for (const m of fences) {
    const candidate = m[1];
    const parsed = tryParseJson(candidate);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  }

  // 2. Brace-depth tracking (last balanced object first)
  const byDepth = extractJsonByDepth(text);
  if (byDepth) return byDepth;

  // 3. Whole text
  const whole = tryParseJson(text);
  if (whole && typeof whole === "object" && !Array.isArray(whole)) {
    return whole as Record<string, unknown>;
  }
  return null;
}

function tryParseJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function extractJsonByDepth(text: string): Record<string, unknown> | null {
  const candidates: Array<[number, number]> = [];
  let depth = 0;
  let inString = false;
  let escape = false;
  let start = -1;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (ch === "{") {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === "}") {
      depth--;
      if (depth === 0 && start !== -1) {
        candidates.push([start, i + 1]);
        start = -1;
      }
    }
  }

  if (candidates.length === 0) return null;
  // Try last-first (LLMs put JSON after reasoning).
  for (let i = candidates.length - 1; i >= 0; i--) {
    const [s, e] = candidates[i];
    const parsed = tryParseJson(text.slice(s, e));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  }
  return null;
}

// ── Report / findings parsing ─────────────────────────────────────

function ensureStrList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => String(v));
  if (typeof value === "string" && value) return [value];
  return [];
}

function asNum(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function asStr(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : value == null ? fallback : String(value);
}

function buildReport(data: Record<string, unknown>, fallbackText: string): DiagnosisReport {
  const hasReportFields =
    "root_cause" in data ||
    "primary_category" in data ||
    "fix_suggestion" in data ||
    "confidence" in data;

  if (!hasReportFields && fallbackText) {
    return {
      primary_category: "",
      categories: [],
      symptom_tier: "backend",
      root_cause_tier: "backend",
      root_cause: fallbackText.slice(0, 500),
      affected_file: null,
      affected_line: null,
      fix_suggestion: "",
      evidence_chain: [],
      confidence: 0.2,
      early_stopped: false,
      notes: "JSON 解析失败，使用原始输出作为 root_cause",
      referenced_case_ids: [],
    };
  }

  return {
    primary_category: asStr(data.primary_category),
    categories: ensureStrList(data.categories),
    symptom_tier: (asStr(data.symptom_tier, "backend") as DiagnosisReport["symptom_tier"]),
    root_cause_tier: (asStr(data.root_cause_tier, "backend") as DiagnosisReport["root_cause_tier"]),
    root_cause: asStr(data.root_cause),
    affected_file: data.affected_file == null ? null : asStr(data.affected_file) || null,
    affected_line:
      data.affected_line == null ? null : Math.trunc(asNum(data.affected_line, 0)) || null,
    fix_suggestion: asStr(data.fix_suggestion),
    evidence_chain: ensureStrList(data.evidence_chain),
    confidence: Math.max(0, Math.min(1, asNum(data.confidence, 0.5))),
    early_stopped: Boolean(data.early_stopped),
    notes: asStr(data.notes),
    // §8.1 path 2: parsed raw from the agent JSON (unclamped here). When the
    // outer-graph state.report is synced, parseAgentState returns THAT (server
    // clamped) value directly; this messages-fallback path is only used when
    // state.report is absent. The backend endpoint re-validates against the
    // checkpoint's clamped set regardless.
    referenced_case_ids: ensureStrList(data.referenced_case_ids),
  };
}

function parseReportFromMessages(messages: AgentMessage[]): DiagnosisReport | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (!isAIMessage(msg) || hasToolCalls(msg)) continue;
    const text = messageText(msg);
    const data = extractJsonFromText(text);
    if (data) return buildReport(data, text);
  }
  return null;
}

function parseFindingsFromMessages(messages: AgentMessage[]): Finding[] {
  const findings: Finding[] = [];
  for (const msg of messages) {
    if (!isAIMessage(msg) || hasToolCalls(msg)) continue;
    const data = extractJsonFromText(messageText(msg));
    if (!data) continue;
    if (!("summary" in data) && !("root_cause" in data)) continue;

    const evRefs = Array.isArray(data.evidence_refs) && data.evidence_refs.length
      ? data.evidence_refs
      : data.evidence_chain;
    const affFiles = Array.isArray(data.affected_files) && data.affected_files.length
      ? data.affected_files
      : data.affected_file != null
        ? [data.affected_file]
        : [];

    findings.push({
      agent: asStr(data.agent, "diagnosis_agent"),
      summary: asStr(data.summary, asStr(data.root_cause)),
      evidence_refs: ensureStrList(evRefs),
      affected_files: ensureStrList(affFiles),
      fix_suggestion: asStr(data.fix_suggestion),
      confidence: Math.max(0, Math.min(1, asNum(data.confidence, 0.5))),
      cross_layer: Boolean(data.cross_layer),
      contradiction: Boolean(data.contradiction),
    });
  }
  return findings;
}

// ── Evidence synthesis ────────────────────────────────────────────

/**
 * Build a reduced NormalizedEvidence from the parsed report + findings.
 *
 * Without the ingest node there are no real golden_signals/correlations, so
 * we synthesise one Signal node per distinct `evidence_ref` mentioned in the
 * report's `evidence_chain` and the findings' `evidence_refs`. This gives the
 * EvidenceChainGraph a left column to render and lets the evidence_chain
 * highlight path resolve.
 */
function synthesizeEvidence(
  report: DiagnosisReport | null,
  findings: Finding[],
): NormalizedEvidence | null {
  const refs: string[] = [];
  if (report) refs.push(...report.evidence_chain);
  for (const f of findings) refs.push(...f.evidence_refs);

  const seen = new Set<string>();
  const signals: Signal[] = [];
  for (const ref of refs) {
    if (!ref || seen.has(ref)) continue;
    seen.add(ref);
    signals.push({
      signal_id: ref,
      source: "user_report",
      signal_type: "behavior_mismatch",
      service_tier: "backend",
      severity: "info",
      summary: ref,
      evidence_ref: ref,
      timestamp: null,
      metadata: { synthetic: true },
    });
  }

  if (!report && signals.length === 0 && findings.length === 0) return null;
  return {
    user_report: "",
    golden_signals: signals,
    timeline: [],
    correlations: [],
    raw_refs: {},
    noise_ratio: 0,
    trigger_time: null,
    trigger_trace_ids: [],
    frontend_span_count: 0,
    backend_span_count: 0,
    metadata: { synthetic: true },
  };
}

// ── Public API ────────────────────────────────────────────────────

/**
 * Recover the diagnosis report/findings/evidence for the UI.
 *
 * @param state    The `useCoAgent` state (may carry direct `report`/`findings`/
 *                 `evidence` fields if the outer graph is exposed, or `messages`
 *                 if the inner agent state is synced verbatim).
 * @param chatMessages  The CopilotKit chat messages (`useCopilotMessagesContext`).
 *                      This is the reliable source in the current architecture:
 *                      CopilotKit invokes the inner `create_agent` directly, so
 *                      the report lives in the final assistant chat message as
 *                      JSON. Pass `undefined` to rely solely on `state`.
 */
export function parseAgentState(
  state: RawAgentState | undefined | null,
  chatMessages?: AgentMessage[] | null,
): ParsedDiagnosis {
  // Direct fields (outer-graph flow / future-proofing) take precedence.
  if (state?.report || state?.findings?.length || state?.evidence) {
    return {
      report: state.report ?? null,
      findings: state.findings ?? [],
      evidence: state.evidence ?? null,
    };
  }

  // Prefer the chat messages (reliable in the inner-agent flow), then fall
  // back to any messages carried in the synced agent state.
  const messages = chatMessages?.length ? chatMessages : state?.messages ?? [];
  const report = parseReportFromMessages(messages);
  const findings = parseFindingsFromMessages(messages);
  const evidence = synthesizeEvidence(report, findings);
  return { report, findings, evidence };
}
