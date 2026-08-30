from __future__ import annotations

import pytest

from resolve.bootstrap import Application, build_application
from resolve.bus.memory import InMemoryBus
from resolve.config import Settings
from resolve.runtime import Runtime, build_runtime
from resolve.seed.generate import build_dataset


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Local settings: deterministic provider, throwaway SQLite database."""
    s = Settings()
    return s.model_copy(
        update={
            "log_level": "ERROR",
            "db": s.db.model_copy(update={"url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"}),
        }
    )


@pytest.fixture
def dataset():
    return build_dataset()


@pytest.fixture
async def application(settings: Settings, dataset) -> Application:
    return await build_application(settings, dataset=dataset)


@pytest.fixture
async def runtime(settings: Settings, dataset) -> Runtime:
    rt = await build_runtime(settings, bus=InMemoryBus(claim_idle_seconds=0.0), dataset=dataset)
    try:
        yield rt
    finally:
        await rt.close()
