import Link from "next/link"
import { notFound } from "next/navigation"
import { api, safe, type AgentRun, type AgentStep, type ReplyState } from "@/lib/api"
import {
  ApiOffline,
  CodeBlock,
  Mono,
  PageHeader,
  Panel,
  Row,
  StatusPill,
  Timestamp,
} from "@/components/ui"

export const dynamic = "force-dynamic"

/* ==========================================================================
   The agent execution trace.

   Read as a debugger, not a log. Every event says who caused it, what it
   cost, how long it took and what it decided — and the three actors (the
   model, a person, an external system) are visually distinct, because "the AI
   chose to refund" and "a human authorised the refund" are entirely different
   claims and must never look alike.
   ========================================================================== */

type Actor = "agent" | "human" | "system"

const STAGE: Record<
  string,
  { label: string; tone: "accent" | "ok" | "warn" | "danger" | "human" | "neutral" }
> = {
  redact: { label: "Redact", tone: "neutral" },
  safety: { label: "Safety", tone: "warn" },
  triage: { label: "Triage", tone: "accent" },
  retrieve: { label: "Retrieve", tone: "accent" },
  plan: { label: "Plan", tone: "accent" },
  act: { label: "Act", tone: "accent" },
  approval: { label: "Approval requested", tone: "warn" },
  draft: { label: "Draft", tone: "accent" },
  verify: { label: "Verify", tone: "accent" },
  human_decision: { label: "Human decision", tone: "human" },
  execute: { label: "Execute", tone: "ok" },
  not_executed: { label: "Not executed", tone: "danger" },
  escalate: { label: "Escalate", tone: "accent" },
  resolve: { label: "Resolved", tone: "ok" },
}

const DOT: Record<string, string> = {
  accent: "bg-[var(--color-accent)]",
  ok: "bg-[var(--color-ok)]",
  warn: "bg-[var(--color-warn)]",
  danger: "bg-[var(--color-danger)]",
  human: "bg-[var(--color-human)]",
  neutral: "bg-[var(--color-text-faint)]",
}

const ACTOR_LABEL: Record<Actor, string> = {
  agent: "AI",
  human: "Human",
  system: "System",
}

function TraceEvent({ step, last }: { step: AgentStep; last: boolean }) {
  const stage = STAGE[step.kind] ?? { label: step.kind, tone: "neutral" as const }
  const isHuman = step.actor_type === "human"
  const hasDetail = Object.keys(step.detail).length > 0

  return (
    <li className="relative pl-8">
      {!last ? (
        <span
          className="absolute left-[5px] top-4 h-full w-px bg-[var(--color-line)]"
          aria-hidden="true"
        />
      ) : null}
      <span
        className={`absolute left-0 top-[7px] h-[11px] w-[11px] rounded-full border-2 border-[var(--color-surface)] ${
          DOT[stage.tone]
        }`}
        aria-hidden="true"
      />

      <div
        className={`pb-5 ${
          isHuman
            ? "-ml-2 rounded-lg border border-[var(--color-human)]/20 bg-[var(--color-human-dim)]/40 px-3 py-2.5 pb-3"
            : ""
        }`}
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span
            className={`text-[11px] font-semibold uppercase tracking-[0.1em] ${
              isHuman ? "text-[var(--color-human)]" : "text-[var(--color-text-faint)]"
            }`}
          >
            {stage.label}
          </span>
          {step.actor_type !== "agent" ? (
            <span className="rounded border border-[var(--color-line-strong)] px-1.5 py-px text-[10px] uppercase tracking-wide text-[var(--color-text-faint)]">
              {ACTOR_LABEL[step.actor_type]}
            </span>
          ) : null}
          <span className="ml-auto flex shrink-0 items-center gap-2.5 font-mono text-[11px] text-[var(--color-text-faint)]">
            {step.duration_ms > 0 ? <span>{step.duration_ms.toFixed(0)}ms</span> : null}
            {step.cost_usd > 0 ? <span>${step.cost_usd.toFixed(5)}</span> : null}
            {step.created_at ? <Timestamp value={step.created_at} /> : null}
          </span>
        </div>

        <p className="mt-1 text-sm leading-relaxed text-[var(--color-text)]">{step.summary}</p>

        {step.actor ? (
          <p className="mt-1 text-xs text-[var(--color-text-muted)]">by {step.actor}</p>
        ) : null}

        {hasDetail ? (
          <details className="group mt-2">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[11px] text-[var(--color-text-faint)] transition-colors duration-[var(--dur-fast)] hover:text-[var(--color-text-muted)]">
              <span className="transition-transform duration-[var(--dur-fast)] group-open:rotate-90">
                ▸
              </span>
              detail
            </summary>
            <div className="mt-2">
              <CodeBlock>{JSON.stringify(step.detail, null, 2)}</CodeBlock>
            </div>
          </details>
        ) : null}
      </div>
    </li>
  )
}

