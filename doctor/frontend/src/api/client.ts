/**
 * REST API client for DiagDoctor doctor frontend.
 *
 * Used for evaluation/cases/feedback REST endpoints (NOT for CopilotKit
 * streaming — that goes through the CopilotKit runtime via AG-UI protocol).
 */

// 默认相对路径 -> 经 vite dev 代理(/api -> :8001)同源无 CORS,与 CopilotKit 的
// runtimeUrl="/api/copilotkit"(相对)一致。生产分源时设 VITE_DOCTOR_API_URL。
import type { CaseFeedbackResponse, DiagnosisThreadDetail } from "./types";

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
  /** P2 复诊轮次:1=初诊,>1=第 N 轮复诊。前端历史列表据此标"第 N 轮"。 */
  round: number;
  /** P2 上限门:超出 MAX_ROUNDS 后置真,前端据此隐藏"追加诊断"入口。 */
  rounds_exhausted: boolean;
}

export interface ThreadsResponse {
  threads: DiagnosisThread[];
}

/** List recent diagnosis threads (paused first). Enabler for the F3 history list. */
export function listThreads(limit = 50): Promise<ThreadsResponse> {
  return apiFetch<ThreadsResponse>(`/api/diagnose/threads?limit=${limit}`);
}

/** GET /api/diagnose/threads/{thread_id} -- full report of a completed or
 *  paused thread (P0 historical report view, see docs/hitl-evolution-plan.md §3). */
export function getThread(threadId: string): Promise<DiagnosisThreadDetail> {
  return apiFetch<DiagnosisThreadDetail>(`/api/diagnose/threads/${threadId}`);
}

// ── P2 复诊:历史消息回放(追加诊断时左侧聊天回填)──────────────────

/** AG-UI chat message (subset of the ag-ui protocol; backend serializes via
 *  langchain_messages_to_agui + model_dump(by_alias=True) -> camelCase, matching
 *  @ag-ui/core's zod schema that CopilotKit v2 parses). CopilotKit's Message
 *  type isn't a public export, so we type loosely here and cast at the
 *  ``setMessages`` call site. NB: tool calls are ``toolCalls`` / ``toolCallId``
 *  (camelCase) -- NOT snake_case; sending snake_case silently drops them. */
export interface AGUIMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content?: string;
  toolCalls?: unknown[];
  toolCallId?: string;
  name?: string | null;
}

export interface ThreadMessagesResponse {
  thread_id: string;
  messages: AGUIMessage[];
}

/** GET /api/diagnose/threads/{tid}/messages -- AG-UI chat messages for history
 *  replay. P2: 追加诊断时前端 setMessages 回填(CopilotKit 无 persistence,
 *  setThreadId 不拉历史)。消息 id 与 checkpoint 一致 -> 后续 send 走复诊轮。 */
export function getThreadMessages(threadId: string): Promise<ThreadMessagesResponse> {
  return apiFetch<ThreadMessagesResponse>(`/api/diagnose/threads/${threadId}/messages`);
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
