<div align="center">

# Resolve

**An autonomous order-operations agent for e-commerce.**

It reads inbound customer email, grounds itself in live order data and written policy,
and either resolves the request end to end or escalates it with a drafted reply —
under hard budget, safety and human-approval constraints.

[![CI](https://img.shields.io/badge/CI-lint%20·%20types%20·%20tests%20·%20eval%20gate-2ea043)](.github/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-169%20passing-2ea043)](tests/)
[![Runs offline](https://img.shields.io/badge/API%20keys%20required-none-4c9aff)](#running-it)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

## The problem

A mid-size online retailer receives thousands of customer emails a month. "Where is my
order." "I was charged twice." "My payment failed." "Change the delivery address." Each
one costs a person four to eight minutes: open the ticket, look the order up, check the
tracking, check the policy, write a reply.

Most of them are answerable entirely from order data plus written policy. A minority are
not, and those must be escalated cleanly rather than guessed at — because the cost of a
wrong automated answer is not a slightly worse reply, it is a refund issued twice, a
delivery date invented, or a customer told their parcel is safe when it is lost.

**Resolve automates the first group and refuses to touch the second.**

## What it does

<div align="center">
<img src="docs/screenshots/04-agent-trace.png" alt="Agent trace for a duplicate-charge ticket" width="880">
<br><em>Every decision the agent made, what it cost, and the policy it cited.</em>
</div>

For each inbound message it runs a five-stage loop, all of it observable:

| Stage | What happens |
|---|---|
| **Triage** | A cheap model classifies intent, urgency and confidence, and extracts the order reference. PII is stripped before anything reaches a provider. |
| **Retrieve** | Hybrid search (dense embeddings + BM25) over the policy corpus finds the passages that govern the answer. |
| **Plan** | A stronger model proposes a sequence of typed tool calls, or declares that a human is needed. |
| **Act** | Every proposed call passes the policy engine first. Read-only calls run. Anything that moves money or changes fulfilment is queued for a human. |
| **Draft → Verify** | A reply is drafted from retrieved facts only, then a second pass checks every claim against those facts. A reply that fails verification is never sent. |

## Results

Reproduce every number below with `make eval` and `make demo`. Nothing here is asserted
that the repository cannot re-derive.

### Evaluation — 30-case golden set

| Metric | Result |
|---|---|
| Cases passed | **30 / 30** |
| Intent accuracy · macro-F1 | **100%** · **1.000** |
| Outcome accuracy | **100%** |
| Escalation precision · recall | **100%** · **100%** |
| Tool precision · recall | 90.9% · 100% |
| Groundedness | **100%** |
| Replies carrying a policy citation | **100%** |
| Adversarial messages contained | **5 / 5** |
| Mean cost per ticket | $0.0175 |
| Latency p50 · p95 | 2 ms · 3 ms |

> **Read these honestly.** They were produced by the bundled **deterministic provider**,
> not by a frontier model. What they measure is the pipeline, the safety layer, the
> retrieval quality and the golden set — and a rule-based classifier scoring 100% on a
> set its rules were tuned against is a weak signal on its own. What is *not* weak is
> that the harness exists, runs in CI, and blocks merges. Point it at a real model with
> `make eval PROVIDER=anthropic` and the same numbers are produced the same way.

### Behaviour on the 24-message synthetic inbox

| Outcome | Share | Meaning |
|---|---:|---|
| Auto-resolved | 50% | Handled end to end, reply grounded and cited |
| Awaiting approval | 25% | Correct action identified, human must authorise |
| Escalated | 25% | Correctly refused: complaints, unclassifiable requests, attacks |

Half the inbox handled with no human, and — the number that actually matters — **zero
incorrect autonomous actions**, including on five adversarial messages.

## Why the escalation rate is a feature

Over-automation is the expensive failure mode. An unnecessary escalation costs a few
minutes. An incorrect auto-resolution reaches a customer, and in this domain it can
move money. So the system escalates when:

- classification confidence is below the threshold;
- the drafted reply cites no supporting policy;
- verification finds a claim the retrieved facts do not support;
- the per-ticket budget would be exceeded — it stops rather than reasoning with less;
- the message shows signs of prompt injection;
- published service policy says a person must handle it, as with complaints.

## Human authorisation

<div align="center">
<img src="docs/screenshots/05-human-rejection.png" alt="A rejected action: not executed, reply voided, ticket escalated" width="880">
<br><em>A human rejected the proposed address change. The action did not run, the drafted
reply was voided rather than sent, and the trace records who decided and why.</em>
</div>

Resolve separates five concerns that are usually collapsed into one:

| Concern | Question |
|---|---|
| **AI decision-making** | What should be done? |
| **Policy enforcement** | May the agent do it alone? |
| **Human authorisation** | Does a person permit it? |
| **Execution** | Did it actually happen? |
| **Auditability** | Who decided, when, and why? |

A decision on a privileged action produces a durable chain:

```
APPROVAL REQUESTED
  → HUMAN DECISION: REJECTED     attributed, timestamped, with a recorded reason
  → ACTION NOT EXECUTED          an explicit outcome, not an absence
  → PROVISIONAL DRAFT VOIDED     kept for audit, never shown as an outcome
  → TICKET ESCALATED             with a reason that names the rejection
```

Three distinctions make that possible, and each is a column rather than a
convention:

- **Decision ≠ outcome.** `decision` is what a person intended; `outcome` is
  what happened. An approved refund the commerce backend then refuses is
  `approved` / `execution_failed` — a state neither field expresses alone.
- **A draft is not a sent message.** `ReplyState` tracks `provisional`,
  `awaiting_approval`, `sent`, `voided`, `held`. A reply describing a refund a
  human refused is voided, so it can never be presented as the outcome.
- **Every trace event is attributed.** Human decisions are appended to the
  agent's own timeline, tagged `human`, so one ordered story shows where the
  machine stopped and a person took over.

Deciding twice is always a 409. That guard is what stops a double-click issuing
two refunds.

## Human oversight, measured separately

<div align="center">
<img src="docs/screenshots/06-activity.png" alt="The human-oversight audit log" width="880">
</div>

`/v1/oversight` reports approval rate, override rate, approval rate by action
type and financial exposure — counted from real decisions on real tickets.
`/v1/audit` is the append-only record of who authorised what.

This is deliberately **not** merged with the evaluation report. Evaluation asks
whether the AI behaved correctly against a fixed golden set; oversight asks what
operators actually did about its proposals. A system can score 30/30 and still
have most of its proposals refused — and that is the number worth watching
before widening autonomy.

## Safety

<div align="center">
<img src="docs/screenshots/08-injection-blocked.png" alt="A prompt-injection attempt stopped before any tool ran" width="880">
<br><em>An instruction-override attack: detected, escalated, zero tool calls, no money moved.</em>
</div>

Three independent layers, in order of how much work they do:

1. **Structural.** Customer text — subject *and* body — is fenced and the system prompt
   names the fence as data, never instructions. This is the load-bearing defence.
2. **Capability.** The policy engine gates on *what a tool can do*, never on how
   confident the model is. A £480 refund waits for a human at 0.51 confidence and at
   0.99 confidence alike. An attack that cannot reach a consequential capability is an
   annoyance rather than an incident.
3. **Detection.** Known injection phrasings raise the risk tier. Detection alone is not
   a defence; it is evidence, and it is treated as such.

Plus: PII redacted before any provider call and restored only locally; outbound webhooks
HMAC-signed and restricted by an egress allowlist that blocks private and link-local
addresses; every API key compared in constant time.

## Architecture

```mermaid
flowchart LR
    subgraph ingress[" "]
        E[Inbound email<br/>or webhook] --> API[FastAPI ingress<br/>idempotent]
    end

    API -->|tickets.received| BUS[(Redis Streams<br/>consumer groups)]
    BUS --> W[Agent worker<br/>lease · ack · reclaim]

    W --> WF[[Durable workflow engine<br/>checkpointed steps]]

    subgraph agent["Agent loop"]
        direction TB
        T[Triage] --> R[Retrieve] --> P[Plan] --> A[Act] --> D[Draft] --> V[Verify]
    end

    WF --> agent
    R -.-> VDB[(Postgres + pgvector<br/>hybrid search)]
    A -.->|policy engine| TOOLS[Typed tool registry<br/>order · payments · shipping]

    A -->|risk: approval| Q[Approval queue]
    Q --> HUMAN([Ops console])
    V -->|grounded| SEND[Send reply]
    V -->|not grounded| HUMAN

    WF --> DB[(Postgres<br/>tickets · runs · steps<br/>outbox · audit)]
    DB --> OUT[Outbox dispatcher<br/>HMAC · allowlist · retry]

    style agent fill:#0d1117,stroke:#4c9aff
    style HUMAN fill:#1a1f28,stroke:#f5b544
```

Full component detail, sequence diagrams and failure-mode analysis:
**[SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md)**.

## Running it

The whole system runs from a clean checkout with **no third-party API keys**. The default
model provider is deterministic and local, and so is the embedder. This is a deliberate
engineering decision, not a limitation — see
[ADR-002](TECHNICAL_DECISIONS.md#adr-002-a-deterministic-provider-is-the-default).

```bash
git clone <this-repo> && cd resolve
make install
```

```bash
make demo
```

Runs the agent over the 24-message synthetic inbox and prints a per-ticket outcome table.

```bash
make ticket N=8
```

Runs one ticket and prints the complete agent trace: every step, its cost, its latency,
the policy it retrieved, the reply it drafted and how verification judged it.

```bash
make eval
```

Runs the golden set and writes a report to `evals/results/`. With the stack up
this executes **inside the API container**, and the console reads the result
back through `GET /v1/evals/latest` — see
[Evaluation as a build gate](#evaluation-as-a-build-gate). Use `make eval-local`
to run it on the host instead.

```bash
make test
```

169 tests. No network, no database server, no API key.

### The full stack

```bash
make up      # Postgres+pgvector, Redis, API, 2 agent workers, outbox dispatcher,
             # console, OpenTelemetry collector, Prometheus
make seed    # load the 24-message synthetic inbox
make eval    # run the golden set and publish the report to the console
```

The console is at `http://localhost:3000` and the OpenAPI docs at
`http://localhost:8000/docs`.

`make seed` and `make eval` run **inside the API container**, not on the host.
That container owns the database, the seeded data and the evaluation results,
and a command that runs anywhere else operates on a different world than the one
serving traffic. Host equivalents (`make seed-local`, `make eval-local`,
`make eval-gate-local`) exist for CI, which has Python but no running stack.

### Using a real model

```bash
pip install 'resolve[anthropic]'
export RESOLVE_LLM_PROVIDER=anthropic
export RESOLVE_LLM_API_KEY=sk-ant-...
make eval
```

Nothing else changes. The provider sits behind a protocol; the agent, the workflow
engine and the evaluation harness do not know which one is in use.

## The ops console

<div align="center">
<img src="docs/screenshots/01-overview.png" alt="Console overview" width="880">
</div>

Five screens: **Overview** (outcomes, cost, human intervention), **Tickets** (a filterable
queue), **ticket detail** (the full execution trace), **Approvals** (the human control
surface) and **Activity** (the oversight audit log), plus **Evaluation**.

Built with Next.js Server Components — every mutation is a Server Action, so the browser
can *ask* for an action but can never perform one. The API key never reaches the client.
Consequential actions require a confirmation step, and the form disables while in flight,
so a double submission cannot be attempted from the UI even before the API's 409 catches
it.

<div align="center">
<img src="docs/screenshots/03-approvals.png" alt="Approval queue" width="880">
<br><em>Actions the agent judged correct but is not permitted to take alone.</em>
</div>

## Evaluation as a build gate

<div align="center">
<img src="docs/screenshots/07-evaluation.png" alt="Evaluation dashboard" width="880">
</div>

The console reads the report from the API — `GET /v1/evals/latest` — rather than
from disk. That endpoint returns the newest report the harness produced, a clean
404 when no evaluation has been run, and a 500 if a report exists but fails
schema validation. The three states are rendered distinctly, so "nobody has run
it yet" can never be confused with "something is broken", and no metric is ever
defaulted or inferred.

`evals/golden/support_tickets.yaml` is a versioned set of 30 cases stating what the agent
*should* do — expected intent, expected tool calls, expected outcome, text that must
appear in the reply, text that must not, and five adversarial cases that must never reach
a consequential action.

CI runs `python -m evals.runner --gate` on every pull request and fails the build if any
metric falls below `evals/thresholds.yaml`. Adversarial containment has **no tolerance**:
one containment failure is a security defect, not a quality regression.

This is the part most LLM projects skip, and it is the difference between a system that
is measured and a system that is vibed.

## Project layout

```
src/resolve/
  agent/          the loop, the typed tool registry, the policy engine
  llm/            provider abstraction, routing, cache, cost ledger, prompt registry
  rag/            chunking, embeddings, hybrid retrieval, pgvector store
  workflow/       durable engine — checkpointed, resumable, retrying
  bus/            event bus — Redis Streams and an in-memory twin
  safety/         PII redaction, injection defence, egress allowlist
  services/       commerce backend protocol + a simulator that enforces real rules
  api/            FastAPI: ingest, traces, approvals, oversight, audit, evals, metrics
  oversight.py    human authorisation, execution and the audit chain
  evaluation_store.py  reads and validates reports for the API to serve
  workers/        agent worker, outbox dispatcher
  seed/           the synthetic world: orders, payments, policies, tickets
evals/            golden set, runner, metrics, CI gate, published results
console/          Next.js 15 operations console
tests/            169 tests
```

## Honesty notes

**Built for this portfolio**, not extracted from commercial work. The domain —
failed payments, refunds, order operations — is one I worked in, which is why
the policy rules and the ticket set read the way they do, but the system itself
is new work.

The portfolio this belongs to has a rule against overstating things, so:

- **Every order, customer, policy and ticket is synthetic**, generated by
  `src/resolve/seed/generate.py` from a fixed seed. There is no real customer data here.
- **The evaluation figures above come from the deterministic provider.** They measure the
  machinery, not model quality. The report file records which provider produced it.
- **The "hours saved" figure in the console is a model, not a measurement.** It applies
  published assumptions to observed outcomes, and the console shows the assumptions
  beside the number so it can be argued with.
- **This system has not run in production.** It is engineered as though it would, and the
  parts that make that claim credible — durable state, leases, idempotency, dead-letter
  handling, an eval gate, an audit trail — are implemented and tested, not described.

## Documentation

| | |
|---|---|
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Components, data flow, sequence diagrams, data model, failure modes, scaling limits |
| [CASE_STUDY.md](CASE_STUDY.md) | The problem, the constraints, what was built, what it cost, what I would change |
| [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md) | Sixteen ADRs: context, options weighed, decision, consequences |
| [ROADMAP.md](ROADMAP.md) | Shipped, next, later — and what is deliberately out of scope |

---

<div align="center">
<sub>Built by <strong>Calum Caulfield</strong> · MIT licensed</sub>
</div>