/** What happened to the drafted reply, stated rather than implied. */
function ReplyBanner({ state, reason }: { state: ReplyState; reason: string | null }) {
  const copy: Record<ReplyState, { title: string; body: string; tone: string } | null> = {
    none: null,
    sent: {
      title: "Sent to the customer",
      body: "Every action this reply describes was completed.",
      tone: "ok",
    },
    provisional: {
      title: "Provisional — not sent",
      body: "Drafted and verified, but not released.",
      tone: "warn",
    },
    awaiting_approval: {
      title: "Not sent — awaiting approval",
      body: "This reply describes an action that still needs a person to authorise it.",
      tone: "warn",
    },
    voided: {
      title: "Voided — not sent",
      body:
        reason ??
        "An action this reply describes was rejected, so its claims are not true and it was never sent.",
      tone: "danger",
    },
    held: {
      title: "Held for review — not sent",
      body: reason ?? "An approved action failed to execute, so a person must review this reply.",
      tone: "warn",
    },
  }
  const meta = copy[state]
  if (!meta) return null

  const toneClass = {
    ok: "border-[var(--color-ok)]/25 bg-[var(--color-ok-dim)] text-[var(--color-ok)]",
    warn: "border-[var(--color-warn)]/25 bg-[var(--color-warn-dim)] text-[var(--color-warn)]",
    danger: "border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)] text-[var(--color-danger)]",
  }[meta.tone]!

  return (
    <div className={`mb-4 rounded-lg border px-4 py-3 ${toneClass}`}>
      <p className="text-[13px] font-semibold">{meta.title}</p>
      <p className="mt-1 text-xs leading-relaxed text-[var(--color-text-muted)]">{meta.body}</p>
    </div>
  )
}

export default async function TicketDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  const ticket = await safe(() => api.ticket(id))
  if (ticket === null) return <ApiOffline />
  if (!ticket.id) notFound()

  const run: AgentRun | undefined = ticket.runs[0]
  const rejected = run?.reply_state === "voided"

  return (
    <>
      <div className="mb-5">
        <Link
          href="/tickets"
          className="text-[13px] text-[var(--color-text-muted)] transition-colors duration-[var(--dur-fast)] hover:text-[var(--color-text)]"
        >
          ← Tickets
        </Link>
      </div>

      <PageHeader
        eyebrow={ticket.external_id}
        title={ticket.subject}
        subtitle={`${ticket.from_email}${ticket.order_ref ? ` · ${ticket.order_ref}` : ""}`}
        actions={<StatusPill status={ticket.status} />}
      />

      {/* The single most important thing to communicate on a rejected ticket. */}
      {rejected ? (
        <div className="mb-5 rounded-xl border border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)] p-5">
          <p className="text-sm font-semibold text-[var(--color-danger)]">Rejected by a human</p>
          <ul className="mt-2.5 space-y-1 text-[13px] leading-relaxed text-[var(--color-text-muted)]">
            <li>The proposed action was <strong className="text-[var(--color-text)]">not executed</strong>.</li>
            <li>The provisional reply was <strong className="text-[var(--color-text)]">not sent</strong>.</li>
            <li>The ticket is escalated for manual handling.</li>
          </ul>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-4">
          <Panel title="Customer message">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-[var(--color-text-muted)]">
              {ticket.body}
            </p>
          </Panel>

          {run ? (
            <Panel
              title="Execution trace"
              description={`${run.steps.length} events · ${run.provider} · prompt ${run.prompt_version}`}
            >
              <ol className="mt-1">
                {run.steps.map((step, i) => (
                  <TraceEvent
                    key={`${step.index}-${step.kind}`}
                    step={step}
                    last={i === run.steps.length - 1}
                  />
                ))}
              </ol>
            </Panel>
          ) : null}

          {run?.reply_body ? (
            <Panel title="Drafted reply">
              <ReplyBanner state={run.reply_state} reason={run.reply_state_reason} />
              <p className="mb-3 text-[13px] font-medium">{run.reply_subject}</p>
              <p
                className={`whitespace-pre-wrap text-sm leading-relaxed ${
                  run.reply_state === "voided"
                    ? "text-[var(--color-text-faint)] line-through decoration-[var(--color-danger)]/40"
                    : "text-[var(--color-text-muted)]"
                }`}
              >
                {run.reply_body}
              </p>
              <p className="mt-4 border-t border-[var(--color-line)] pt-3 text-[11px] text-[var(--color-text-faint)]">
                {run.citation_count} policy citation{run.citation_count === 1 ? "" : "s"} ·{" "}
                {run.grounded ? "verified as grounded" : "failed groundedness verification"}
              </p>
            </Panel>
          ) : null}
        </div>

        <div className="space-y-4">
          {run ? (
            <Panel title="Run">
              <dl className="space-y-0.5">
                <Row label="Intent" value={run.intent?.replace(/_/g, " ") ?? "—"} />
                <Row label="Outcome" value={<StatusPill status={run.status} size="xs" />} />
                <Row label="Reply" value={<StatusPill status={run.reply_state} size="xs" />} />
                <Row label="Cost" value={`$${run.cost_usd.toFixed(5)}`} mono />
                <Row label="Tokens" value={`${run.tokens_in} / ${run.tokens_out}`} mono />
                <Row label="Latency" value={`${run.duration_ms.toFixed(0)} ms`} mono />
                <Row label="Steps" value={String(run.steps.length)} mono />
                <Row label="Provider" value={run.provider} mono />
                <Row label="Prompt" value={run.prompt_version} mono />
                <Row label="Started" value={<Timestamp value={run.created_at} />} mono />
              </dl>
            </Panel>
          ) : null}

          {run?.escalation_reason ? (
            <Panel title="Why a human is involved">
              <p className="text-[13px] leading-relaxed text-[var(--color-text-muted)]">
                {run.escalation_reason}
              </p>
            </Panel>
          ) : null}

          <Panel title="Identifiers">
            <dl className="space-y-0.5">
              <Row label="Ticket" value={<Mono>{ticket.id.slice(0, 18)}…</Mono>} />
              <Row label="External" value={<Mono>{ticket.external_id}</Mono>} />
              <Row
                label="Order"
                value={ticket.order_ref ? <Mono>{ticket.order_ref}</Mono> : <Mono dim>—</Mono>}
              />
              <Row label="Received" value={<Timestamp value={ticket.created_at} />} />
            </dl>
          </Panel>
        </div>
      </div>
    </>
  )
}
