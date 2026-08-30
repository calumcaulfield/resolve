/**
 * Server-side API client.
 *
 * Every call runs in a React Server Component or a Server Action. The API key
 * never reaches the browser, and the browser has no direct database access —
 * which is the specific mistake the portfolio audit found in the earlier
 * Proflow prototype, where the client held write authority over the payments
 * table.
 */

const API_URL = process.env.RESOLVE_API_URL ?? "http://localhost:8000"
const API_KEY = process.env.RESOLVE_API_KEY ?? "dev-local-key"

export type TicketStatus =
  | "received"
  | "triaged"
  | "in_progress"
  | "awaiting_approval"
  | "resolved"
  | "escalated"
  | "failed"

export interface Ticket {
  id: string
  external_id: string
  from_email: string
  subject: string
  body: string
  status: TicketStatus
  intent: string | null
  urgency: string | null
  order_ref: string | null
  created_at: string
}

export interface AgentStep {
  index: number
  kind: string
  summary: string
  detail: Record<string, unknown>
  duration_ms: number
  cost_usd: number
  /** agent | human | system — lets the trace distinguish who did what. */
  actor_type: "agent" | "human" | "system"
  actor: string | null
  created_at: string | null
}

export type ReplyState =
  | "none"
  | "provisional"
  | "awaiting_approval"
  | "sent"
  | "voided"
  | "held"

export interface AgentRun {
  id: string
  status: TicketStatus
  intent: string | null
  escalation_reason: string | null
  reply_subject: string | null
  reply_body: string | null
  /** A draft is not a sent message. This is the difference. */
  reply_state: ReplyState
  reply_state_reason: string | null
  grounded: boolean | null
  citation_count: number
  cost_usd: number
  tokens_in: number
  tokens_out: number
  duration_ms: number
  provider: string
  prompt_version: string
  created_at: string
  steps: AgentStep[]
}

export interface TicketDetail extends Ticket {
  runs: AgentRun[]
}

export type ApprovalOutcome = "pending" | "executed" | "execution_failed" | "not_executed"

export interface Approval {
  id: string
  ticket_id: string
  run_id: string | null
  tool: string
  arguments: Record<string, unknown>
  rationale: string
  risk: string
  resource: string | null
  amount_gbp: number | null
  decision: "pending" | "approved" | "rejected" | "expired"
  decided_by: string | null
  decided_by_type: string
  decided_at: string | null
  note: string | null
  /** What happened, as opposed to what was decided. */
  outcome: ApprovalOutcome
  executed: boolean
  execution_result: Record<string, unknown> | null
  created_at: string
}

export interface ApprovalContext {
  approval: Approval
  ticket_subject: string
  customer_email: string
  customer_message: string
  intent: string | null
  ai_rationale: string
  policy_citations: { chunk_id: string; title: string; score: string }[]
  proposed_reply_subject: string | null
  proposed_reply_body: string | null
  reply_state: ReplyState
}

export interface DecisionResult {
  approval: Approval
  ticket_id: string
  ticket_status: string
  reply_state: ReplyState
  reply_state_reason: string | null
  escalation_reason: string | null
  executed: boolean
  outcome: ApprovalOutcome
}

export interface ActionOversight {
  action: string
  proposed: number
  approved: number
  rejected: number
  pending: number
  approval_rate: number
}

export interface OversightMetrics {
  decisions_total: number
  pending: number
  approved: number
  rejected: number
  execution_failed: number
  approval_rate: number
  rejection_rate: number
  override_rate: number
  by_action: ActionOversight[]
  total_value_approved_gbp: number
  total_value_rejected_gbp: number
  median_time_to_decision_seconds: number | null
}

export interface AuditEvent {
  approval_id: string
  ticket_id: string
  action: string
  resource: string | null
  amount_gbp: number | null
  risk: string
  decision: string
  outcome: ApprovalOutcome
  actor: string | null
  actor_type: string
  note: string | null
  decided_at: string
  ticket_subject: string
}

