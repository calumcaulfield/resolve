"""Serving evaluation reports over the API.

The console used to read `evals/results/*.json` off the filesystem. That worked
in a dev checkout and failed silently in Docker, because the console image is
built from `./console` and never contains `evals/`. The page reported "No
evaluation report found" on a stack where the report existed.

These tests pin the replacement: the report is served by the API, validated
against a schema, and — most importantly — never fabricated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from resolve.api.app import create_app
from resolve.domain.evaluation import EvalReport
from resolve.evaluation_store import (
    EvaluationStore,
    InvalidReportError,
    NoReportError,
)

AUTH = {"x-api-key": "dev-local-key"}


def make_report(provider: str = "mock", generated_at: str | None = None, **overrides) -> dict:
    report = {
        "provider": provider,
        "prompt_version": "2026-08-19.1",
        "model_fast": "claude-haiku-4-5",
        "model_reasoning": "claude-opus-5",
        "dataset": "synthetic-v1 (30 cases)",
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "total_cases": 30,
        "passed": 30,
        "pass_rate": 1.0,
        "intent_accuracy": 1.0,
        "intent_macro_f1": 1.0,
        "per_intent": [
            {"label": "order_status", "support": 4, "precision": 1.0, "recall": 1.0, "f1": 1.0}
        ],
        "confusion": {"order_status": {"order_status": 4}},
        "outcome_accuracy": 1.0,
        "tool_precision": 0.909,
        "tool_recall": 1.0,
        "escalation_precision": 1.0,
        "escalation_recall": 1.0,
        "groundedness_rate": 1.0,
        "citation_rate": 1.0,
        "adversarial_cases": 5,
        "adversarial_contained": 5,
        "containment_rate": 1.0,
        "mean_cost_usd": 0.017482,
        "total_cost_usd": 0.52446,
        "p50_latency_ms": 2.0,
        "p95_latency_ms": 3.0,
        "failures": [],
    }
    report.update(overrides)
    return {"report": report, "cases": [{"case_id": "status-001"}]}


def write_report(results_dir: Path, provider: str = "mock", **kwargs) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"latest-{provider}.json"
    path.write_text(json.dumps(make_report(provider, **kwargs), indent=2))
    return path


@pytest.fixture
def results_dir(tmp_path) -> Path:
    return tmp_path / "evals" / "results"


@pytest.fixture
async def client(runtime, results_dir):  # type: ignore[no-untyped-def]
    runtime.settings.evals.results_dir = results_dir
    app = create_app()
    app.state.runtime = runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


class TestStore:
    def test_missing_directory_is_an_empty_state(self, results_dir) -> None:
        with pytest.raises(NoReportError):
            EvaluationStore(results_dir).latest()

    def test_empty_directory_is_an_empty_state(self, results_dir) -> None:
        results_dir.mkdir(parents=True)
        with pytest.raises(NoReportError):
            EvaluationStore(results_dir).latest()

    def test_reads_and_validates_a_report(self, results_dir) -> None:
        write_report(results_dir)
        report, path = EvaluationStore(results_dir).latest()
        assert isinstance(report, EvalReport)
        assert report.provider == "mock"
        assert path.name == "latest-mock.json"

    def test_picks_the_newest_run_not_the_first_filename(self, results_dir) -> None:
        older = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        newer = datetime.now(UTC).isoformat(timespec="seconds")
        # "anthropic" sorts before "mock", so filename order would pick the wrong one.
        write_report(results_dir, "anthropic", generated_at=older)
        write_report(results_dir, "mock", generated_at=newer)
        report, _ = EvaluationStore(results_dir).latest()
        assert report.provider == "mock"

    def test_provider_filter(self, results_dir) -> None:
        write_report(results_dir, "mock")
        write_report(results_dir, "anthropic")
        report, _ = EvaluationStore(results_dir).latest("anthropic")
        assert report.provider == "anthropic"

    def test_unknown_provider_is_an_empty_state(self, results_dir) -> None:
        write_report(results_dir, "mock")
        with pytest.raises(NoReportError):
            EvaluationStore(results_dir).latest("openai")

    def test_malformed_json_raises_rather_than_returning_nothing(self, results_dir) -> None:
        results_dir.mkdir(parents=True)
        (results_dir / "latest-mock.json").write_text("{ not json")
        with pytest.raises(InvalidReportError):
            EvaluationStore(results_dir).latest()

    def test_a_report_missing_required_fields_is_rejected(self, results_dir) -> None:
        """Never partially parsed into something that looks like results."""
        results_dir.mkdir(parents=True)
        (results_dir / "latest-mock.json").write_text(
            json.dumps({"report": {"provider": "mock"}, "cases": []})
        )
        with pytest.raises(InvalidReportError):
            EvaluationStore(results_dir).latest()

    def test_one_bad_file_does_not_hide_a_good_one(self, results_dir) -> None:
        write_report(results_dir, "mock")
        (results_dir / "latest-broken.json").write_text("{ not json")
        report, path = EvaluationStore(results_dir).latest()
        assert report.provider == "mock"
        assert path.name == "latest-mock.json"

    def test_available_providers(self, results_dir) -> None:
        write_report(results_dir, "mock")
        write_report(results_dir, "anthropic")
        assert EvaluationStore(results_dir).available_providers() == ["anthropic", "mock"]

    def test_unparseable_timestamp_falls_back_to_mtime(self, results_dir) -> None:
        write_report(results_dir, "mock", generated_at="not-a-timestamp")
        report, _ = EvaluationStore(results_dir).latest()
        assert report.provider == "mock"


class TestEndpoint:
    async def test_requires_an_api_key(self, client) -> None:
        assert (await client.get("/v1/evals/latest")).status_code == 401

    async def test_404_when_no_evaluation_has_run(self, client) -> None:
        response = await client.get("/v1/evals/latest", headers=AUTH)
        assert response.status_code == 404
        assert "make eval" in response.json()["detail"]

    async def test_returns_the_report_with_provenance(self, client, results_dir) -> None:
        write_report(results_dir)
        response = await client.get("/v1/evals/latest", headers=AUTH)
        assert response.status_code == 200

        body = response.json()
        assert body["source"] == "latest-mock.json"
        assert body["available_providers"] == ["mock"]
        assert body["report"]["provider"] == "mock"
        assert body["report"]["prompt_version"] == "2026-08-19.1"

    async def test_serves_every_field_the_console_renders(self, client, results_dir) -> None:
        """Guards the contract: a field the dashboard shows must be served."""
        write_report(results_dir)
        report = (await client.get("/v1/evals/latest", headers=AUTH)).json()["report"]
        for field in (
            "passed",
            "total_cases",
            "pass_rate",
            "intent_accuracy",
            "intent_macro_f1",
            "outcome_accuracy",
            "tool_precision",
            "tool_recall",
            "escalation_precision",
            "groundedness_rate",
            "citation_rate",
            "adversarial_cases",
            "adversarial_contained",
            "mean_cost_usd",
            "total_cost_usd",
            "p50_latency_ms",
            "p95_latency_ms",
            "per_intent",
            "failures",
            "model_fast",
            "model_reasoning",
            "dataset",
            "generated_at",
        ):
            assert field in report, f"console renders {field!r} but the API does not serve it"

    async def test_provider_query_parameter(self, client, results_dir) -> None:
        write_report(results_dir, "mock")
        write_report(results_dir, "anthropic")
        body = (await client.get("/v1/evals/latest?provider=anthropic", headers=AUTH)).json()
        assert body["report"]["provider"] == "anthropic"
        assert sorted(body["available_providers"]) == ["anthropic", "mock"]

    async def test_a_malformed_report_is_a_server_error_not_an_empty_state(
        self, client, results_dir
    ) -> None:
        """A broken report must never be indistinguishable from no report."""
        results_dir.mkdir(parents=True)
        (results_dir / "latest-mock.json").write_text("{ not json")
        response = await client.get("/v1/evals/latest", headers=AUTH)
        assert response.status_code == 500
        assert "not valid" in response.json()["detail"]

    async def test_no_metrics_are_invented(self, client, results_dir) -> None:
        """Every served number must be exactly what the file contained."""
        write_report(results_dir, intent_accuracy=0.8333, passed=25, pass_rate=0.8333)
        report = (await client.get("/v1/evals/latest", headers=AUTH)).json()["report"]
        assert report["intent_accuracy"] == 0.8333
        assert report["passed"] == 25
        assert report["pass_rate"] == 0.8333


class TestHarnessContract:
    """The harness writes; the API reads. This pins them together."""

    def test_a_real_harness_report_validates_against_the_served_schema(self, tmp_path) -> None:
        from evals.metrics import build_report
        from evals.schema import CaseResult

        results = [
            CaseResult(
                case_id="status-001",
                expected_intent="order_status",
                actual_intent="order_status",
                intent_correct=True,
                expected_outcome="resolved",
                actual_outcome="resolved",
                outcome_correct=True,
                grounded=True,
                citation_count=2,
                cost_usd=0.0134,
                duration_ms=3.0,
            )
        ]
        report = build_report(
            results,
            provider="mock",
            prompt_version="2026-08-19.1",
            model_fast="claude-haiku-4-5",
            model_reasoning="claude-opus-5",
            dataset="synthetic-v1 (1 case)",
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

        results_dir = tmp_path / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "latest-mock.json").write_text(
            json.dumps({"report": report.model_dump(), "cases": [r.model_dump() for r in results]})
        )

        served, _ = EvaluationStore(results_dir).latest()
        assert served.provider == "mock"
        assert served.total_cases == 1
        assert served.intent_accuracy == 1.0

    def test_the_harness_and_the_api_share_one_model(self) -> None:
        """Not two definitions that happen to agree — literally the same class."""
        from evals.metrics import EvalReport as HarnessReport

        assert HarnessReport is EvalReport
