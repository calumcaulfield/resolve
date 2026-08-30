"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/tickets", label: "Tickets" },
  { href: "/approvals", label: "Approvals" },
  { href: "/activity", label: "Activity" },
  { href: "/evals", label: "Evaluation" },
] as const

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href)
}

export function Nav({ pendingApprovals }: { pendingApprovals: number }) {
  const pathname = usePathname()

  return (
    <nav className="flex items-center gap-0.5" aria-label="Primary">
      {NAV.map((item) => {
        const active = isActive(pathname, item.href)
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`relative flex items-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors duration-[var(--dur-fast)] ${
              active
                ? "bg-[var(--color-surface-2)] font-medium text-[var(--color-text)]"
                : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]/60 hover:text-[var(--color-text)]"
            }`}
          >
            {item.label}
            {/* The only count in the navigation, because it is the only one
                that represents work waiting on a person. */}
            {item.href === "/approvals" && pendingApprovals > 0 ? (
              <span className="tnum rounded-full border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)] px-1.5 py-px text-[10px] font-medium text-[var(--color-warn)]">
                {pendingApprovals}
              </span>
            ) : null}
          </Link>
        )
      })}
    </nav>
  )
}

export function Wordmark() {
  return (
    <Link href="/" className="flex items-center gap-2.5" aria-label="Resolve home">
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
        <rect
          x="0.75"
          y="0.75"
          width="20.5"
          height="20.5"
          rx="5.5"
          stroke="var(--color-line-strong)"
          fill="var(--color-surface-2)"
        />
        <path
          d="M7 15V7h4.2a2.6 2.6 0 0 1 0 5.2H9.4L14 15"
          stroke="var(--color-accent)"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-[14px] font-semibold tracking-[-0.02em]">Resolve</span>
    </Link>
  )
}
