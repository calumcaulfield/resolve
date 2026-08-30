"""OpenAI provider.

Present so the provider abstraction is demonstrably an abstraction and not a
single-vendor wrapper with extra steps. Optional: ``pip install 'resolve[openai]'``
"""

from __future__ import annotations

import json
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
    from openai import AsyncOpenAI

TModel = TypeVar("TModel", bound=BaseModel)

DEFAULT_MODEL = "gpt-4o"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        max_retries: int = 3,
        base_url: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError(
                "The 'openai' package is not installed. "
                "Install it with: pip install 'resolve[openai]'"
            ) from exc
        kwargs: dict[str, Any] = {"timeout": timeout, "max_retries": max_retries}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client: AsyncOpenAI = AsyncOpenAI(**kwargs)
        self.default_model = default_model

    def _usage(self, model: str, raw: Any) -> Usage:
        tin = int(getattr(raw, "prompt_tokens", 0) or 0)
        tout = int(getattr(raw, "completion_tokens", 0) or 0)
        return Usage(
            model=model,
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=cost_usd(model, tin, tout),
        )

    def _messages(self, request: LLMRequest) -> list[dict[str, str]]:
        msgs = [{"role": "system", "content": request.system}]
        msgs.extend({"role": m.role.value, "content": m.content} for m in request.messages)
        return msgs

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model
        completion = await self._client.chat.completions.create(
            model=model,
            max_tokens=request.max_output_tokens,
            messages=self._messages(request),
        )
        return LLMResponse(
            text=completion.choices[0].message.content or "",
            usage=self._usage(model, completion.usage),
        )

    async def complete_structured(
        self, request: LLMRequest, schema: type[TModel]
    ) -> tuple[TModel, Usage]:
        model = request.model or self.default_model
        completion = await self._client.chat.completions.create(
            model=model,
            max_tokens=request.max_output_tokens,
            messages=self._messages(request),
            response_format={"type": "json_object"},
        )
        text = (completion.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            value = schema.model_validate(json.loads(text))
        except Exception as exc:
            raise StructuredOutputError(str(exc), payload=text) from exc
        return value, self._usage(model, completion.usage)
