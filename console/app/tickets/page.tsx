import Link from "next/link"
import { api, safe, type Ticket } from "@/lib/api"
import { ApiOffline, Empty, Mono, PageHeader, StatusPill, Timestamp } from "@/components/ui"

export const dynamic = "force-dynamic"

/**
 * The ticket queue.
 *
 * Filters are server-side and driven by the URL, so a filtered view is a real
 * address that can be linked and reloaded. Each filter renders its own count
 * from the same data, which means no control can ever promise results it does
 * not have.
 */

const FILTERS = [
  { key: "all", label: "All" },
  { key: "resolved", label: "Resolved" },
  { key: "awaiting_approval", label: "Awaiting approval" },
  { key: "escalated", label: "Escalated" },
] as const

type FilterKey = (typeof FILTERS)[number]["key"]

function matches(ticket: Ticket, filter: FilterKey, query: string): boolean {
  if (filter !== "all" && ticket.status !== filter) return false
  if (!query) return true
  const q = query.toLowerCase()
  return (
    ticket.external_id.toLowerCase().includes(q) ||
    ticket.subject.toLowerCase().includes(q) ||
    (ticket.order_ref ?? "").toLowerCase().includes(q) ||
    ticket.from_email.toLowerCase().includes(q) ||
    (ticket.intent ?? "").toLowerCase().includes(q)
  )
}

export default async function Tickets({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; q?: string }>
}) {
  const params = await searchParams
  const filter = (FILTERS.find((f) => f.key === params.status)?.key ?? "all") as FilterKey
  const query = (params.q ?? "").trim()

  const tickets = await safe(() => api.tickets())
  if (!tickets) return <ApiOffline />

  const counts = Object.fromEntries(
    FILTERS.map((f) => [
      f.key,
      f.key === "all" ? tickets.length : tickets.filter((t) => t.status === f.key).length,
    ]),
  ) as Record<FilterKey, number>

  const visible = tickets.filter((t) => matches(t, filter, query))

  return (
    <>
      <PageHeader
        eyebrow="Operations"
        title="Tickets"
        subtitle="Every inbound message and what Resolve decided to do with it."
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-1">
          {FILTERS.map((f) => {
            const active = f.key === filter
            const href = {
              pathname: "/tickets",
              query: {
                ...(f.key === "all" ? {} : { status: f.key }),
                ...(query ? { q: query } : {}),
              },
            }
            return (
              <Link
                key={f.key}
                href={href}
                aria-current={active ? "true" : undefined}
                className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors duration-[var(--dur-fast)] ${
                  active
                    ? "bg-[var(--color-surface-3)] font-medium text-[var(--color-text)]"
                    : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                }`}
              >
                {f.label}
                <span className="tnum text-[11px] text-[var(--color-text-faint)]">
                  {counts[f.key]}
                </span>
              </Link>
            )
          })}
        </div>

        {/* A plain GET form: works without JavaScript, and the result is a
            shareable URL. */}
        <form action="/tickets" method="get" className="flex items-center gap-2">
          {filter !== "all" ? <input type="hidden" name="status" value={filter} /> : null}
          <input
            type="search"
            name="q"
            defaultValue={query}
            placeholder="Search ticket, order, customer…"
            aria-label="Search tickets"
            className="w-64 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 text-[13px] text-[var(--color-text)] placeholder:text-[var(--color-text-faint)] transition-colors duration-[var(--dur-fast)] focus:border-[var(--color-accent)]/50 focus:outline-none"
          />
          <button
            type="submit"
            className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-[13px] text-[var(--color-text-muted)] transition-colors duration-[var(--dur-fast)] hover:border-[var(--color-line-strong)] hover:text-[var(--color-text)]"
          >
            Search
          </button>
          {query ? (
            <Link
              href={filter === "all" ? "/tickets" : `/tickets?status=${filter}`}
              className="text-xs text-[var(--color-text-muted)] transition-colors hover:text-[var(--color-text)]"
            >
              Clear
            </Link>
          ) : null}
        </form>

        <span className="ml-auto text-xs text-[var(--color-text-faint)]">
          {visible.length} of {tickets.length}
        </span>
      </div>

      {visible.length === 0 ? (
        <Empty
          title="No matching tickets"
          message={
            query
              ? `Nothing matches “${query}” in this view.`
              : "No tickets have this status yet."
          }
        />
      ) : (
        <div className="hairline overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)]">
          <div className="grid grid-cols-[88px_1fr_150px_112px_150px] items-center gap-4 border-b border-[var(--color-line)] bg-[var(--color-surface-2)] px-5 py-2.5 text-[11px] font-medium uppercase tracking-[0.1em] text-[var(--color-text-faint)]">
            <span>Ticket</span>
            <span>Subject</span>
            <span className="hidden md:block">Intent</span>
            <span className="hidden lg:block">Order</span>
            <span className="text-right">Status</span>
          </div>
          <ul>
            {visible.map((t) => (
              <li key={t.id}>
                <Link
                  href={`/tickets/${t.id}`}
                  className="grid grid-cols-[88px_1fr_150px_112px_150px] items-center gap-4 border-b border-[var(--color-line)] px-5 py-3 text-sm transition-colors duration-[var(--dur-fast)] last:border-0 hover:bg-[var(--color-surface-2)]"
                >
                  <Mono dim>{t.external_id}</Mono>
                  <span className="min-w-0">
                    <span className="block truncate text-[var(--color-text)]">{t.subject}</span>
                    <span className="mt-0.5 block truncate text-[11px] text-[var(--color-text-faint)]">
                      {t.from_email} · <Timestamp value={t.created_at} />
                    </span>
                  </span>
                  <span className="hidden truncate text-xs text-[var(--color-text-muted)] md:block">
                    {t.intent?.replace(/_/g, " ") ?? "—"}
                  </span>
                  <span className="hidden lg:block">
                    {t.order_ref ? <Mono>{t.order_ref}</Mono> : <Mono dim>—</Mono>}
                  </span>
                  <span className="flex justify-end">
                    <StatusPill status={t.status} size="xs" />
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}
