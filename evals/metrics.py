"""Metrics.

The report *shape* is defined once, in `resolve.domain.evaluation`, and
imported here. The harness produces reports; the API serves them; neither owns
a private copy that could drift from the other.

Deliberately plain implementations rather than a scikit-learn dependency: the
eval harness must run in CI with the same minimal dependency set as the
service, and a confusion matrix is fifteen lines.
"""

from __future__ import annotations

import statistics
from collections import Counter

from evals.schema import CaseResult
from resolve.domain.evaluation import ClassMetrics, EvalReport


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1)))
    return round(ordered[index], 3)


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return round(precision, 4), round(recall, 4), round(f1, 4)


def build_report(
    results: list[CaseResult],
    *,
    provider: str,
    prompt_version: str,
    model_fast: str,
    model_reasoning: str,
    dataset: str,
    generated_at: str,
) -> EvalReport:
    report = EvalReport(
        provider=provider,
        prompt_version=prompt_version,
        model_fast=model_fast,
        model_reasoning=model_reasoning,
        dataset=dataset,
        generated_at=generated_at,
        total_cases=len(results),
    )
    if not results:
        return report

    n = len(results)
    report.passed = sum(1 for r in results if r.passed)
    report.pass_rate = round(report.passed / n, 4)
    report.intent_accuracy = round(sum(1 for r in results if r.intent_correct) / n, 4)
    report.outcome_accuracy = round(sum(1 for r in results if r.outcome_correct) / n, 4)

    # Confusion matrix + per-class precision/recall/F1
    labels = sorted(
        {r.expected_intent for r in results} | {r.actual_intent or "none" for r in results}
    )
    confusion: dict[str, dict[str, int]] = {a: dict.fromkeys(labels, 0) for a in labels}
    for r in results:
        confusion[r.expected_intent][r.actual_intent or "none"] += 1
    report.confusion = {
        k: {kk: vv for kk, vv in v.items() if vv} for k, v in confusion.items() if any(v.values())
    }

    expected_counts = Counter(r.expected_intent for r in results)
    per_class: list[ClassMetrics] = []
    for label in sorted(expected_counts):
        tp = sum(1 for r in results if r.expected_intent == label and r.actual_intent == label)
        fp = sum(1 for r in results if r.expected_intent != label and r.actual_intent == label)
        fn = sum(1 for r in results if r.expected_intent == label and r.actual_intent != label)
        precision, recall, f1 = _prf(tp, fp, fn)
        per_class.append(
            ClassMetrics(
                label=label,
                support=expected_counts[label],
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )
    report.per_intent = per_class
    report.intent_macro_f1 = (
        round(statistics.fmean(c.f1 for c in per_class), 4) if per_class else 0.0
    )

    # Tool selection, micro-averaged over every (case, tool) pair.
    # Precision answers "when it called a tool, should it have?"; recall
    # answers "of the tools it needed, how many did it call?".
    tp = fp = fn = 0
    for r in results:
        expected = set(r.expected_tools)
        called = set(r.tools_called)
        tp += len(expected & called)
        fp += len(called - expected)
        fn += len(expected - called)
    report.tool_precision, report.tool_recall, _ = _prf(tp, fp, fn)

    # Escalation: treat "did not auto-resolve" as the positive class. This is
    # the metric that matters most operationally — an incorrect auto-resolution
    # reaches a customer, an incorrect escalation costs a few minutes.
    esc_tp = sum(
        1 for r in results if r.expected_outcome != "resolved" and r.actual_outcome != "resolved"
    )
    esc_fp = sum(
        1 for r in results if r.expected_outcome == "resolved" and r.actual_outcome != "resolved"
    )
    esc_fn = sum(
        1 for r in results if r.expected_outcome != "resolved" and r.actual_outcome == "resolved"
    )
    report.escalation_precision, report.escalation_recall, _ = _prf(esc_tp, esc_fp, esc_fn)

    replied = [r for r in results if r.grounded is not None]
    if replied:
        report.groundedness_rate = round(sum(1 for r in replied if r.grounded) / len(replied), 4)
        report.citation_rate = round(
            sum(1 for r in replied if r.citation_count > 0) / len(replied), 4
        )

    adversarial = [r for r in results if r.adversarial]
    report.adversarial_cases = len(adversarial)
    report.adversarial_contained = sum(1 for r in adversarial if r.contained)
    report.containment_rate = (
        round(report.adversarial_contained / len(adversarial), 4) if adversarial else 1.0
    )

    costs = [r.cost_usd for r in results]
    latencies = [r.duration_ms for r in results]
    report.total_cost_usd = round(sum(costs), 6)
    report.mean_cost_usd = round(report.total_cost_usd / n, 6)
    report.p50_latency_ms = _percentile(latencies, 50)
    report.p95_latency_ms = _percentile(latencies, 95)

    report.failures = [r.case_id for r in results if not r.passed]
    return report


__all__ = ["ClassMetrics", "EvalReport", "build_report"]
