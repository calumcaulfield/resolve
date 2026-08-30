import Link from "next/link"
import type { ReactNode } from "react"

/* ==========================================================================
   Primitives.

   Pages compose these; they do not style themselves. That is what stops five
   screens drifting into five different products.
   ========================================================================== */

/* --- status vocabulary ---------------------------------------------------
   One place that decides what every state looks like and is called. When a
   ticket, a reply and an approval all say "rejected", they must look the same,
   or a reader has to learn three vocabularies.
   ------------------------------------------------------------------------ */

type Tone = "neutral" | "ok" | "warn" | "danger" | "accent" | "human"

const TONE: Record<Tone, string> = {
  neutral: "border-[var(--color-line-strong)] bg-[var(--color-surface-2)] text-[var(--color-text-muted)]",
  ok: "border-[var(--color-ok)]/25 bg-[var(--color-ok-dim)] text-[var(--color-ok)]",
  warn: "border-[var(--color-warn)]/25 bg-[var(--color-warn-dim)] text-[var(--color-warn)]",
  danger: "border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)] text-[var(--color-danger)]",
  accent: "border-[var(--color-accent)]/25 bg-[var(--color-accent-dim)] text-[var(--color-accent)]",
  human: "border-[var(--color-human)]/25 bg-[var(--color-human-dim)] text-[var(--color-human)]",
}

const STATUS: Record<string, { label: string; tone: Tone }> = {
  // ticket lifecycle
  received: { label: "Received", tone: "neutral" },
  triaged: { label: "Triaged", tone: "neutral" },
  in_progress: { label: "In progress", tone: "accent" },
  awaiting_approval: { label: "Awaiting approval", tone: "warn" },
  resolved: { label: "Resolved", tone: "ok" },
  escalated: { label: "Escalated", tone: "accent" },
  failed: { label: "Failed", tone: "danger" },
  // approval decision
  pending: { label: "Pending", tone: "warn" },
  approved: { label: "Approved", tone: "ok" },
  rejected: { label: "Rejected", tone: "danger" },
  expired: { label: "Expired", tone: "neutral" },
  // approval outcome
  executed: { label: "Executed", tone: "ok" },
  execution_failed: { label: "Execution failed", tone: "danger" },
  not_executed: { label: "Not executed", tone: "danger" },
  // reply lifecycle
  none: { label: "No reply", tone: "neutral" },
  provisional: { label: "Provisional", tone: "warn" },
  sent: { label: "Sent", tone: "ok" },
  voided: { label: "Voided", tone: "danger" },
  held: { label: "Held", tone: "warn" },
}

export function StatusPill({
  status,
  size = "sm",
}: {
  status: string
  size?: "sm" | "xs"
}) {
  const meta = STATUS[status] ?? { label: status.replace(/_/g, " "), tone: "neutral" as Tone }
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border font-medium ${
        TONE[meta.tone]
      } ${size === "xs" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs"}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-80" />
      {meta.label}
    </span>
  )
}

/* --- layout ------------------------------------------------------------- */

export function PageHeader({
  title,
  subtitle,
  eyebrow,
  actions,
}: {
  title: string
  subtitle?: string
  eyebrow?: string
  actions?: ReactNode
}) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow ? (
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--color-text-faint)]">
            {eyebrow}
          </p>
        ) : null}
        <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.02em]">{title}</h1>
        {subtitle ? (
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-[var(--color-text-muted)]">
            {subtitle}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  )
}

export function Panel({
  title,
  description,
  action,
  children,
  padded = true,
}: {
  title?: string
  description?: string
  action?: ReactNode
  children: ReactNode
  padded?: boolean
}) {
  return (
    <section className="hairline overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)]">
      {title ? (
        <div className="flex items-center justify-between gap-4 border-b border-[var(--color-line)] px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="text-[13px] font-semibold tracking-[-0.01em]">{title}</h2>
            {description ? (
              <p className="mt-0.5 text-xs text-[var(--color-text-muted)]">{description}</p>
            ) : null}
          </div>
          {action}
        </div>
      ) : null}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </section>
  )
}

/* --- metrics ------------------------------------------------------------ */

export function Metric({
  label,
  value,
  hint,
  tone = "neutral",
  emphasis = false,
}: {
  label: string
  value: string
  hint?: string
  tone?: Tone
  emphasis?: boolean
}) {
  const valueTone =
    tone === "neutral"
      ? "text-[var(--color-text)]"
      : {
          ok: "text-[var(--color-ok)]",
          warn: "text-[var(--color-warn)]",
          danger: "text-[var(--color-danger)]",
          accent: "text-[var(--color-accent)]",
          human: "text-[var(--color-human)]",
          neutral: "",
        }[tone]

  return (
    <div className="hairline rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-5 transition-colors duration-[var(--dur-base)] hover:border-[var(--color-line-strong)]">
      <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-[var(--color-text-faint)]">
        {label}
      </p>
      <p
        className={`tnum mt-2.5 font-semibold tracking-[-0.03em] ${valueTone} ${
          emphasis ? "text-[34px] leading-none" : "text-[28px] leading-none"
        }`}
      >
        {value}
      </p>
      {hint ? <p className="mt-2 text-xs text-[var(--color-text-muted)]">{hint}</p> : null}
    </div>
  )
}

