import Link from "next/link"
import { api, safe } from "@/lib/api"
import {
  ApiOffline,
  Bar,
  Empty,
  Metric,
  Notice,
  PageHeader,
  Panel,
  Row,
  StatusPill,
  money,
  pct,
} from "@/components/ui"

export const dynamic = "force-dynamic"

/**
 * The operations dashboard.
 *
 * Answers, in order: what is Resolve doing, how much is automated, what needs
 * a person, what does it cost, and is it behaving safely.
 *
 * Every figure comes from `/v1/analytics` and `/v1/oversight`. There are no
 * trend charts, because there is no historical series to draw one from —
 * inventing a sparkline would make the page look better and mean nothing.
 */
export default async function Overview() {
  const [analytics, oversight] = await Promise.all([
    safe(api.analytics),
    safe(api.oversight),
  ])

  if (!analytics) return <ApiOffline />

  if (analytics.tickets_total === 0) {
    return (
      <>
        <PageHeader eyebrow="AI Operations" title="Overview" />
        <Empty
          title="No tickets processed yet"
          message="Load the synthetic inbox to see Resolve triage, act on and resolve customer messages."
          hint={
            <>
              Run <code className="text-[var(--color-text-muted)]">make seed</code> with the stack
              running.
            </>
          }
        />
      </>
    )
  }

  const total = analytics.tickets_total
  const resolved = analytics.by_status.resolved ?? 0
  const awaiting = analytics.by_status.awaiting_approval ?? 0
  const escalated = analytics.by_status.escalated ?? 0
  const intents = Object.entries(analytics.by_intent).sort((a, b) => b[1] - a[1])
  const humanTouch = total > 0 ? (awaiting + escalated) / total : 0

  return (
    <>
      <PageHeader
        eyebrow="AI Operations"
        title="Overview"
        subtitle={`${total} tickets processed against the synthetic dataset. Autonomous resolution is bounded by policy: anything that moves money or changes fulfilment waits for a person.`}
        actions={
          <span className="flex items-center gap-2 rounded-md border border-[var(--color-ok)]/25 bg-[var(--color-ok-dim)] px-3 py-1.5 text-xs font-medium text-[var(--color-ok)]">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-ok)]" />
            System operational
          </span>
        }
      />

      <div className="rise grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          label="Auto-resolved"
          value={pct(analytics.auto_resolution_rate, 0)}
          hint={`${resolved} of ${total}, handled with no human touch`}
          tone="ok"
          emphasis
        />
        <Metric
          label="Awaiting approval"
          value={pct(analytics.approval_rate, 0)}
          hint={`${analytics.pending_approvals} action${
            analytics.pending_approvals === 1 ? "" : "s"
          } queued for a person`}
          tone={analytics.pending_approvals > 0 ? "warn" : "neutral"}
          emphasis
        />
        <Metric
          label="Escalated"
          value={pct(analytics.escalation_rate, 0)}
          hint={`${escalated} routed to a person by design`}
          tone="accent"
          emphasis
        />
        <Metric
          label="Groundedness"
          value={pct(analytics.groundedness_rate, 0)}
          hint="Replies whose every claim was verified"
          tone={analytics.groundedness_rate >= 0.99 ? "ok" : "warn"}
          emphasis
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Panel title="Outcome distribution" description="Where tickets ended up">
          <div className="space-y-1">
            {Object.entries(analytics.by_status)
              .sort((a, b) => b[1] - a[1])
              .map(([status, count]) => (
                <Bar
                  key={status}
                  label={status.replace(/_/g, " ")}
                  count={count}
                  total={total}
                  tone={
                    status === "resolved"
                      ? "ok"
                      : status === "awaiting_approval"
                        ? "warn"
                        : status === "escalated"
                          ? "accent"
                          : "neutral"
                  }
                />
              ))}
          </div>
        </Panel>

        <Panel title="Intent distribution" description="What customers asked for">
          <div className="max-h-[268px] space-y-1 overflow-y-auto pr-1">
            {intents.map(([intent, count]) => (
              <Bar
                key={intent}
                label={intent.replace(/_/g, " ")}
                count={count}
                total={total}
                tone="neutral"
              />
            ))}
          </div>
        </Panel>

        <Panel title="Cost and latency" description="Model spend on processed traffic">
          <dl className="space-y-0.5">
            <Row label="Total spend" value={`$${analytics.total_cost_usd.toFixed(5)}`} mono />
            <Row label="Mean per ticket" value={`$${analytics.mean_cost_usd.toFixed(6)}`} mono />
            <Row
              label="Mean latency"
              value={`${analytics.mean_duration_ms.toFixed(0)} ms`}
              mono
            />
            <Row label="Human intervention" value={pct(humanTouch, 0)} />
          </dl>
          <p className="mt-4 border-t border-[var(--color-line)] pt-3 text-[11px] leading-relaxed text-[var(--color-text-faint)]">
            Spend is measured per call against published list prices. Latency excludes the queue
            wait.
          </p>
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Panel
            title="Human oversight"
            description="What people decided about the actions Resolve proposed"
            action={
              <Link
                href="/activity"
                className="text-xs text-[var(--color-accent)] transition-opacity duration-[var(--dur-fast)] hover:opacity-80"
              >
                Audit log →
              </Link>
            }
          >
            {!oversight || oversight.decisions_total === 0 ? (
              <p className="py-6 text-center text-sm text-[var(--color-text-muted)]">
                No privileged actions have been proposed yet.
              </p>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
                      Approved
                    </p>
                    <p className="tnum mt-1.5 text-2xl font-semibold text-[var(--color-ok)]">
                      {oversight.approved}
                    </p>
                    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                      {money(oversight.total_value_approved_gbp)} authorised
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
                      Rejected
                    </p>
                    <p className="tnum mt-1.5 text-2xl font-semibold text-[var(--color-danger)]">
                      {oversight.rejected}
                    </p>
                    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                      {money(oversight.total_value_rejected_gbp)} refused
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
                      Override rate
                    </p>
                    <p className="tnum mt-1.5 text-2xl font-semibold">
                      {oversight.approved + oversight.rejected > 0
                        ? pct(oversight.override_rate, 0)
                        : "—"}
                    </p>
                    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                      Proposals a person refused
                    </p>
                  </div>
                </div>

                {oversight.by_action.length > 0 ? (
                  <div className="mt-5 border-t border-[var(--color-line)] pt-4">
                    <p className="mb-2 text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
                      By action
                    </p>
                    <div className="space-y-0.5">
                      {oversight.by_action.map((a) => (
                        <Row
                          key={a.action}
                          label={a.action.replace(/_/g, " ")}
                          value={
                            <span className="text-[var(--color-text-muted)]">
                              {a.approved} approved · {a.rejected} rejected · {a.pending} pending
                            </span>
                          }
                        />
                      ))}
                    </div>
                  </div>
                ) : null}
              </>
            )}
          </Panel>
        </div>

        <Panel title="Projected time saved" description="A model, not a measurement">
          <p className="tnum text-[34px] font-semibold leading-none tracking-[-0.03em]">
            {analytics.projected_hours_saved.toFixed(1)}
            <span className="ml-1.5 text-base font-normal text-[var(--color-text-muted)]">
              hours
            </span>
          </p>
          <div className="mt-4">
            <Notice>
              Applies the assumptions below to the outcomes above. Autonomous resolutions save the
              full handling time; approved actions save the difference between writing a reply and
              reviewing one. This is arithmetic on stated inputs, not an observed result.
            </Notice>
          </div>
          <dl className="mt-4 space-y-0.5">
            {Object.entries(analytics.assumptions).map(([key, value]) => (
              <Row key={key} label={key.replace(/_/g, " ")} value={String(value)} mono />
            ))}
          </dl>
        </Panel>
      </div>

      <div className="mt-4">
        <Panel title="Recent outcomes" padded={false}>
          <RecentTickets />
        </Panel>
      </div>
    </>
  )
}

async function RecentTickets() {
  const tickets = await safe(() => api.tickets())
  if (!tickets || tickets.length === 0) {
    return <p className="p-5 text-sm text-[var(--color-text-muted)]">No tickets yet.</p>
  }
  return (
    <ul>
      {tickets.slice(0, 6).map((t) => (
        <li key={t.id}>
          <Link
            href={`/tickets/${t.id}`}
            className="flex items-center gap-4 border-b border-[var(--color-line)] px-5 py-3 text-sm transition-colors duration-[var(--dur-fast)] last:border-0 hover:bg-[var(--color-surface-2)]"
          >
            <span className="w-20 shrink-0 font-mono text-[11px] text-[var(--color-text-faint)]">
              {t.external_id}
            </span>
            <span className="min-w-0 flex-1 truncate">{t.subject}</span>
            <span className="hidden w-36 shrink-0 text-xs text-[var(--color-text-muted)] md:block">
              {t.intent?.replace(/_/g, " ") ?? "—"}
            </span>
            <StatusPill status={t.status} size="xs" />
          </Link>
        </li>
      ))}
    </ul>
  )
}
