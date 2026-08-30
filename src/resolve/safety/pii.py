"""PII redaction before text leaves the trust boundary.

Customer messages contain card fragments, phone numbers, full postal addresses
and occasionally passwords people have typed into the wrong box. None of that
is needed to classify a ticket or draft a reply, so none of it is sent to a
model provider.

Redaction is reversible within the process: each removed value is replaced by
a stable placeholder and the mapping is returned, so a drafted reply can have
real values restored locally before it is sent to the customer. The provider
never sees them.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from pydantic import BaseModel, Field


class _Rule(NamedTuple):
    label: str
    pattern: re.Pattern[str]


# Order matters: card numbers before generic long digit runs.
_RULES: list[_Rule] = [
    _Rule("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    _Rule("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    _Rule("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    _Rule(
        "PHONE",
        re.compile(r"(?<!\w)(?:\+44|0)\s?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\w)"),
    ),
    _Rule(
        "POSTCODE",
        re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b", re.IGNORECASE),
    ),
    _Rule("SORTCODE", re.compile(r"\b\d{2}-\d{2}-\d{2}\b")),
]

# Never redact these — they are the identifiers the agent needs to do its job.
_PRESERVE = re.compile(r"\b(?:ORD|BG|UK|IE)[-_ ]?\d{4,10}\b", re.IGNORECASE)


class RedactionResult(BaseModel):
    text: str
    mapping: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)

    def restore(self, text: str) -> str:
        """Put the real values back, for text that goes to the customer."""
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text

    @property
    def redacted_any(self) -> bool:
        return bool(self.mapping)


def redact(text: str) -> RedactionResult:
    """Replace PII with stable placeholders such as `[EMAIL_1]`."""
    protected: dict[str, str] = {}

    def _protect(match: re.Match[str]) -> str:
        token = f"\x00P{len(protected)}\x00"
        protected[token] = match.group(0)
        return token

    working = _PRESERVE.sub(_protect, text)

    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    seen: dict[str, str] = {}

    for label, pattern in _RULES:

        def _replace(match: re.Match[str], label: str = label) -> str:
            value = match.group(0)
            if value in seen:
                return seen[value]
            counts[label] = counts.get(label, 0) + 1
            placeholder = f"[{label}_{counts[label]}]"
            mapping[placeholder] = value
            seen[value] = placeholder
            return placeholder

        working = pattern.sub(_replace, working)

    for token, original in protected.items():
        working = working.replace(token, original)

    return RedactionResult(text=working, mapping=mapping, counts=counts)
