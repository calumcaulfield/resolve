"""Command line interface.

resolve demo               run the whole pipeline over the synthetic inbox
resolve ticket <n>         run one ticket and print the full agent trace
resolve retrieve "<q>"     inspect what hybrid retrieval returns
resolve policies           list the indexed policy corpus
resolve config             show the resolved configuration
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from resolve.bootstrap import build_application
from resolve.config import get_settings
from resolve.domain.enums import TicketStatus
from resolve.domain.schemas import Ticket
from resolve.logging import configure_logging

app = typer.Typer(add_completion=False, help="Resolve — autonomous order operations agent")
console = Console()

_STATUS_STYLE = {
    TicketStatus.RESOLVED: "bold green",
    TicketStatus.AWAITING_APPROVAL: "bold yellow",
    TicketStatus.ESCALATED: "bold magenta",
    TicketStatus.FAILED: "bold red",
}


def _ticket_from_message(message) -> Ticket:  # type: ignore[no-untyped-def]
    return Ticket(
        external_id=message.external_id,
        channel=message.channel,
        from_email=message.from_email,
        subject=message.subject,
        body=message.body,
    )


@app.command()
def demo(
    limit: Annotated[int, typer.Option(help="How many tickets to process")] = 0,
    verbose: Annotated[bool, typer.Option(help="Print each agent step")] = False,
) -> None:
    """Run the agent over the whole synthetic inbox and summarise the outcome."""
    configure_logging(level="WARNING", json_output=False)
    asyncio.run(_demo(limit, verbose))


async def _demo(limit: int, verbose: bool) -> None:
    application = await build_application()
    messages = application.dataset.messages
    if limit:
        messages = messages[:limit]

    console.print(
        Panel(
            f"provider=[cyan]{application.settings.llm.provider}[/]  "
            f"orders=[cyan]{len(application.dataset.orders)}[/]  "
            f"policy chunks=[cyan]{application.policy_chunks}[/]  "
            f"tools=[cyan]{len(application.registry)}[/]",
            title="Resolve",
            border_style="cyan",
        )
    )

    table = Table(show_lines=False, header_style="bold")
    for column in ("Ticket", "Subject", "Intent", "Outcome", "Steps", "Cost", "ms"):
        table.add_column(column)

    counts: dict[TicketStatus, int] = {}
    total_cost = 0.0
    total_ms = 0.0

    for message in messages:
        outcome = await application.agent.run(_ticket_from_message(message))
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        total_cost += outcome.total_cost_usd
        total_ms += outcome.duration_ms

        table.add_row(
            message.external_id,
            message.subject[:34],
            outcome.intent.value if outcome.intent else "—",
            f"[{_STATUS_STYLE.get(outcome.status, 'white')}]{outcome.status.value}[/]",
            str(len(outcome.steps)),
            f"${outcome.total_cost_usd:.5f}",
            f"{outcome.duration_ms:.0f}",
        )

        if verbose:
            for step in outcome.steps:
                console.print(f"    [dim]{step.index}. {step.kind}[/] {step.summary}")
            if outcome.escalation_reason:
                console.print(f"    [magenta]→ {outcome.escalation_reason}[/]")

    console.print(table)

    total = len(messages)
    resolved = counts.get(TicketStatus.RESOLVED, 0)
    awaiting = counts.get(TicketStatus.AWAITING_APPROVAL, 0)
    escalated = counts.get(TicketStatus.ESCALATED, 0)
    console.print(
        Panel(
            f"tickets=[cyan]{total}[/]  "
            f"auto-resolved=[green]{resolved}[/] ({resolved / total:.0%})  "
            f"awaiting approval=[yellow]{awaiting}[/]  "
            f"escalated=[magenta]{escalated}[/]\n"
            f"total cost=[cyan]${total_cost:.5f}[/]  "
            f"mean=[cyan]${total_cost / total:.6f}[/]/ticket  "
            f"mean latency=[cyan]{total_ms / total:.0f}ms[/]",
            title="Summary",
            border_style="green",
        )
    )


@app.command()
def ticket(
    index: Annotated[int, typer.Argument(help="Index into the synthetic inbox")] = 0,
    show_reply: Annotated[bool, typer.Option(help="Print the drafted reply")] = True,
) -> None:
    """Run one ticket and print the complete agent trace."""
    configure_logging(level="WARNING", json_output=False)
    asyncio.run(_ticket(index, show_reply))


async def _ticket(index: int, show_reply: bool) -> None:
    application = await build_application()
    message = application.dataset.messages[index]
    console.print(
        Panel(
            f"[bold]{message.subject}[/]\nfrom {message.from_email}\n\n{message.body}",
            title=f"Inbound {message.external_id}",
            border_style="blue",
        )
    )

    outcome = await application.agent.run(_ticket_from_message(message))

    for step in outcome.steps:
        console.print(
            f"[dim]{step.index:>2}[/] [bold cyan]{step.kind:<9}[/] {step.summary}"
            + (f"  [dim]${step.cost_usd:.5f}[/]" if step.cost_usd else "")
        )

    style = _STATUS_STYLE.get(outcome.status, "white")
    console.print(
        Panel(
            f"status=[{style}]{outcome.status.value}[/]  "
            f"cost=[cyan]${outcome.total_cost_usd:.5f}[/]  "
            f"tokens=[cyan]{outcome.total_tokens_in}/{outcome.total_tokens_out}[/]  "
            f"latency=[cyan]{outcome.duration_ms:.0f}ms[/]"
            + (f"\n[magenta]{outcome.escalation_reason}[/]" if outcome.escalation_reason else ""),
            title="Outcome",
            border_style=style.split()[-1],
        )
    )

    if show_reply and outcome.reply:
        citations = "\n".join(f"  · {c.title} ({c.chunk_id})" for c in outcome.reply.citations)
        console.print(
            Panel(
                f"[bold]{outcome.reply.subject}[/]\n\n{outcome.reply.body}"
                + (f"\n\n[dim]Citations:\n{citations}[/]" if citations else ""),
                title="Drafted reply",
                border_style="green",
            )
        )


@app.command()
def retrieve(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option()] = 5,
) -> None:
    """Inspect hybrid retrieval: dense score, lexical score and fused rank."""
    asyncio.run(_retrieve(query, limit))


async def _retrieve(query: str, limit: int) -> None:
    application = await build_application()
    results = await application.retriever.retrieve(query, limit=limit)
    table = Table(title=f"Retrieval — {query!r}", header_style="bold")
    for column in ("Rank", "Chunk", "Title", "Fused", "Dense", "BM25"):
        table.add_column(column)
    for i, r in enumerate(results, start=1):
        table.add_row(
            str(i),
            r.chunk.id,
            r.chunk.title[:44],
            f"{r.score:.4f}",
            f"{r.semantic_score:.4f}",
            f"{r.lexical_score:.4f}",
        )
    console.print(table)


@app.command()
def policies() -> None:
    """List the indexed policy corpus."""
    asyncio.run(_policies())


async def _policies() -> None:
    application = await build_application()
    table = Table(title="Policy corpus", header_style="bold")
    for column in ("Document", "Title", "Category", "Chars"):
        table.add_column(column)
    for doc in application.dataset.policies:
        table.add_row(doc.id, doc.title, doc.category, str(len(doc.body)))
    console.print(table)
    console.print(f"Indexed chunks: [cyan]{application.policy_chunks}[/]")


@app.command()
def config() -> None:
    """Print the resolved configuration (secrets masked)."""
    settings = get_settings()
    payload = settings.model_dump(mode="json")
    for key in ("api_key",):
        if payload.get("llm", {}).get(key):
            payload["llm"][key] = "***"
    payload["security"]["api_keys"] = ["***"] * len(payload["security"]["api_keys"])
    payload["security"]["hmac_secret"] = "***"  # noqa: S105 - masking, not a secret
    console.print_json(json.dumps(payload))


if __name__ == "__main__":
    app()
