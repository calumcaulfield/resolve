# Roadmap

## Shipped

**Agent** — five-stage loop (triage → retrieve → plan → act → draft → verify) with typed
structured outputs at every stage, schema-repair retry, step limits and a hard per-ticket
budget that escalates rather than degrading.

**Retrieval** — heading-aware chunking, hybrid dense + BM25 search fused with Reciprocal
Rank Fusion, pgvector in production and an in-memory twin for tests, enforced citations.

**Human authorisation** — a decision lifecycle with durable audit: decision and outcome
modelled separately, a reply lifecycle that distinguishes a draft from a sent message,
human events attributed inside the agent's own trace, ticket state reconciled across all
approvals, and idempotent decisions. Oversight metrics (approval rate, override rate, by
action type, financial exposure) served separately from evaluation.

**Safety** — capability-based policy gating independent of model confidence; three-layer
prompt-injection defence; PII redaction before any provider call with local restoration;
egress allowlist blocking private and link-local addresses; HMAC-signed webhooks with a
replay window; constant-time key comparison.

**Durability** — Postgres-backed workflow engine with per-step checkpointing, exactly-once
step semantics, bounded retries with jittered backoff and a dead-letter state; Redis
Streams consumer groups with lease, acknowledge and automatic reclaim; idempotency at
three levels; transactional outbox.

**Evaluation** — 30-case golden set, intent/outcome/tool/escalation/groundedness metrics,
adversarial containment with zero tolerance, and a CI gate that fails the build on
regression.

**Operations** — Next.js console (outcomes, tickets with full agent traces, approval
queue, evaluation dashboard served from `GET /v1/evals/latest`); OpenTelemetry traces;
Prometheus metrics; structured JSON logs with correlation ids; `docker compose up` for
the entire stack, with `make seed` and `make eval` executing inside it; 169 tests.

---

## Next

### Retrieval evaluation, separated from end-to-end
When an end-to-end case fails today, it is not immediately clear whether retrieval missed
the passage or drafting ignored it. A recall@k set over the policy corpus, gated
separately, would isolate that. *Small, and the highest information-per-hour item here.*

### A third-party-enquiry intent
"My neighbour ordered this, where is it?" currently escalates because the classifier is
unsure, not because it recognises a data-protection boundary. Right outcome, weak
reasoning. A dedicated intent with an explicit policy would make it deliberate.

### Multi-tenancy
One organisation today. Tenant-scoped rows, per-tenant policy corpora, per-tenant
thresholds and budgets, and row-level security. Nothing in the design resists it — the
work is real but mechanical.

### Reply delivery
The system drafts; a human or the helpdesk sends. Closing the loop means an outbound
channel, threading, and a send-time approval for the first N replies of any new intent.

### Cost controls above the ticket
The per-ticket ceiling stops one runaway ticket, not a thousand. Needed: a global daily
cap with graceful degradation to human handling, and a semantic cache shared across
tickets rather than per-process.

---

## Later

### Temporal
When the workflow count passes roughly ten, or throughput passes roughly a thousand runs
per minute, the custom engine's missing features — timers, signals, child workflows,
visibility — stop being acceptable. `ctx.step(name, fn)` is deliberately Temporal-shaped
so the migration is mechanical. *Adopting it now would be over-engineering; ADR-003
explains why.*

### Learning from approvals
Every approve/reject is a labelled example of a decision a human thought the agent should
not have made alone. Feeding those back — first as retrieved few-shot context, later as
threshold tuning — is the obvious next capability. It is *later* rather than *next*
because it needs enough volume to avoid overfitting to a handful of decisions.

### ANN indexing
Exact cosine degrades past roughly 100k chunks. An HNSW index sits behind the identical
interface. Not needed at 25 chunks; the interface exists so it will not be a rewrite.

### Voice and chat channels
The agent is channel-agnostic; ingest is not. Chat adds a real constraint the current
design does not handle: latency budgets measured in seconds, which changes the model
routing calculus.

---

## Deliberately out of scope

**Fine-tuning.** The bottleneck is grounding and authority, not model capability. Nothing
observed so far would be fixed by a fine-tune, and it would add a training pipeline, a
data-collection process and a versioning problem in exchange for that.

**Autonomous refunds above the threshold.** Not a technical limit — a deliberate product
decision. Widening autonomy should be an explicit configuration change a person makes,
visible in `.env`, not a behaviour that drifts (ADR-007).

**A conversational interface for customers.** Resolve answers a message; it does not hold
a conversation. Multi-turn adds state, context management and a much larger injection
surface, for a problem that is largely single-turn.

**Replacing the helpdesk.** Resolve is a component. Owning the mailbox would double the
blast radius for no benefit.
