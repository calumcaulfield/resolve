"""Versioned prompt registry.

Prompts are code. They are versioned, reviewed in diffs, and referenced by
name so that an evaluation result can be attributed to an exact prompt
version. `PROMPT_VERSION` is recorded in every eval run — a quality change
with no prompt-version change means something else moved.
"""

from __future__ import annotations

from resolve.llm.prompts.templates import (
    PROMPT_VERSION,
    draft_reply_prompt,
    plan_prompt,
    triage_prompt,
    verify_prompt,
)

__all__ = [
    "PROMPT_VERSION",
    "draft_reply_prompt",
    "plan_prompt",
    "triage_prompt",
    "verify_prompt",
]
