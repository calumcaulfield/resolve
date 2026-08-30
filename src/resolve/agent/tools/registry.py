"""Typed tool registry.

A tool is a Pydantic argument schema, a risk tier, and an async handler. The
schema is the single source of truth: it generates the JSON schema advertised
to the model *and* validates what comes back. There is no path by which the
model's arguments reach a handler without being validated first.

The risk tier is attached to the tool, not to the call. That is deliberate:
whether a human is required is a property of the capability, and cannot be
argued out of by a confident model (see agent/policy.py).
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from resolve.domain.enums import ActionRisk
from resolve.domain.schemas import ToolResult
from resolve.services.commerce import CommerceBackend, CommerceError

TArgs = TypeVar("TArgs", bound=BaseModel)


@dataclass
class ToolContext:
    """Everything a handler is allowed to touch."""

    commerce: CommerceBackend
    ticket_id: str
    correlation_id: str = ""
    scratch: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


class ToolError(Exception):
    """A handler failure that should be reported to the agent, not crash it."""


@dataclass
class Tool(Generic[TArgs]):
    name: str
    description: str
    args_model: type[TArgs]
    risk: ActionRisk
    handler: Handler
    #: Human-readable summary used in approval requests.
    approval_summary: Callable[[TArgs], str] | None = None

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "input_schema": schema,
        }

    def describe(self) -> str:
        props = self.args_model.model_json_schema().get("properties", {})
        args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items()) or "no arguments"
        marker = " [REQUIRES HUMAN APPROVAL]" if self.risk is ActionRisk.REQUIRES_APPROVAL else ""
        return f"- {self.name}({args}){marker}: {self.description}"

    def parse_args(self, raw: dict[str, Any]) -> TArgs:
        try:
            return self.args_model.model_validate(raw)
        except ValidationError as exc:
            raise ToolError(f"invalid arguments for {self.name}: {exc.errors()}") from exc


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]] | None = None) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any] | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def catalogue(self) -> str:
        """The tool list as shown to the model."""
        return "\n".join(
            tool.describe()
            for tool in sorted(self._tools.values(), key=lambda t: t.name)
            if tool.risk is not ActionRisk.FORBIDDEN
        )

    def json_schemas(self) -> list[dict[str, Any]]:
        return [t.json_schema() for t in self._tools.values() if t.risk is not ActionRisk.FORBIDDEN]

    async def execute(
        self, name: str, raw_args: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Validate, run, and wrap the outcome. Never raises for tool failure."""
        tool = self.get(name)
        started = time.perf_counter()

        if tool is None:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"unknown tool {name!r}; available: {', '.join(self.names())}",
                duration_ms=0.0,
            )
        if tool.risk is ActionRisk.FORBIDDEN:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"tool {name!r} is disabled by policy",
                risk=tool.risk,
            )

        try:
            args = tool.parse_args(raw_args)
            result = tool.handler(args, context)
            data = await result if inspect.isawaitable(result) else result
            return ToolResult(
                tool=name,
                ok=True,
                data=dict(data),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                risk=tool.risk,
            )
        except (ToolError, CommerceError) as exc:
            # Expected, explainable failures: the agent can reason about these
            # and often should relay them to the customer.
            return ToolResult(
                tool=name,
                ok=False,
                error=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                risk=tool.risk,
            )
        except Exception as exc:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                risk=tool.risk,
            )


def json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def dumps(value: Any) -> str:
    return json.dumps(value, default=json_default, ensure_ascii=False)
