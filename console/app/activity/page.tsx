import Link from "next/link"
import { api, safe } from "@/lib/api"
import {
  ApiOffline,
  Empty,
  Metric,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Row,
  StatusPill,
  Timestamp,
  money,
  pct,
} from "@/components/ui"

export const dynamic = "force-dynamic"

/**
 * The human-oversight audit log.
 *
 * The durable record of who authorised what, and what actually happened as a
 * result. Read-only by construction — there is no endpoint that edits or
 * deletes an entry, which is the point of an audit log.
 *
 * Kept separate from Evaluation on purpose. Evaluation asks whether the AI
 * behaved correctly against a fixed golden set; this asks what operators
 * actually did about the actions it proposed on real traffic. A system can
 * score perfectly on the former while operators refuse most of the latter.
 */
export default async function Activity() {
  const [events, oversight] = await Promise.all([safe(() => api.audit(200)), safe(api.oversight)])
  if (!events) return <ApiOffline />

  const decided = (oversight?.approved ?? 0) + (oversight?.rejected ?? 0)

  return (
    <>
      <PageHeader
        eyebrow="Human oversight"
        title="Activity"
        subtitle="Every decision a person made about an action Resolve proposed. Append-only: entries cannot be edited or removed."
      />

      {oversight && oversight.decisions_total > 0 ? (
        <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="Decisions recorded"
            value={String(decided)}
            hint={`${oversight.pending} still pending`}
          />
          <Metric
            label="Approval rate"
            value={decided > 0 ? pct(oversight.approval_rate, 0) : "—"}
            hint="Of decided proposals"
            tone="ok"
          />
          <Metric
            label="Override rate"
            value={decided > 0 ? pct(oversight.override_rate, 0) : "—"}
            hint="Refused by a person"
            tone={oversight.rejected > 0 ? "danger" : "neutral"}
          />
          <Metric
            label="Median decision time"
            value={
              oversight.median_time_to_decision_seconds === null
                ? "—"
                : `${oversight.median_time_to_decision_seconds.toFixed(0)}s`
            }
            hint="Request to decision"
          />
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Panel title="Decision log" padded={false}>
          {events.length === 0 ? (
            <div className="p-5">
              <Empty
                title="No decisions yet"
                message="Once a person approves or rejects a proposed action, it is recorded here permanently."
              />
            </div>
          ) : (
            <ul>
              {events.map((e) => (
                <li
                  key={e.approval_id}
                  className="border-b border-[var(--color-line)] px-5 py-3.5 transition-colors duration-[var(--dur-fast)] last:border-0 hover:bg-[var(--color-surface-2)]"
                >
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
                    <StatusPill status={e.decision} size="xs" />
                    <span className="text-[13px] font-medium text-[var(--color-text)]">
                      {e.action.replace(/_/g, " ")}
                    </span>
                    {e.resource ? <Mono>{e.resource}</Mono> : null}
                    {e.amount_gbp ? (
                      <span className="tnum text-[13px] text-[var(--color-warn)]">
                        {money(e.amount_gbp)}
                      </span>
                    ) : null}
                    <span className="ml-auto text-[11px] text-[var(--color-text-faint)]">
                      <Timestamp value={e.decided_at} />
                    </span>
                  </div>

                  <p className="mt-1.5 text-[13px] text-[var(--color-text-muted)]">
                    <span className="text-[var(--color-human)]">{e.actor ?? "unknown"}</span>{" "}
                    {e.decision === "approved" ? "authorised" : "refused"} this action ·{" "}
                    <span
                      className={
                        e.outcome === "executed"
                          ? "text-[var(--color-ok)]"
                          : e.outcome === "not_executed"
                            ? "text-[var(--color-danger)]"
                            : "text-[var(--color-warn)]"
                      }
                    >
                      {e.outcome.replace(/_/g, " ")}
                    </span>
                  </p>

                  {e.note ? (
                    <p className="mt-1 text-xs italic text-[var(--color-text-faint)]">
                      “{e.note}”
                    </p>
                  ) : null}

                  <Link
                    href={`/tickets/${e.ticket_id}`}
                    className="mt-1.5 inline-block text-[11px] text-[var(--color-accent)] transition-opacity duration-[var(--dur-fast)] hover:opacity-80"
                  >
                    {e.ticket_subject || "View ticket"} →
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <div className="space-y-4">
          {oversight && oversight.by_action.length > 0 ? (
            <Panel title="Trust by action" description="Where operators intervene most">
              <dl className="space-y-0.5">
                {oversight.by_action.map((a) => {
                  const settled = a.approved + a.rejected
                  return (
                    <Row
                      key={a.action}
                      label={a.action.replace(/_/g, " ")}
                      value={
                        settled > 0 ? (
                          <span className={a.rejected > 0 ? "text-[var(--color-warn)]" : ""}>
                            {pct(a.approval_rate, 0)} approved
                          </span>
                        ) : (
                          <span className="text-[var(--color-text-faint)]">
                            {a.pending} pending
                          </span>
                        )
                      }
                    />
                  )
                })}
              </dl>
              <p className="mt-3 border-t border-[var(--color-line)] pt-3 text-[11px] leading-relaxed text-[var(--color-text-faint)]">
                A low approval rate for one action is the signal that the agent&rsquo;s judgement
                is not trusted there — and the place to look before widening autonomy.
              </p>
            </Panel>
          ) : null}

          {oversight && (oversight.total_value_approved_gbp > 0 ||
            oversight.total_value_rejected_gbp > 0) ? (
            <Panel title="Financial exposure">
              <dl className="space-y-0.5">
                <Row
                  label="Authorised"
                  value={money(oversight.total_value_approved_gbp)}
                  mono
                  tone="ok"
                />
                <Row
                  label="Refused"
                  value={money(oversight.total_value_rejected_gbp)}
                  mono
                  tone="danger"
                />
              </dl>
            </Panel>
          ) : null}

          <Notice>
            This log records <strong className="text-[var(--color-text)]">operations</strong>: what
            happened to real processed tickets and who authorised it. It is deliberately separate
            from <Link href="/evals" className="text-[var(--color-accent)]">Evaluation</Link>,
            which measures the agent against a fixed golden set.
          </Notice>
        </div>
      </div>
    </>
  )
}
