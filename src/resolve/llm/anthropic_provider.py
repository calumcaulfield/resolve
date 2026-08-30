"""Anthropic provider.

Notes that cost real debugging time and are therefore written down:

* Current Claude models (Opus 5, Sonnet 5, and the 4.6+ family) **reject**
  `temperature`, `top_p` and `top_k` — sending them returns a 400. Determinism
  is achieved with schema-constrained output, not with `temperature=0`.
* `budget_tokens` is likewise removed; thinking is configured with
  `{"type": "adaptive"}` and depth is controlled by `output_config.effort`.
* Structured output uses `client.messages.parse(output_format=Model)`, which
  validates the response against a Pydantic model server-side and returns
  `parsed_output`. That removes the fence-stripping / `json.loads` dance the
  original Proflow implementation needed.

Install with the optional extra:  ``pip install 'resolve[anthropic]'``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from resolve.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    StructuredOutputError,
    Usage,
)
from resolve.llm.pricing import cost_usd

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import AsyncAnthropic

TModel = TypeVar("TModel", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        max_retries: int = 3,
        base_url: str | None = None,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError(
                "The 'anthropic' package is not installed. "
                "Install it with: pip install 'resolve[anthropic]'"
            ) from exc

        kwargs: dict[str, Any] = {"timeout": timeout, "max_retries": max_retries}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # With no api_key the SDK resolves ANTHROPIC_API_KEY or an `ant auth
        # login` profile from the environment.
        self._client: AsyncAnthropic = AsyncAnthropic(**kwargs)
        self.default_model = default_model

    def _model(self, request: LLMRequest) -> str:
        return request.model or self.default_model

    def _usage(self, model: str, raw_usage: Any) -> Usage:
        tin = int(getattr(raw_usage, "input_tokens", 0) or 0)
        tout = int(getattr(raw_usage, "output_tokens", 0) or 0)
        cached = int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0)
        return Usage(
            model=model,
            input_tokens=tin,
            output_tokens=tout,
            cached_input_tokens=cached,
            cost_usd=cost_usd(model, tin, tout),
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = self._model(request)
        response = await self._client.messages.create(
            model=model,
            max_tokens=request.max_output_tokens,
            system=request.system,
            messages=[{"role": m.role.value, "content": m.content} for m in request.messages],
            thinking={"type": "adaptive"},
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        return LLMResponse(text=text, usage=self._usage(model, response.usage))

    async def complete_structured(
        self, request: LLMRequest, schema: type[TModel]
    ) -> tuple[TModel, Usage]:
        model = self._model(request)
        response = await self._client.messages.parse(
            model=model,
            max_tokens=request.max_output_tokens,
            system=request.system,
            messages=[{"role": m.role.value, "content": m.content} for m in request.messages],
            output_format=schema,
            thinking={"type": "adaptive"},
        )
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise StructuredOutputError(
                f"model returned no parseable output for task {request.task!r}"
            )
        return parsed, self._usage(model, response.usage)
