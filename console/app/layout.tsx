import type { Metadata } from "next"
import { api, safe } from "@/lib/api"
import { Nav, Wordmark } from "@/components/shell"
import "./globals.css"

export const metadata: Metadata = {
  title: "Resolve — AI Operations Console",
  description:
    "Supervise autonomous customer operations: what the agent did, what it wants permission to do, and how well it is behaving.",
}

export const dynamic = "force-dynamic"

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Read from the API like everything else. Falls back to zero rather than
  // guessing, so a nav badge can never imply work that does not exist.
  const analytics = await safe(api.analytics)
  const pending = analytics?.pending_approvals ?? 0

  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <div className="relative z-10">
          <header className="sticky top-0 z-30 border-b border-[var(--color-line)] bg-[var(--color-canvas)]/85 backdrop-blur-xl">
            <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-6">
              <Wordmark />
              <div className="h-5 w-px bg-[var(--color-line)]" aria-hidden="true" />
              <Nav pendingApprovals={pending} />

              <div className="ml-auto flex items-center gap-2.5">
                {/* Environment, stated plainly. An operator should never have
                    to wonder whether they are looking at real customers. */}
                <span className="flex items-center gap-1.5 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1 text-[11px] font-medium tracking-wide text-[var(--color-text-muted)]">
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-warn)]" />
                  SYNTHETIC
                </span>
                <span className="hidden rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1 text-[11px] font-medium tracking-wide text-[var(--color-text-faint)] sm:inline">
                  LOCAL
                </span>
              </div>
            </div>
          </header>

          <main className="mx-auto max-w-[1400px] px-6 py-9">{children}</main>

          <footer className="mx-auto max-w-[1400px] px-6 pb-10">
            <p className="border-t border-[var(--color-line)] pt-5 text-[11px] leading-relaxed text-[var(--color-text-faint)]">
              All orders, customers, policies and tickets are synthetic and generated from a fixed
              seed. Resolve has not been run against real customer data.
            </p>
          </footer>
        </div>
      </body>
    </html>
  )
}
