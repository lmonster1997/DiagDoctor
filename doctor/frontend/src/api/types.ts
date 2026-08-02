/**
 * TypeScript types for the doctor frontend.
 *
 * Trimmed to the types actually consumed by the UI (diagnosis report +
 * findings + case-level feedback). The backend exposes more schemas (budget,
 * normalized evidence, eval runs/cases, SSE events) but those are no longer
 * used by the frontend after the 证据链 / 进度 / 评测 panels were removed.
 * Re-expand when the backend redo lands new frontend contracts.
 */

// ── Diagnosis Report ─────────────────────────────────────────────

export type ServiceTier = "frontend" | "backend";
export type RootCauseTier = "frontend" | "backend" | "data";

export interface DiagnosisReport {
  primary_category: string;
  categories: string[];
  symptom_tier: ServiceTier;
  root_cause_tier: RootCauseTier;
  root_cause: string;
  affected_file: string | null;
  affected_line: number | null;
  fix_suggestion: string;
  /** Ordered chain of evidence_ref strings grounding the root cause. */
  evidence_chain: string[];
  confidence: number; // 0.0 – 1.0
  early_stopped: boolean;
  notes: string;
  /** §8.1 path 2: historical case_ids the agent declared it referenced (clamped
   *  to ⊆ retrieved_case_ids server-side). Drives the per-case "有帮助" UI. */
  referenced_case_ids: string[];
}

// ── Findings ─────────────────────────────────────────────────────

export interface Finding {
  agent: string;
  summary: string;
  evidence_refs: string[];
  affected_files: string[];
  fix_suggestion: string;
  confidence: number; // 0.0 – 1.0
  cross_layer: boolean;
  contradiction: boolean;
}

// ── Diagnosis case-level feedback (§8.1 path 2) ──────────────────

/** Body for POST /api/feedback/{run_id}/case -- mark a referenced case. */
export interface CaseFeedbackRequest {
  case_id: string;
  /** true = 有帮助 (backfill effectiveness +delta); false = 没帮助 (log only). */
  helpful: boolean;
}

/** Response from POST /api/feedback/{run_id}/case. */
export interface CaseFeedbackResponse {
  ok: boolean;
  run_id: string;
  case_id: string;
  helpful: boolean;
}
