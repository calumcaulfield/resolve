"""Reading evaluation reports from disk.

The evaluation harness writes `evals/results/latest-{provider}.json`. The API
serves the newest of those so the operations console can display it.

Why the console does not read the file itself
---------------------------------------------
It used to, and it was broken in Docker. The console image is built from
`./console`, so `evals/` is not in its filesystem at all — the page rendered
"No evaluation report found" on a stack where the report existed and the API
container could see it perfectly well. Reading a file that only one service
has is not a shared contract, it is a coincidence of local development.

So the report is served over the API like every other piece of state, and the
console is a pure consumer.

Honesty constraints, enforced here rather than assumed
------------------------------------------------------
* A missing report is reported as missing. It is never substituted with
  defaults, zeros, or a previous run.
* A report that exists but does not validate raises `InvalidReportError`. It
  is never partially parsed into something that looks plausible. A dashboard
  showing a number that no run produced is worse than a dashboard showing
  nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from resolve.domain.evaluation import EvalReport, EvalReportEnvelope
from resolve.logging import get_logger

log = get_logger(__name__)

#: The harness writes one file per provider, so a mock run and a live-model run
#: can coexist and be compared.
REPORT_GLOB = "latest-*.json"


class EvaluationStoreError(Exception):
    """Base class for report-reading failures."""


class NoReportError(EvaluationStoreError):
    """No evaluation has been run yet. An empty state, not an error condition."""


class InvalidReportError(EvaluationStoreError):
    """A report file exists but does not match the published schema."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path.name} does not match the evaluation report schema: {detail}")
        self.path = path
        self.detail = detail


def _parse_generated_at(report: EvalReport, fallback: Path) -> datetime:
    """Order reports by when the run happened, not by filename.

    Falls back to file mtime if `generated_at` is unparseable, so a slightly
    malformed timestamp degrades ordering rather than hiding the report.
    """
    try:
        parsed = datetime.fromisoformat(report.generated_at)
    except ValueError:
        return datetime.fromtimestamp(fallback.stat().st_mtime, tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class EvaluationStore:
    def __init__(self, results_dir: Path | str) -> None:
        self.results_dir = Path(results_dir)

    def available_providers(self) -> list[str]:
        """Providers that have a report on disk, e.g. `["mock", "anthropic"]`."""
        if not self.results_dir.is_dir():
            return []
        return sorted(p.stem.removeprefix("latest-") for p in self.results_dir.glob(REPORT_GLOB))

    def _load_one(self, path: Path) -> EvalReport:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidReportError(path, f"could not be read as JSON: {exc}") from exc
        try:
            return EvalReportEnvelope.model_validate(payload).report
        except ValidationError as exc:
            # Surfaced, never smoothed over: a report that does not validate is
            # a real problem with the harness or a stale file format.
            raise InvalidReportError(path, str(exc.errors()[:3])) from exc

    def latest(self, provider: str | None = None) -> tuple[EvalReport, Path]:
        """Return the most recent valid report, and the file it came from.

        Raises `NoReportError` if none exists, and `InvalidReportError` if the
        only candidate is malformed.
        """
        if not self.results_dir.is_dir():
            raise NoReportError(f"no evaluation results directory at {self.results_dir}")

        pattern = f"latest-{provider}.json" if provider else REPORT_GLOB
        candidates = sorted(self.results_dir.glob(pattern))
        if not candidates:
            raise NoReportError(f"no evaluation report matching {pattern!r} in {self.results_dir}")

        loaded: list[tuple[datetime, EvalReport, Path]] = []
        last_error: InvalidReportError | None = None
        for path in candidates:
            try:
                report = self._load_one(path)
            except InvalidReportError as exc:
                # One bad file must not hide the good ones, but if *every*
                # candidate is bad the caller hears about it.
                log.warning("evals.invalid_report", path=str(path), detail=exc.detail)
                last_error = exc
                continue
            loaded.append((_parse_generated_at(report, path), report, path))

        if not loaded:
            assert last_error is not None
            raise last_error

        loaded.sort(key=lambda item: item[0], reverse=True)
        _generated_at, report, path = loaded[0]
        return report, path
