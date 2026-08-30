"""Application configuration.

Every setting is validated at import time by Pydantic. A missing or malformed
value fails loudly at startup rather than at 3am inside a worker.

Design note (see TECHNICAL_DECISIONS.md, ADR-002): the defaults are chosen so
that a clean checkout runs end-to-end with *no third-party API keys*. The
default LLM provider is deterministic and local; the default embedder is a
local hashing embedder. Nothing here phones home unless you ask it to.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMSettings(BaseSettings):
    """LLM provider configuration and the cost/safety envelope around it."""

    model_config = SettingsConfigDict(env_prefix="RESOLVE_LLM_", extra="ignore")

    provider: Literal["mock", "anthropic", "openai"] = "mock"

    # Two-tier model routing: a cheap model classifies, a strong model drafts.
    # See ADR-006.
    triage_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-opus-5"

    api_key: str | None = None
    base_url: str | None = None

    max_output_tokens: int = 2048
    temperature: float = 0.0
    request_timeout_seconds: float = 60.0
    max_retries: int = 3

    # Hard ceiling per ticket. The agent loop aborts and escalates rather than
    # spending past this. Expressed in USD.
    budget_usd_per_ticket: float = 0.25

    # Semantic response cache. Identical prompts inside the TTL are free.
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600

    @field_validator("api_key")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v or None


class AgentSettings(BaseSettings):
    """Bounds on autonomous behaviour."""

    model_config = SettingsConfigDict(env_prefix="RESOLVE_AGENT_", extra="ignore")

    max_steps: int = 8
    max_tool_calls: int = 6
    max_retrieval_chunks: int = 6

    # Monetary ceiling above which a refund always requires a human, regardless
    # of what the model concluded. See safety/policy.py.
    refund_auto_approve_limit_gbp: float = 25.0

    # A drafted reply must cite at least this many retrieved policy chunks or
    # it is rejected by the verifier and escalated. See ADR-005.
    min_citations_for_auto_send: int = 1

    # Confidence below which the agent escalates instead of acting.
    min_confidence_for_auto_action: float = 0.70


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESOLVE_DB_", extra="ignore")

    url: str = "postgresql+asyncpg://resolve:resolve@localhost:5432/resolve"
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESOLVE_REDIS_", extra="ignore")

    url: str = "redis://localhost:6379/0"
    consumer_group: str = "resolve"
    # How long a message may be pending before another consumer may claim it.
    claim_idle_ms: int = 60_000
    block_ms: int = 5_000
    batch_size: int = 10


class SecuritySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESOLVE_SECURITY_", extra="ignore")

    api_keys: list[str] = Field(default_factory=lambda: ["dev-local-key"])
    # Outbound webhooks may only target these hosts. Closes the SSRF class of
    # bug: a control plane that fetches any URL a caller supplies can be made
    # to probe internal services from inside the network.
    webhook_allowed_hosts: list[str] = Field(default_factory=lambda: ["example.com"])
    # Development default. `assert_production_safe()` refuses to boot with it.
    hmac_secret: str = "dev-local-hmac-secret"  # noqa: S105 - not a real secret
    # Customer text is untrusted input. Redact before it reaches any provider.
    redact_pii_before_llm: bool = True


class EvaluationSettings(BaseSettings):
    """Where the API looks for evaluation reports written by `make eval`.

    The default is relative, which resolves correctly both in the container
    (WORKDIR `/app`, so `/app/evals/results`) and in a repository checkout when
    commands are run from the project root.
    """

    model_config = SettingsConfigDict(env_prefix="RESOLVE_EVALS_", extra="ignore")

    results_dir: Path = Path("evals/results")


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESOLVE_OTEL_", extra="ignore")

    enabled: bool = False
    service_name: str = "resolve"
    endpoint: str = "http://localhost:4318/v1/traces"
    metrics_port: int = 9464


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESOLVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_json: bool = True

    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    evals: EvaluationSettings = Field(default_factory=EvaluationSettings)
    otel: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    def assert_production_safe(self) -> None:
        """Refuse to boot in production with development defaults."""
        if not self.is_production:
            return
        problems: list[str] = []
        if "dev-local-key" in self.security.api_keys:
            problems.append("RESOLVE_SECURITY_API_KEYS still contains the development key")
        if self.security.hmac_secret.startswith("dev-local"):
            problems.append("RESOLVE_SECURITY_HMAC_SECRET is still the development secret")
        if self.llm.provider == "mock":
            problems.append("RESOLVE_LLM_PROVIDER is 'mock' in production")
        if problems:
            raise RuntimeError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
