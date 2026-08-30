"""A deterministic, keyless LLM provider.

Why this exists (ADR-002)
-------------------------
Every part of this system that touches a model — triage, planning, drafting,
verification — is exercised by the test suite, by `make demo`, and by the
evaluation harness. If those paths required a paid API key:

  * CI could not run them, so they would rot;
  * a reviewer cloning the repo could not see the system work;
  * evaluation results would be non-deterministic and unrepeatable.

So the default provider is this one: a rule-based implementation that satisfies
the same `LLMProvider` protocol, returns the same validated Pydantic schemas,
and produces the same output for the same input every time.

Honesty note
------------
This is a *baseline*, not a language model. Evaluation runs record which
provider produced them (`evals/results/*.json` carries a `provider` field) and
the README reports mock-provider and live-provider numbers separately. Mock
numbers measure the deterministic baseline and the surrounding machinery;
they are not a claim about frontier-model quality.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from resolve.llm.base import (
    LLMRequest,
    LLMResponse,
    StructuredOutputError,
    Usage,
)
from resolve.llm.pricing import cost_usd, estimate_tokens
from resolve.safety.injection import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

TModel = TypeVar("TModel", bound=BaseModel)

MODEL_NAME = "mock-deterministic"

ORDER_REF_RE = re.compile(r"\b((?:ORD|BG|UK|IE)[-_ ]?\d{4,10})\b", re.IGNORECASE)

# Extracting a replacement delivery address from free text.
#
# Anchoring on lead-in phrases ("change it to ...") turned out to be too
# brittle: customers write "change the address on ORD-1 to ..." just as often.
# So instead: find the sentence that *looks* like it contains an address, then
# trim the connector and the politeness off either end.
ADDRESS_SHAPE_RE = re.compile(
    r"[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}"  # UK/IE postcode
    r"|\[POSTCODE_\d+\]"  # ...or its redaction
    r"|\b(?:road|street|grove|avenue|lane|park|unit|drive|close|way)\b",
    re.IGNORECASE,
)
_LEAD_IN_RE = re.compile(r".*\b(?:to|instead|is|at)\b[:\s]+|.*:\s*", re.IGNORECASE | re.DOTALL)
_TRAILING_RE = re.compile(r"[\s,]*\b(?:please|thanks|thank you|cheers|ta)\b.*$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def extract_new_address(text: str) -> str:
    """Pull a replacement delivery address out of a customer message.

    Returns "" when nothing address-shaped is present, which the planner
    treats as a reason to escalate rather than to act on a guess.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        shape = ADDRESS_SHAPE_RE.search(sentence)
        if not shape:
            continue
        # Drop everything up to the last connector that precedes the address.
        head = sentence[: shape.start()]
        lead = _LEAD_IN_RE.match(head)
        candidate = sentence[lead.end() :] if lead else sentence
        candidate = _TRAILING_RE.sub("", candidate)
        candidate = " ".join(candidate.split()).strip(" ,.:;?")
        if len(candidate) >= 8 and ADDRESS_SHAPE_RE.search(candidate):
            return candidate
    return ""


# Ordered most-specific first: the first rule whose pattern matches wins.
# Ordering matters — "charged twice" must beat the generic "refund" rule.
_INTENT_RULES: list[tuple[str, str, float]] = [
    (
        "duplicate_charge",
        r"charged twice|double charge|two payments|duplicate charge|charged me 2",
        0.92,
    ),
    (
        "payment_failed",
        r"payment (failed|declined|didn'?t go through)"
        r"|card (was )?declined|couldn'?t pay|payment error",
        0.90,
    ),
    (
        "cancel_order",
        r"\bcancel(lation)?\b|don'?t want it any ?more|ordered it by mistake",
        0.88,
    ),
    (
        "change_address",
        r"change.{0,25}address|wrong address"
        r"|update.{0,20}delivery address|deliver(ed)? to a different",
        0.90,
    ),
    (
        "delivery_issue",
        r"not (arrived|received|delivered)"
        r"|hasn'?t (arrived|turned up|shown up)"
        r"|(never|nothing has) (arrived|turned up|showed up)"
        r"|damaged|smashed|broken|crushed|missing item|late delivery|lost parcel",
        0.86,
    ),
    ("refund_request", r"\brefund\b|money back|return this", 0.85),
    (
        "order_status",
        r"where is my order|order status|tracking"
        r"|any (news|update) on|update on (my )?order"
        r"|when will .{0,20}(arrive|ship)|has (it|my order) shipped|dispatch",
        0.88,
    ),
    (
        "product_question",
        r"ingredient|allerg|gluten|contain"
        r"|shelf ?life|best before|how long will .{0,25}(keep|last)"
        r"|fridge|refrigerat|storage|store it"
        r"|size|dimension|does it come with|what'?s in",
        0.80,
    ),
    (
        "complaint",
        r"unacceptable|appalling|disgust|worst|complain|furious|terrible service",
        0.75,
    ),
]

