/**
 * REST API client for DiagDoctor doctor frontend.
 *
 * Used for evaluation/cases/feedback REST endpoints (NOT for CopilotKit
 * streaming — that goes through the CopilotKit runtime via AG-UI protocol).
 */

// 默认相对路径 -> 经 vite dev 代理(/api -> :8001)同源无 CORS,与 CopilotKit 的
// runtimeUrl="/api/copilotkit"(相对)一致。生产分源时设 VITE_DOCTOR_API_URL。
import type { CaseFeedbackResponse } from "./types";

const BASE_URL = import.meta.env.VITE_DOCTOR_API_URL || "";

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { body, headers: initHeaders, ...rest } = options;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(initHeaders as Record<string, string>),
  };

  const res = await fetch(`${BASE_URL}${path}`, {
    ...rest,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const errBody = await res.json();
      detail = errBody.detail || detail;
    } catch {
      /* use statusText fallback */
    }
    throw new ApiError(res.status, detail);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}

// ── Diagnosis threads (#5 F3 HITL history) ───────────────────────

/** A single diagnosis thread summary from GET /api/diagnose/threads. */
export interface DiagnosisThread {
  thread_id: string;
  case_id: string | null;
  /** "paused" = mid-graph (awaiting HITL guidance, resumable); "completed" = END with report; "empty" = no values. */
  status: "paused" | "completed" | "empty";
  early_stopped: boolean;
  hitl_resumed: boolean;
  findings_count: number;
  has_report: boolean;
  /** Pending graph node names (non-empty while paused). */
  next: string[];
}

export interface ThreadsResponse {
  threads: DiagnosisThread[];
}

/** List recent diagnosis threads (paused first). Enabler for the F3 history list. */
export function listThreads(limit = 50): Promise<ThreadsResponse> {
  return apiFetch<ThreadsResponse>(`/api/diagnose/threads?limit=${limit}`);
}

// ── Diagnosis case-level feedback (§8.1 path 2) ──────────────────

/**
 * Mark a referenced historical case helpful/not-helpful (§8.1 path 2).
 * POST /api/feedback/{run_id}/case -- backend validates `case_id ∈
 * report.referenced_case_ids` (404 no report / 422 not referenced).
 * `runId` is the backend LangGraph thread_id (= `state.case_id` in the
 * CopilotKit flow, NOT `useCopilotContext().threadId` which is out of sync).
 */
export function postCaseFeedback(
  runId: string,
  caseId: string,
  helpful: boolean,
): Promise<CaseFeedbackResponse> {
  return apiFetch<CaseFeedbackResponse>(`/api/feedback/${runId}/case`, {
    method: "POST",
    body: { case_id: caseId, helpful },
  });
}
