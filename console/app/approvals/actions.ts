"use server"

import { revalidatePath } from "next/cache"
import { api } from "@/lib/api"

export interface DecisionState {
  status: "idle" | "ok" | "error"
  message?: string
}

/**
 * Record a human decision.
 *
 * A Server Action, so the decision is made with the server's credentials. The
 * browser can *ask* for an action; it can never perform one. The API is the
 * only component with write authority, and it enforces the double-decision
 * guard — a second submission gets a 409 and is reported here rather than
 * silently swallowed.
 */
export async function decideApproval(
  _previous: DecisionState,
  formData: FormData,
): Promise<DecisionState> {
  const id = String(formData.get("id") ?? "")
  const approve = String(formData.get("approve") ?? "") === "true"
  const decidedBy = String(formData.get("decided_by") ?? "").trim() || "console@example.com"
  const note = String(formData.get("note") ?? "").trim() || undefined

  if (!id) return { status: "error", message: "Missing approval id." }

  try {
    const result = await api.decide(id, approve, decidedBy, note)
    revalidatePath("/approvals")
    revalidatePath("/activity")
    revalidatePath("/tickets")
    revalidatePath(`/tickets/${result.ticket_id}`)
    revalidatePath("/")

    return {
      status: "ok",
      message: approve
        ? result.executed
          ? `Approved and executed. Ticket ${result.ticket_status}.`
          : `Approved, but execution failed. Ticket ${result.ticket_status}.`
        : `Rejected. The action was not executed and the draft was ${result.reply_state}.`,
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : "The decision could not be recorded."
    // A 409 means someone else already decided this. Say so plainly rather
    // than letting the operator think their click did nothing.
    return {
      status: "error",
      message: message.includes("409")
        ? "This approval has already been decided by someone else."
        : message,
    }
  }
}
