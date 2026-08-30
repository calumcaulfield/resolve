import Link from "next/link"
import { api, safe, type ApprovalContext } from "@/lib/api"
import {
  ApiOffline,
  CodeBlock,
  Empty,
  Metric,
  Mono,
  PageHeader,
  Panel,
  Row,
  StatusPill,
  Timestamp,
  money,
  pct,
} from "@/components/ui"
import { DecisionForm } from "./decision-form"

export const dynamic = "force-dynamic"

/**
 * The human control surface.
 *
 * Resolve is asking permission. A reviewer needs enough to actually judge the
 * request — the customer's words, the agent's reasoning, the policy it relied
 * on, and the reply that will be sent if this is approved or voided if it is
 * not. An approval queue that shows only a JSON argument blob is asking
 * someone to rubber-stamp a decision they cannot evaluate.
 */
export default async function Approvals() {
  const [pending, oversight] = await Promise.all([
    safe(() => api.approvals("pending")),
    safe(api.oversight),
  ])
  if (!pending) return <ApiOffline />

  // Fetch the reviewing context for each pending item, in parallel.
  const contexts = (
    await Promise.all(pending.map((a) => safe(() => api.approval(a.id))))
  ).filter((c): c is ApprovalContext => c !== null)

  return (
    <>
      <PageHeader
        eyebrow="Human oversight"
        title="Approvals"
        subtitle="Resolve wants permission to perform these actions. The gate is the capability, not the agent's confidence: a refund above the configured limit waits here whether the model was 51% or 99% sure."
      />

      {oversight && oversight.decisions_total > 0 ? (
        <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="Pending"
            value={String(oversight.pending)}
            hint="Waiting on a person"
            tone={oversight.pending > 0 ? "warn" : "neutral"}
          />
          <Metric label="Approved" value={String(oversight.approved)} tone="ok" />
          <Metric label="Rejected" value={String(oversight.rejected)} tone="danger" />
          <Metric
            label="Override rate"
            value={
              oversight.approved + oversight.rejected > 0
                ? pct(oversight.override_rate, 0)
                : "—"
            }
            hint="Proposals a person refused"
          />
        </div>
      ) : null}

      {contexts.length === 0 ? (
        <Empty
          title="Nothing waiting"
          message="Every action Resolve proposed was within policy and ran without asking."
          hint="Privileged actions — refunds, cancellations, address changes — appear here for authorisation."
        />
      ) : (
        <div className="space-y-4">
          {contexts.map((ctx) => (
            <ApprovalCard key={ctx.approval.id} ctx={ctx} />
          ))}
        </div>
      )}
    </>
  )
}

function ApprovalCard({ ctx }: { ctx: ApprovalContext }) {
  const { approval } = ctx
  const financial = approval.amount_gbp !== null && approval.amount_gbp > 0

  return (
    <article className="hairline overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)]">
      {/* Header. Financial actions get a restrained left rule — enough to
          notice while scanning, not enough to make the page shout. */}
      <div
        className={`flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[var(--color-line)] px-5 py-3.5 ${
          financial ? "border-l-2 border-l-[var(--color-warn)]" : ""
        }`}
      >
        <h2 className="text-[15px] font-semibold tracking-[-0.01em]">
          {approval.tool.replace(/_/g, " ")}
        </h2>
        {approval.resource ? (
          <Mono>{approval.resource}</Mono>
        ) : null}
        {financial ? (
          <span className="tnum rounded-md border border-[var(--color-warn)]/25 bg-[var(--color-warn-dim)] px-2 py-0.5 text-[13px] font-semibold text-[var(--color-warn)]">
            {money(approval.amount_gbp)}
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-3">
          <span className="text-[11px] text-[var(--color-text-faint)]">
            requested <Timestamp value={approval.created_at} />
          </span>
          <StatusPill status={approval.decision} size="xs" />
        </span>
      </div>

      <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4 p-5">
          <div>
            <p className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
              Why Resolve proposed this
            </p>
            <p className="text-[13px] leading-relaxed text-[var(--color-text)]">
              {ctx.ai_rationale || approval.rationale}
            </p>
          </div>

          <div>
            <p className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
              Customer said
            </p>
            <p className="whitespace-pre-wrap rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-3.5 text-[13px] leading-relaxed text-[var(--color-text-muted)]">
              {ctx.customer_message}
            </p>
          </div>

          {ctx.policy_citations.length > 0 ? (
            <div>
              <p className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
                Policy the agent relied on
              </p>
              <ul className="space-y-1">
                {ctx.policy_citations.map((c) => (
                  <li
                    key={c.chunk_id}
                    className="flex items-baseline gap-2 text-[13px] text-[var(--color-text-muted)]"
                  >
                    <span className="text-[var(--color-text-faint)]">·</span>
                    <span className="min-w-0 flex-1 truncate">{c.title}</span>
                    <Mono dim>{c.chunk_id}</Mono>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {ctx.proposed_reply_body ? (
            <details className="group">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)] transition-colors duration-[var(--dur-fast)] hover:text-[var(--color-text-muted)]">
                <span className="transition-transform duration-[var(--dur-fast)] group-open:rotate-90">
                  ▸
                </span>
                Reply that will be sent if approved
              </summary>
              <div className="mt-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-3.5">
                <p className="mb-2 text-[13px] font-medium">{ctx.proposed_reply_subject}</p>
                <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-[var(--color-text-muted)]">
                  {ctx.proposed_reply_body}
                </p>
                <p className="mt-3 border-t border-[var(--color-line)] pt-2.5 text-[11px] text-[var(--color-text-faint)]">
                  If this action is rejected, the reply is voided and never sent.
                </p>
              </div>
            </details>
          ) : null}

          <details className="group">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)] transition-colors duration-[var(--dur-fast)] hover:text-[var(--color-text-muted)]">
              <span className="transition-transform duration-[var(--dur-fast)] group-open:rotate-90">
                ▸
              </span>
              Exact arguments
            </summary>
            <div className="mt-2">
              <CodeBlock>{JSON.stringify(approval.arguments, null, 2)}</CodeBlock>
            </div>
          </details>
        </div>

        <aside className="space-y-4 border-t border-[var(--color-line)] bg-[var(--color-surface-2)]/40 p-5 lg:border-l lg:border-t-0">
          <dl className="space-y-0.5">
            <Row label="Action" value={approval.tool.replace(/_/g, " ")} />
            <Row label="Resource" value={approval.resource ?? "—"} mono />
            {financial ? (
              <Row label="Amount" value={money(approval.amount_gbp)} mono tone="warn" />
            ) : null}
            <Row label="Capability" value="Requires approval" />
            <Row label="Intent" value={ctx.intent?.replace(/_/g, " ") ?? "—"} />
            <Row label="Customer" value={ctx.customer_email} mono />
          </dl>

          <DecisionForm
            approvalId={approval.id}
            action={approval.tool}
            amountGbp={approval.amount_gbp}
            resource={approval.resource}
          />

          <Link
            href={`/tickets/${approval.ticket_id}`}
            className="inline-block text-[13px] text-[var(--color-accent)] transition-opacity duration-[var(--dur-fast)] hover:opacity-80"
          >
            View the full execution trace →
          </Link>
        </aside>
      </div>
    </article>
  )
}