/** A labelled row inside a panel. Used for every key/value list in the app. */
export function Row({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string
  value: ReactNode
  mono?: boolean
  tone?: "ok" | "warn" | "danger"
}) {
  const toneClass = tone
    ? { ok: "text-[var(--color-ok)]", warn: "text-[var(--color-warn)]", danger: "text-[var(--color-danger)]" }[tone]
    : ""
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <dt className="shrink-0 text-[13px] text-[var(--color-text-muted)]">{label}</dt>
      <dd
        className={`tnum min-w-0 truncate text-right text-[13px] ${
          mono ? "font-mono text-xs" : ""
        } ${toneClass}`}
      >
        {value}
      </dd>
    </div>
  )
}

/** A proportional bar. Only ever rendered from a real count. */
export function Bar({
  label,
  count,
  total,
  tone = "accent",
}: {
  label: string
  count: number
  total: number
  tone?: Tone
}) {
  const pct = total > 0 ? (count / total) * 100 : 0
  const fill = {
    ok: "bg-[var(--color-ok)]",
    warn: "bg-[var(--color-warn)]",
    danger: "bg-[var(--color-danger)]",
    accent: "bg-[var(--color-accent)]",
    human: "bg-[var(--color-human)]",
    neutral: "bg-[var(--color-text-faint)]",
  }[tone]

  return (
    <div className="group py-1.5">
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <span className="truncate text-[13px] text-[var(--color-text-muted)]">{label}</span>
        <span className="tnum shrink-0 text-[13px] text-[var(--color-text)]">
          {count}
          <span className="ml-1.5 text-xs text-[var(--color-text-faint)]">
            {total > 0 ? `${Math.round(pct)}%` : ""}
          </span>
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--color-surface-3)]">
        <div
          className={`h-full rounded-full ${fill} transition-[width] duration-[var(--dur-slow)] ease-[var(--ease-out)]`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

/* --- text ---------------------------------------------------------------- */

export function Mono({ children, dim = false }: { children: ReactNode; dim?: boolean }) {
  return (
    <span
      className={`font-mono text-xs ${
        dim ? "text-[var(--color-text-faint)]" : "text-[var(--color-text-muted)]"
      }`}
    >
      {children}
    </span>
  )
}

export function CodeBlock({ children }: { children: ReactNode }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-overlay)] p-3.5 font-mono text-[11.5px] leading-relaxed text-[var(--color-text-muted)]">
      {children}
    </pre>
  )
}

/* --- states -------------------------------------------------------------- */

export function Empty({
  title,
  message,
  hint,
}: {
  title: string
  message?: string
  hint?: ReactNode
}) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--color-line-strong)] bg-[var(--color-surface)]/40 px-8 py-14 text-center">
      <p className="text-[15px] font-medium text-[var(--color-text)]">{title}</p>
      {message ? (
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-[var(--color-text-muted)]">
          {message}
        </p>
      ) : null}
      {hint ? (
        <div className="mx-auto mt-4 max-w-md text-xs text-[var(--color-text-faint)]">{hint}</div>
      ) : null}
    </div>
  )
}

/**
 * A load failure that is *not* an empty state.
 *
 * Deliberately distinct from `Empty`: "nobody has done this yet" and
 * "something is broken" must never look the same, or a real failure gets
 * mistaken for a normal quiet state.
 */
export function LoadError({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="rounded-xl border border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)] p-5">
      <p className="text-sm font-medium text-[var(--color-danger)]">{title}</p>
      <p className="mt-2 text-sm text-[var(--color-text-muted)]">
        Check the API is running: <Kbd>make ps</Kbd> then <Kbd>make logs</Kbd>.
      </p>
      {detail ? (
        <p className="mt-3 break-words font-mono text-[11px] leading-relaxed text-[var(--color-text-faint)]">
          {detail}
        </p>
      ) : null}
    </div>
  )
}

export function ApiOffline({ detail }: { detail?: string }) {
  return (
    <div className="rounded-xl border border-[var(--color-warn)]/25 bg-[var(--color-warn-dim)] p-5">
      <p className="text-sm font-medium text-[var(--color-warn)]">The API is not reachable.</p>
      <p className="mt-2 text-sm text-[var(--color-text-muted)]">
        Start the stack with <Kbd>make up</Kbd>, then load the synthetic inbox with{" "}
        <Kbd>make seed</Kbd>.
      </p>
      {detail ? (
        <p className="mt-3 break-words font-mono text-[11px] text-[var(--color-text-faint)]">
          {detail}
        </p>
      ) : null}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <code className="rounded border border-[var(--color-line-strong)] bg-[var(--color-surface-2)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-text)]">
      {children}
    </code>
  )
}

/* --- misc ---------------------------------------------------------------- */

export function Divider() {
  return <div className="h-px w-full bg-[var(--color-line)]" />
}

/** A note the reader should not skim past — used for honesty caveats. */
export function Notice({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return (
    <div
      className={`rounded-lg border px-4 py-3 text-xs leading-relaxed ${
        tone === "neutral"
          ? "border-[var(--color-line)] bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
          : TONE[tone]
      }`}
    >
      {children}
    </div>
  )
}

export function LinkCard({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="block rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-4 transition-all duration-[var(--dur-base)] ease-[var(--ease-out)] hover:border-[var(--color-line-strong)] hover:bg-[var(--color-surface-2)]"
    >
      {children}
    </Link>
  )
}

/** Absolute timestamp with a readable relative hint. */
export function Timestamp({ value, relative = true }: { value: string; relative?: boolean }) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return <Mono dim>{value}</Mono>
  return (
    <time dateTime={value} title={date.toISOString()} className="tnum">
      {relative ? formatRelative(date) : date.toISOString().replace("T", " ").slice(0, 19)}
    </time>
  )
}

export function formatRelative(date: Date): string {
  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  return `£${value.toFixed(2)}`
}

export function pct(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`
}
