"use client"

import { useActionState, useState } from "react"
import { useFormStatus } from "react-dom"
import { decideApproval, type DecisionState } from "./actions"

const INITIAL: DecisionState = { status: "idle" }

function Submit({
  variant,
  children,
  onArm,
}: {
  variant: "approve" | "reject"
  children: React.ReactNode
  onArm?: () => void
}) {
  const { pending } = useFormStatus()
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-[13px] font-medium transition-all duration-[var(--dur-fast)] disabled:cursor-not-allowed disabled:opacity-50"
  const style =
    variant === "approve"
      ? "bg-[var(--color-ok)] text-[#06120c] hover:brightness-110 active:brightness-95"
      : "border border-[var(--color-line-strong)] bg-[var(--color-surface-2)] text-[var(--color-text)] hover:border-[var(--color-danger)]/40 hover:text-[var(--color-danger)]"

  return (
    <button type="submit" disabled={pending} onClick={onArm} className={`${base} ${style}`}>
      {pending ? (
        <>
          <span
            className="h-3 w-3 animate-spin rounded-full border-[1.5px] border-current border-t-transparent"
            aria-hidden="true"
          />
          Working…
        </>
      ) : (
        children
      )}
    </button>
  )
}

/**
 * The decision control.
 *
 * Consequential actions get a confirmation step; the button that moves money
 * is not one click away from a mis-click. `useActionState` keeps the pending
 * and error states real — the form disables while in flight, so a double
 * submission cannot even be attempted from here, and the API's 409 guard
 * catches anything that gets past the UI.
 */
export function DecisionForm({
  approvalId,
  action,
  amountGbp,
  resource,
}: {
  approvalId: string
  action: string
  amountGbp: number | null
  resource: string | null
}) {
  const [state, formAction] = useActionState(decideApproval, INITIAL)
  const [confirming, setConfirming] = useState<"approve" | "reject" | null>(null)
  const financial = amountGbp !== null && amountGbp > 0

  if (state.status === "ok") {
    return (
      <div className="rise flex items-center gap-2.5 rounded-lg border border-[var(--color-ok)]/25 bg-[var(--color-ok-dim)] px-4 py-3">
        <span className="text-[var(--color-ok)]">✓</span>
        <p className="text-[13px] text-[var(--color-text)]">{state.message}</p>
      </div>
    )
  }

  return (
    <form action={formAction} className="space-y-3">
      <input type="hidden" name="id" value={approvalId} />
      <input type="hidden" name="approve" value={confirming === "approve" ? "true" : "false"} />
      <input type="hidden" name="decided_by" value="console@example.com" />

      {confirming ? (
        <div
          className={`rise rounded-lg border p-4 ${
            confirming === "approve"
              ? "border-[var(--color-ok)]/25 bg-[var(--color-ok-dim)]"
              : "border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)]"
          }`}
        >
          <p className="text-[13px] font-medium text-[var(--color-text)]">
            {confirming === "approve"
              ? financial
                ? `Authorise ${action.replace(/_/g, " ")} of £${amountGbp.toFixed(2)}${
                    resource ? ` on ${resource}` : ""
                  }?`
                : `Authorise ${action.replace(/_/g, " ")}${resource ? ` on ${resource}` : ""}?`
              : `Reject ${action.replace(/_/g, " ")}${resource ? ` on ${resource}` : ""}?`}
          </p>
          <p className="mt-1.5 text-xs leading-relaxed text-[var(--color-text-muted)]">
            {confirming === "approve"
              ? "This executes immediately against the commerce system and cannot be undone from here."
              : "The action will not run and the drafted reply will be voided. The ticket is escalated for manual handling."}
          </p>

          <label className="mt-3 block">
            <span className="text-[11px] uppercase tracking-[0.1em] text-[var(--color-text-faint)]">
              Reason {confirming === "reject" ? "(recorded in the audit log)" : "(optional)"}
            </span>
            <input
              name="note"
              type="text"
              maxLength={1000}
              placeholder={
                confirming === "reject" ? "e.g. customer already refunded by phone" : "Optional"
              }
              className="mt-1.5 w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 text-[13px] text-[var(--color-text)] placeholder:text-[var(--color-text-faint)] focus:border-[var(--color-accent)]/50 focus:outline-none"
            />
          </label>

          <div className="mt-3 flex items-center gap-2">
            <Submit variant={confirming}>
              {confirming === "approve" ? "Confirm & execute" : "Confirm rejection"}
            </Submit>
            <button
              type="button"
              onClick={() => setConfirming(null)}
              className="rounded-lg px-3 py-2 text-[13px] text-[var(--color-text-muted)] transition-colors duration-[var(--dur-fast)] hover:text-[var(--color-text)]"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setConfirming("approve")}
            className="inline-flex items-center gap-2 rounded-lg bg-[var(--color-ok)] px-4 py-2 text-[13px] font-medium text-[#06120c] transition-all duration-[var(--dur-fast)] hover:brightness-110 active:brightness-95"
          >
            Approve &amp; execute
          </button>
          <button
            type="button"
            onClick={() => setConfirming("reject")}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface-2)] px-4 py-2 text-[13px] font-medium text-[var(--color-text)] transition-all duration-[var(--dur-fast)] hover:border-[var(--color-danger)]/40 hover:text-[var(--color-danger)]"
          >
            Reject
          </button>
        </div>
      )}

      {state.status === "error" ? (
        <p className="rounded-lg border border-[var(--color-danger)]/25 bg-[var(--color-danger-dim)] px-3 py-2 text-xs text-[var(--color-danger)]">
          {state.message}
        </p>
      ) : null}
    </form>
  )
}