_URGENCY_RULES: list[tuple[str, str]] = [
    ("critical", r"legal|solicitor|chargeback|trading standards|ombudsman|fraud"),
    ("high", r"urgent|asap|immediately|tomorrow|birthday|funeral|wedding|today|charged twice"),
    ("low", r"no rush|whenever|just wondering|out of interest"),
]

_TOOLS_BY_INTENT: dict[str, list[str]] = {
    "order_status": ["lookup_order", "get_tracking"],
    "delivery_issue": ["lookup_order", "get_tracking"],
    "payment_failed": ["lookup_order", "create_payment_link"],
    "duplicate_charge": ["lookup_order", "list_payments"],
    "refund_request": ["lookup_order", "issue_refund"],
    "cancel_order": ["lookup_order", "cancel_order"],
    "change_address": ["lookup_order", "update_shipping_address"],
    "product_question": [],
    "complaint": ["lookup_order"],
    "other": [],
}


def _trim_to_sentence(text: str, limit: int) -> str:
    """Quote policy text without cutting a sentence in half."""
    body = " ".join(text.split())
    if len(body) <= limit:
        return body
    window = body[:limit]
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    return (window[: cut + 1] if cut > limit // 3 else window.rsplit(" ", 1)[0] + "...").strip()


def _seeded_float(material: str, lo: float, hi: float) -> float:
    """Deterministic pseudo-random in [lo, hi] — stable across runs and machines."""
    digest = hashlib.sha256(material.encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return round(lo + unit * (hi - lo), 4)


class MockProvider:
    """Rule-based stand-in satisfying `LLMProvider`."""

    name = "mock"

    def __init__(self, model: str = MODEL_NAME) -> None:
        self.model = model
        self.calls: list[LLMRequest] = []

    # -- protocol ---------------------------------------------------------

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        text = self._freeform(request)
        usage = self._usage(request, text)
        return LLMResponse(text=text, usage=usage, raw={"provider": "mock"})

    async def complete_structured(
        self, request: LLMRequest, schema: type[TModel]
    ) -> tuple[TModel, Usage]:
        self.calls.append(request)
        handler = {
            "triage": self._triage,
            "plan": self._plan,
            "draft_reply": self._draft_reply,
            "verify": self._verify,
        }.get(request.task)
        if handler is None:
            raise StructuredOutputError(f"mock provider has no handler for task {request.task!r}")

        payload = handler(request)
        try:
            value = schema.model_validate(payload)
        except Exception as exc:  # pragma: no cover - defensive
            raise StructuredOutputError(str(exc), payload=str(payload)) from exc

        usage = self._usage(request, str(payload))
        return value, usage

    # -- helpers ----------------------------------------------------------

    def _usage(self, request: LLMRequest, output: str) -> Usage:
        tin = estimate_tokens(request.system) + sum(
            estimate_tokens(m.content) for m in request.messages
        )
        tout = estimate_tokens(output)
        model = request.model or self.model
        return Usage(
            model=model,
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=cost_usd(model, tin, tout),
        )

    @staticmethod
    def _text(request: LLMRequest) -> str:
        return "\n".join(m.content for m in request.messages)

    @classmethod
    def _customer_text(cls, request: LLMRequest) -> str:
        """Only the fenced, customer-supplied part of the prompt.

        This matters. The planner prompt also carries the order context, which
        includes the order's *current* shipping address. An extractor that
        reads the whole prompt happily pulls that out and proposes "changing"
        the address to the one already on the order. Anything derived from the
        customer must come from inside the fence and nowhere else.
        """
        text = cls._text(request)
        start = text.find(UNTRUSTED_OPEN)
        end = text.find(UNTRUSTED_CLOSE)
        if start == -1 or end == -1 or end < start:
            return text
        return text[start + len(UNTRUSTED_OPEN) : end].strip()

    # -- task handlers ----------------------------------------------------

    def _triage(self, request: LLMRequest) -> dict[str, Any]:
        body = self._customer_text(request)
        lowered = body.lower()

        intent, confidence = "other", 0.45
        for candidate, pattern, conf in _INTENT_RULES:
            if re.search(pattern, lowered):
                intent, confidence = candidate, conf
                break

        urgency = "normal"
        for candidate, pattern in _URGENCY_RULES:
            if re.search(pattern, lowered):
                urgency = candidate
                break

        match = ORDER_REF_RE.search(body)
        order_ref = re.sub(r"[-_ ]", "-", match.group(1)).upper() if match else None

        first_sentence = re.split(r"(?<=[.!?])\s+", body.strip())[0][:200]
        sentiment = (
            "frustrated"
            if re.search(r"unacceptable|furious|appalling|terrible|angry|disappointed", lowered)
            else "neutral"
        )

        return {
            "intent": intent,
            "urgency": urgency,
            "confidence": confidence,
            "order_ref": order_ref,
            "summary": (
                f"Customer message classified as {intent.replace('_', ' ')}: {first_sentence}"
            ),
            "customer_sentiment": sentiment,
        }

    def _plan(self, request: LLMRequest) -> dict[str, Any]:
        intent = str(request.metadata.get("intent", "other"))
        order_ref = request.metadata.get("order_ref")
        confidence = float(request.metadata.get("confidence", 0.5))

        if intent == "other" or confidence < 0.5:
            return {
                "actions": [],
                "reasoning": "Intent could not be established with enough confidence to act.",
                "needs_human": True,
                "human_reason": "Unclassified or low-confidence request.",
            }

        # Published service policy routes complaints to a named human. This is
        # a business rule, not a model limitation: an automated reply to
        # "your service has been appalling" makes the situation worse.
        if intent == "complaint":
            return {
                "actions": [],
                "reasoning": "Service policy requires complaints to be handled by a person.",
                "needs_human": True,
                "human_reason": "Complaint — customer service standards require human handling.",
            }

        # Questions answerable from published policy alone need no tools, but
        # they are still fully resolvable. This is the pure-retrieval path.
        if intent == "product_question":
            return {
                "actions": [],
                "reasoning": "Answerable directly from the published product and allergen policy.",
                "needs_human": False,
                "human_reason": None,
            }

        actions: list[dict[str, Any]] = []
        for tool in _TOOLS_BY_INTENT.get(intent, []):
            args: dict[str, Any] = {}
            if tool in {
                "lookup_order",
                "get_tracking",
                "list_payments",
                "cancel_order",
                "issue_refund",
                "create_payment_link",
                "update_shipping_address",
            }:
                if not order_ref:
                    # Cannot act without an order. Hand to a human rather than guess.
                    return {
                        "actions": [],
                        "reasoning": "No order reference present in the message.",
                        "needs_human": True,
                        "human_reason": (
                            "Order reference missing; cannot verify the customer's order."
                        ),
                    }
                args["order_ref"] = order_ref
            if tool == "issue_refund":
                args["amount_gbp"] = float(request.metadata.get("order_total_gbp", 0.0))
                args["reason"] = "Customer refund request"
            if tool == "update_shipping_address":
                # Read the address out of the message itself; the planner sees
                # the full customer text, so there is nothing to guess.
                address = extract_new_address(self._customer_text(request))
                if not address:
                    return {
                        "actions": [],
                        "reasoning": "The customer asked to change the address but did not "
                        "give a usable replacement.",
                        "needs_human": True,
                        "human_reason": "No replacement address could be read from the message.",
                    }
                args["new_address"] = address
            actions.append(
                {
                    "tool": tool,
                    "arguments": args,
                    "rationale": f"Required to handle a {intent.replace('_', ' ')} request.",
                }
            )

        return {
            "actions": actions,
            "reasoning": f"Standard resolution path for {intent.replace('_', ' ')}.",
            "needs_human": not actions,
            "human_reason": None if actions else "No automated path exists for this intent.",
        }

    def _draft_reply(self, request: LLMRequest) -> dict[str, Any]:
        meta = request.metadata
        intent = str(meta.get("intent", "other"))
        customer = str(meta.get("customer_name") or "there")
        order_ref = meta.get("order_ref")
        facts: dict[str, Any] = dict(meta.get("facts") or {})
        chunks: list[dict[str, str]] = list(meta.get("chunks") or [])

        lines = [f"Hi {customer.split()[0] if customer.strip() else 'there'},", ""]

        if intent in {"order_status", "delivery_issue"}:
            status = facts.get("status", "being processed")
            lines.append(f"Thanks for getting in touch about order {order_ref}.")
            lines.append(f"Your order is currently {str(status).replace('_', ' ')}.")
            if facts.get("tracking_number"):
                lines.append(
                    f"You can track it with {facts.get('carrier', 'the carrier')} "
                    f"using reference {facts['tracking_number']}."
                )
            if facts.get("estimated_delivery"):
                lines.append(f"Estimated delivery is {facts['estimated_delivery']}.")
        elif intent == "payment_failed":
            lines.append(f"Sorry the payment for order {order_ref} didn't go through.")
            if facts.get("payment_link"):
                lines.append(
                    f"You can complete it securely here: {facts['payment_link']} "
                    "The link is valid for 24 hours."
                )
        elif intent == "duplicate_charge":
            lines.append(
                f"Thanks for flagging this — I've checked the payments on order {order_ref}."
            )
            succeeded = int(facts.get("succeeded_count", 0) or 0)
            if facts.get("duplicate_suspected"):
                lines.append(
                    f"I can see {succeeded} settled payments against this order. "
                    "I've raised a refund for the duplicate with our payments team, "
                    "and it will be back with you within 3-5 working days."
                )
            else:
                lines.append(
                    f"I can only see {succeeded} settled payment against this order. "
                    "The second charge you can see is most likely a pre-authorisation "
                    "that has not yet cleared."
                )
        elif intent == "refund_request":
            lines.append(f"I've looked into your refund request for order {order_ref}.")
            if facts.get("refund_amount_gbp"):
                lines.append(
                    f"A refund of £{facts['refund_amount_gbp']:.2f} has been processed and will "
                    "appear on your original payment method within 3-5 working days."
                )
        elif intent == "cancel_order":
            lines.append(f"Order {order_ref} has been cancelled as requested.")
        elif intent == "change_address":
            lines.append(f"The delivery address on order {order_ref} has been updated.")
        elif intent == "product_question":
            lines.append("Thanks for getting in touch — here is what our published policy says.")
        else:
            lines.append("Thanks for your message — I've passed this to a colleague who can help.")

        if chunks:
            lines.append("")
            lines.append(_trim_to_sentence(str(chunks[0].get("text", "")), 320))

        lines.extend(["", "Best regards,", "Customer Care"])

        citations = [
            {
                "document_id": c.get("document_id", ""),
                "chunk_id": c.get("chunk_id", ""),
                "title": c.get("title", ""),
                "quote": str(c.get("text", ""))[:480],
            }
            for c in chunks[:2]
        ]

        return {
            "subject": f"Re: {meta.get('subject') or 'Your order'}",
            "body": "\n".join(lines),
            "citations": citations,
            "confidence": _seeded_float(request.cache_key_material(), 0.72, 0.94),
            "tone": "professional",
        }

    def _verify(self, request: LLMRequest) -> dict[str, Any]:
        meta = request.metadata
        reply_body = str(meta.get("reply_body", ""))
        citations = list(meta.get("citations") or [])
        known_facts = {str(v).lower() for v in (meta.get("facts") or {}).values()}

        unsupported: list[str] = []
        # A crude but honest groundedness check: any monetary figure or
        # tracking-shaped token in the reply must appear in the retrieved
        # facts. This is exactly the class of claim that hurts when wrong.
        for token in re.findall(r"£\d+(?:\.\d{2})?|\b[A-Z]{2}\d{9}[A-Z]{2}\b", reply_body):
            needle = token.lower().lstrip("£")
            if needle not in " ".join(known_facts) and not any(
                needle in fact for fact in known_facts
            ):
                unsupported.append(token)

        grounded = not unsupported and bool(citations or not known_facts)
        return {
            "grounded": grounded,
            "hallucination_risk": 0.05 if grounded else 0.65,
            "unsupported_claims": unsupported,
            "policy_violations": [],
            "verdict": (
                "Reply is consistent with retrieved order data and cited policy."
                if grounded
                else "Reply contains figures not supported by retrieved data."
            ),
        }

    def _freeform(self, request: LLMRequest) -> str:
        return f"[mock:{request.task}] {self._text(request)[:200]}"
