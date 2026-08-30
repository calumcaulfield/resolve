"""The evaluation report format.

This is the **canonical** definition of an evaluation report, and it lives in
`resolve.domain` rather than in `evals/` for two reasons:

1. The eval harness *writes* reports and the API *serves* them. If each owned
   its own copy of the shape, they would drift, and the drift would surface as
   a dashboard quietly missing a metric rather than as a failure.
2. `evals/` is a top-level package that is not importable from the service
   container (the image puts `src/` on `PYTHONPATH`, not the repository root).
   Depending on it from the API would work in a dev checkout and break in
   Docker — exactly the class of bug this module was added to fix.

Dependency direction stays one-way: `evals` imports from `resolve`, never the
reverse.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClassMetrics(BaseModel):
    """Per-class precision, recall and F1 for one intent label."""

    label: str
    support: int
    precision: float
    recall: float
    f1: float


class EvalReport(BaseModel):
    """One evaluation run.

    `provider`, `prompt_version` and the two model ids are carried so a result
    can be attributed to an exact configuration. A number without them is not
    reproducible, and this project does not publish numbers it cannot re-derive.
    """

    provider: str
    prompt_version: str
    model_fast: str
    model_reasoning: str
    dataset: str
    generated_at: str

    total_cases: int = 0
    passed: int = 0
    pass_rate: float = 0.0

    intent_accuracy: float = 0.0
    intent_macro_f1: float = 0.0
    per_intent: list[ClassMetrics] = Field(default_factory=list)
    confusion: dict[str, dict[str, int]] = Field(default_factory=dict)

    outcome_accuracy: float = 0.0
    tool_precision: float = 0.0
    tool_recall: float = 0.0

    escalation_precision: float = 0.0
    escalation_recall: float = 0.0

    groundedness_rate: float = 0.0
    citation_rate: float = 0.0

    adversarial_cases: int = 0
    adversarial_contained: int = 0
    containment_rate: float = 1.0

    mean_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    failures: list[str] = Field(default_factory=list)


class EvalReportEnvelope(BaseModel):
    """The on-disk file: a report plus the per-case detail behind it.

    `cases` is typed loosely here on purpose. The API serves the summary; the
    case-level detail is the harness's own record and its shape is allowed to
    evolve without forcing a service change.
    """

    model_config = {"extra": "ignore"}

    report: EvalReport
    cases: list[dict[str, object]] = Field(default_factory=list)
