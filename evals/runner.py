"""Evaluation harness.

    python -m evals.runner                     # run the golden set
    python -m evals.runner --provider anthropic
    python -m evals.runner --gate              # non-zero exit on regression

The gate is what makes this more than a report: CI runs it on every pull
request and fails the build if intent accuracy, escalation precision or
adversarial containment falls below the thresholds in `evals/thresholds.yaml`.
A prompt change that quietly makes classification worse cannot merge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evals.metrics import EvalReport, build_report
from evals.schema import CaseResult, GoldenCase
from resolve.bootstrap import build_application
from resolve.config import get_settings
from resolve.domain.enums import TicketStatus
from resolve.domain.schemas import Ticket
from resolve.llm.prompts import PROMPT_VERSION
from resolve.logging import configure_logging

EVALS_DIR = Path(__file__).parent
GOLDEN_DIR = EVALS_DIR / "golden"
RESULTS_DIR = EVALS_DIR / "results"

#: Outcomes that are *not* an automated resolution. Both are safe failures.
_NON_RESOLVED = {TicketStatus.AWAITING_APPROVAL, TicketStatus.ESCALATED, TicketStatus.FAILED}


def load_cases(paths: list[Path] | None = None) -> list[GoldenCase]:
    files = paths or sorted(GOLDEN_DIR.glob("*.yaml"))
    cases: list[GoldenCase] = []
    for path in files:
        payload = yaml.safe_load(path.read_text())
        for raw in payload.get("cases", []):
            cases.append(GoldenCase.model_validate(raw))
    if not cases:
        raise SystemExit(f"no golden cases found in {GOLDEN_DIR}")
    return cases


async def run_case(application: Any, case: GoldenCase) -> CaseResult:
    ticket = Ticket(
        external_id=case.id,
        channel="email",  # type: ignore[arg-type]
        from_email="customer@example.com",
        subject=case.subject,
        body=case.body,
    )
    outcome = await application.agent.run(ticket)

    tools_called = [t.tool for t in outcome.tool_results if t.ok]
    reply_text = outcome.reply.body if outcome.reply else ""

    forbidden_called = [t for t in case.forbidden_tools if t in tools_called]
    # An adversarial case is contained if the agent did not auto-resolve it
    # and executed nothing it was forbidden to execute.
    contained = (not case.adversarial) or (outcome.status in _NON_RESOLVED and not forbidden_called)

    return CaseResult(
        case_id=case.id,
        expected_intent=case.expected_intent,
        actual_intent=outcome.intent.value if outcome.intent else None,
        intent_correct=bool(outcome.intent and outcome.intent.value == case.expected_intent),
        expected_outcome=case.expected_outcome,
        actual_outcome=outcome.status.value,
        outcome_correct=outcome.status.value == case.expected_outcome,
        expected_tools=list(case.expected_tools),
        tools_called=tools_called,
        missing_tools=[t for t in case.expected_tools if t not in tools_called],
        forbidden_tools_called=forbidden_called,
        grounded=outcome.verification.grounded if outcome.verification else None,
        citation_count=len(outcome.reply.citations) if outcome.reply else 0,
        missing_mentions=[
            m for m in case.must_mention if reply_text and m.lower() not in reply_text.lower()
        ],
        prohibited_mentions=[
            m for m in case.must_not_mention if reply_text and m.lower() in reply_text.lower()
        ],
        adversarial=case.adversarial,
        contained=contained,
        cost_usd=outcome.total_cost_usd,
        duration_ms=outcome.duration_ms,
        escalation_reason=outcome.escalation_reason,
    )


async def run_evaluation(provider: str | None = None) -> tuple[EvalReport, list[CaseResult]]:
    settings = get_settings()
    if provider:
        settings = settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"provider": provider})}
        )
    application = await build_application(settings)
    cases = load_cases()

    results = [await run_case(application, case) for case in cases]

    report = build_report(
        results,
        provider=settings.llm.provider,
        prompt_version=PROMPT_VERSION,
        model_fast=settings.llm.triage_model,
        model_reasoning=settings.llm.reasoning_model,
        dataset=f"synthetic-v1 ({len(cases)} cases)",
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return report, results


def load_thresholds() -> dict[str, float]:
    path = EVALS_DIR / "thresholds.yaml"
    if not path.exists():
        return {}
    return dict(yaml.safe_load(path.read_text()).get("minimums", {}))


def check_gate(report: EvalReport) -> list[str]:
    violations: list[str] = []
    for metric, minimum in load_thresholds().items():
        actual = getattr(report, metric, None)
        if actual is None:
            violations.append(f"unknown threshold metric {metric!r}")
        elif float(actual) + 1e-9 < float(minimum):
            violations.append(f"{metric}: {actual:.4f} < required {minimum:.4f}")
    return violations


def render(report: EvalReport, results: list[CaseResult]) -> str:
    lines = [
        "",
        "=" * 78,
        f"  RESOLVE EVALUATION — provider={report.provider}  prompt={report.prompt_version}",
        f"  models: fast={report.model_fast}  reasoning={report.model_reasoning}",
        f"  dataset: {report.dataset}   generated: {report.generated_at}",
        "=" * 78,
        "",
        f"  Cases passed .............. {report.passed}/{report.total_cases}"
        f"  ({report.pass_rate:.1%})",
        f"  Intent accuracy ........... {report.intent_accuracy:.1%}",
        f"  Intent macro-F1 ........... {report.intent_macro_f1:.3f}",
        f"  Outcome accuracy .......... {report.outcome_accuracy:.1%}",
        f"  Tool precision / recall ... {report.tool_precision:.1%} / {report.tool_recall:.1%}",
        f"  Escalation precision ...... {report.escalation_precision:.1%}",
        f"  Escalation recall ......... {report.escalation_recall:.1%}",
        f"  Groundedness .............. {report.groundedness_rate:.1%}",
        f"  Replies with citations .... {report.citation_rate:.1%}",
        f"  Adversarial contained ..... {report.adversarial_contained}"
        f"/{report.adversarial_cases}  ({report.containment_rate:.1%})",
        "",
        f"  Mean cost / ticket ........ ${report.mean_cost_usd:.6f}",
        f"  Total cost ................ ${report.total_cost_usd:.5f}",
        f"  Latency p50 / p95 ......... {report.p50_latency_ms:.0f}ms / "
        f"{report.p95_latency_ms:.0f}ms",
        "",
        "  Per-intent",
        "  " + "-" * 62,
        f"  {'intent':<20}{'n':>4}{'precision':>12}{'recall':>10}{'f1':>8}",
    ]
    for c in report.per_intent:
        lines.append(
            f"  {c.label:<20}{c.support:>4}{c.precision:>12.3f}{c.recall:>10.3f}{c.f1:>8.3f}"
        )

    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", f"  Failures ({len(failed)})", "  " + "-" * 62]
        for r in failed:
            reasons = []
            if not r.intent_correct:
                reasons.append(f"intent {r.actual_intent} != {r.expected_intent}")
            if not r.outcome_correct:
                reasons.append(f"outcome {r.actual_outcome} != {r.expected_outcome}")
            if r.missing_tools:
                reasons.append(f"missing tools {r.missing_tools}")
            if r.forbidden_tools_called:
                reasons.append(f"FORBIDDEN tools called {r.forbidden_tools_called}")
            if r.missing_mentions:
                reasons.append(f"missing text {r.missing_mentions}")
            if r.prohibited_mentions:
                reasons.append(f"PROHIBITED text {r.prohibited_mentions}")
            if not r.contained:
                reasons.append("ADVERSARIAL NOT CONTAINED")
            lines.append(f"  {r.case_id:<22} {'; '.join(reasons)}")

    lines += ["", "=" * 78, ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Resolve evaluation suite")
    parser.add_argument("--provider", default=None, help="Override the LLM provider")
    parser.add_argument("--gate", action="store_true", help="Exit non-zero on threshold violation")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument("--out", default=None, help="Write the report to this path")
    parser.add_argument("--verbose", action="store_true", help="Show agent logs")
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else "ERROR", json_output=False)
    report, results = asyncio.run(run_evaluation(args.provider))

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render(report, results))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"latest-{report.provider}.json"
    out.write_text(
        json.dumps(
            {
                "report": report.model_dump(),
                "cases": [r.model_dump() for r in results],
            },
            indent=2,
        )
    )
    print(f"report written to {out}")

    if args.gate:
        violations = check_gate(report)
        if violations:
            print("\nQUALITY GATE FAILED:")
            for v in violations:
                print(f"  - {v}")
            return 1
        print("\nQuality gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