export interface Analytics {
  tickets_total: number
  by_status: Record<string, number>
  by_intent: Record<string, number>
  auto_resolution_rate: number
  approval_rate: number
  escalation_rate: number
  groundedness_rate: number
  total_cost_usd: number
  mean_cost_usd: number
  mean_duration_ms: number
  pending_approvals: number
  projected_hours_saved: number
  assumptions: Record<string, number>
}

export interface ClassMetrics {
  label: string
  support: number
  precision: number
  recall: number
  f1: number
}

export interface EvalReport {
  provider: string
  prompt_version: string
  model_fast: string
  model_reasoning: string
  dataset: string
  generated_at: string
  total_cases: number
  passed: number
  pass_rate: number
  intent_accuracy: number
  intent_macro_f1: number
  per_intent: ClassMetrics[]
  confusion: Record<string, Record<string, number>>
  outcome_accuracy: number
  tool_precision: number
  tool_recall: number
  escalation_precision: number
  escalation_recall: number
  groundedness_rate: number
  citation_rate: number
  adversarial_cases: number
  adversarial_contained: number
  containment_rate: number
  mean_cost_usd: number
  total_cost_usd: number
  p50_latency_ms: number
  p95_latency_ms: number
  failures: string[]
}

export interface EvalReportResponse {
  report: EvalReport
  source: string
  available_providers: string[]
}

/** Why the evaluation page can be in three states rather than two. */
export type EvalState =
  | { kind: "ok"; data: EvalReportResponse }
  /** No evaluation has been run yet. An empty state, not a failure. */
  | { kind: "none" }
  /** A report exists but is malformed, or the API is unreachable. Shown as a
   *  distinct message so it is never mistaken for "no report yet". */
  | { kind: "error"; message: string }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "x-api-key": API_KEY,
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  })
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path} failed: ${response.status}`)
  }
  return (await response.json()) as T
}

export const api = {
  tickets: (status?: string) =>
    request<Ticket[]>(`/v1/tickets${status ? `?status_filter=${status}` : ""}`),
  ticket: (id: string) => request<TicketDetail>(`/v1/tickets/${id}`),
  approvals: (decision = "pending") =>
    request<Approval[]>(`/v1/approvals?decision=${decision}`),
  approval: (id: string) => request<ApprovalContext>(`/v1/approvals/${id}`),
  decide: (id: string, approve: boolean, decidedBy: string, note?: string) =>
    request<DecisionResult>(`/v1/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ approve, decided_by: decidedBy, note }),
    }),
  oversight: () => request<OversightMetrics>("/v1/oversight"),
  audit: (limit = 100) => request<AuditEvent[]>(`/v1/audit?limit=${limit}`),
  analytics: () => request<Analytics>("/v1/analytics"),

  /**
   * The latest evaluation report, from the API.
   *
   * This deliberately does not read the filesystem. The console image is built
   * from `./console` and does not contain `evals/`, so a filesystem read works
   * in local development and silently fails in Docker — which is exactly the
   * bug this replaced.
   *
   * A 404 is a legitimate empty state ("nobody has run `make eval` yet") and is
   * distinguished from a real failure, so the page never implies a run happened
   * when it did not.
   */
  evals: async (provider?: string): Promise<EvalState> => {
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : ""
    try {
      const response = await fetch(`${API_URL}/v1/evals/latest${query}`, {
        headers: { "x-api-key": API_KEY },
        cache: "no-store",
      })
      if (response.status === 404) return { kind: "none" }
      if (!response.ok) {
        const detail = await response.text()
        return { kind: "error", message: `API returned ${response.status}: ${detail}` }
      }
      return { kind: "ok", data: (await response.json()) as EvalReportResponse }
    } catch (error) {
      return {
        kind: "error",
        message: error instanceof Error ? error.message : "the API is unreachable",
      }
    }
  },
}

/** Returns null instead of throwing, so a page can render a "start the API"
 *  empty state rather than a stack trace. */
export async function safe<T>(fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn()
  } catch {
    return null
  }
}
