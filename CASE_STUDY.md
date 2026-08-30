# Case Study — Resolve

> **Scope note, stated up front.** Resolve is new work, designed and built for this
> portfolio. It has not run in production and no real customer data is involved. What is
> real is the problem: it comes directly from operational work I did automating
> e-commerce order and payment recovery, and the design decisions are answers to
> failures I actually hit there. Where a claim is a projection rather than a measurement,
> it is labelled as one.

---

## 1. The problem

An online gift and hamper retailer handles thousands of customer emails a month. The
distribution is lopsided and predictable:

| Category | Roughly | Handling |
|---|---:|---|
| "Where is my order" | 35% | Look up order, read tracking, reply |
| Delivery problems | 20% | Look up order, check dispatch date against policy, reply |
| Payment failures and duplicate charges | 15% | Read the payment ledger, sometimes move money |
| Refunds and cancellations | 15% | Judgement plus a policy check |
| Address changes | 5% | Check dispatch status, amend or refuse |
| Product and allergen questions | 5% | Read the product policy |
| Everything else | 5% | A person |

Each ticket costs four to eight minutes. The work is repetitive but not trivial: getting
it wrong means an invented delivery date, a refund issued twice, or a customer told their
parcel is fine when it is lost.

The first three categories — around 70% of volume — are answerable entirely from order
data plus written policy. That is the opportunity. The last category is the constraint,
and it is the more interesting half of the design.

## 2. Why the naive version fails

The obvious build is a chatbot with the policy documents in context. It fails on the
majority of tickets for a structural reason: **the answer is not in the documents.**
"Where is my order" is answered by the order record. "I was charged twice" is answered by
the payment ledger. A system that can only read documents always ends at "a colleague
will be in touch", which is the outcome it was meant to remove.

The second naive build gives a model tools and lets it act. That fails differently and
worse. Three failure modes, all of which I designed against explicitly:

1. **Confident invention.** A model with no order data will happily produce a delivery
   date. A fluent wrong answer is more damaging than no answer, because nobody catches it.
2. **Unbounded authority.** A model that can call `issue_refund` will call it. Someone
   who works out that it can will make it.
3. **Silent degradation.** Quality drifts with a prompt change, a model change, or
   nothing at all, and without measurement the first signal is a customer complaint.

Resolve is essentially three answers to those three failures: grounding with enforced
citations, capability gating independent of confidence, and evaluation as a build gate.

## 3. What I built

A five-stage agent — triage, retrieve, plan, act, draft, verify — running inside a durable
workflow, behind an idempotent API, with a human approval queue and a full audit trail.

The interesting choices are documented as ADRs in
[TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md); the three that shaped everything else:

**Capability gating, not confidence gating (ADR-007).** Whether a human is required is a
property of the tool, never of the model's self-report. A £480 refund waits for a person
at 0.99 confidence exactly as it does at 0.51. This matters most in the case it is
easiest to overlook: a successful prompt injection produces a *very* confident model.

**Citations are structural (ADR-005).** A reply that cites no retrieved policy is not
sent — regardless of how good it reads. Prompting for grounding is necessary and
insufficient; a hallucinated tracking number looks exactly like a real one.

**Evaluation is a build gate (ADR-010).** Thirty golden cases with expected intents,
expected tool calls, expected outcomes and five adversarial messages. CI fails the build
on regression. Adversarial containment has zero tolerance.

## 4. Results

All reproducible: `make eval` and `make demo`.

### On the 24-message synthetic inbox

| Outcome | Count | Share |
|---|---:|---:|
| Auto-resolved | 12 | 50% |
| Awaiting human approval | 6 | 25% |
| Escalated | 6 | 25% |

**Zero incorrect autonomous actions**, including on three prompt-injection attempts.

### On the 30-case golden set

| Metric | Result |
|---|---|
| Cases passed | 30 / 30 |
| Intent accuracy · macro-F1 | 100% · 1.000 |
| Escalation precision · recall | 100% · 100% |
| Groundedness | 100% |
| Adversarial contained | 5 / 5 |
| Mean cost per ticket | $0.0175 |

### How to read those numbers

They were produced by the **deterministic local provider**, not a frontier model. They
measure the pipeline, the retrieval quality, the safety layer and the golden set. A
rule-based classifier scoring 100% against a set its rules were tuned to is a weak signal
in isolation, and I would rather say that than let a table imply otherwise.

The claim I *will* make is narrower and, I think, more useful: **the measurement
apparatus exists, runs on every commit, and blocks merges.** Swap the provider and the
same numbers are produced the same way, by the same harness, against the same cases.

### Projected business value

This is a **model, not a measurement**, and the console displays the assumptions beside
the figure so it can be argued with.

| Input | Value | Source |
|---|---|---|
| Manual handling time | 6 min/ticket | Stated assumption |
| Reviewing a drafted reply | 1.5 min/ticket | Stated assumption |
| Auto-resolution rate | 50% | Measured on the synthetic inbox |
| Approval rate | 25% | Measured on the synthetic inbox |

At 2,000 tickets/month: 1,000 fully automated (100 hours) plus 500 reviewed rather than
written (37.5 hours) ≈ **137 hours/month**, against a model spend under $40/month at the
measured per-ticket cost.

The number that would decide a real deployment is not the hours. It is the incorrect
auto-resolution rate, which is why escalation precision, groundedness and containment are
the gated metrics and throughput is not.

## 5. What I learned

**Writing the expectations down changed the system, twice.** One golden case originally
expected the agent to answer a third-party enquiry ("my neighbour ordered this, where is
it?"). Writing the expectation made it obvious that the data-protection policy forbids
it and escalation was correct. The expectation was wrong, not the code. That is the
value of a golden set that nobody has yet: it makes you state what *should* happen, which
is a different and harder question than what does.

**Two bugs came from looking at a screenshot, not from a test.** The approval queue
rendered `"new_address": ""` — the planner was proposing an address change with no
address. Fixing that surfaced a second, worse one: the extractor was reading the *order
context* section of the prompt and pulling out the address already on the order, so the
agent would have "changed" the address to itself. And a third: redaction placeholders
were leaking into tool arguments, so a human approving a queued action would have
executed `42 Willow Grove, Belfast, [POSTCODE_1]`. All three are now regression tests.
The lesson is old but it keeps being true: build the surface that shows you the data, and
look at it.

**A status field is not a lifecycle.** Manual testing found that rejecting a
proposed action changed the ticket's status and nothing else: the trace had no
human event, the escalation reason still claimed the ticket was awaiting
approval, and the drafted reply — which said the refund had been processed —
was still shown as the outcome. The fix was not a rendering change. A rejection
has consequences for the approval, the run, the reply, the ticket and the audit
trail, and modelling only one of them meant the UI was faithfully displaying an
incomplete domain. Building it properly also surfaced a second bug: ticket state
was recomputed from the decision just made rather than from all approvals on the
ticket, so approving one action and rejecting another could leave a ticket
`resolved` carrying an action a human had refused.

**An idle queue is not an error.** Running the full stack under Docker — two
workers competing over Redis — surfaced a defect no unit test would catch. The
worker's blocking read on the stream uses a five-second `BLOCK` window, and the
Redis client's socket deadline expired at the same moment, so every idle cycle
raised `TimeoutError`, was caught by the worker's generic handler, and logged as
`worker.cycle_failed`. A drained, perfectly healthy worker produced an error
line every six seconds forever. Nothing broke — which is exactly the problem:
the noise would bury a real failure and trip any error-rate alert. A blocking
read that finds nothing is the normal state of a drained worker, so `consume`
now returns an empty list on timeout while letting genuine connection errors
propagate, and the socket deadline is given headroom over the `BLOCK` window so
the two are not racing. Two tests pin both halves.

**A shared filesystem is not an interface.** The evaluation dashboard read the
report off disk. That worked on my machine and was structurally impossible in
Docker, where the console image does not contain `evals/` at all — and it failed
by rendering "No evaluation report found", which is a *legitimate* state, so
nothing looked wrong. Fixing it properly meant serving the report from the API
like every other piece of state, defining the report shape once so the producer
and the consumer cannot drift, and making "no report yet" and "the report is
broken" render differently. The general lesson: an undeclared dependency on
another service's environment works right up until it doesn't, and it fails
quietly.

**Verification finds things testing does not.** Bringing the stack up from a
clean volume to check that fix surfaced a startup race I had never hit: the API,
two worker replicas and the dispatcher all call `create_all` simultaneously, and
Postgres failed one of them on concurrent `CREATE TABLE`. Every unit test passed
throughout, because each test owns its own SQLite file and nothing in the suite
starts four processes at once. It took an advisory lock to fix and a clean
`docker compose up` to find.

**Dead UI is a bug, not a cosmetic issue.** The ticket list had an order-reference
column that was empty on every row. The agent *was* extracting the reference
during triage — it sat in the triage step's detail blob — but the workflow never
wrote it back to the ticket, so the column, the database index on it, and any
filter built on it were all dead weight. It looked like a styling detail and it
was a missing line in the persistence step.

**A healthcheck that cannot pass is worse than no healthcheck.** The image
defines an HTTP healthcheck for the API. The worker services are built from the
same image and run queue consumers with no HTTP listener, so they reported
`unhealthy` permanently. Nothing was wrong; the check simply did not apply. A
container that is always red trains you to ignore the column where a real
failure would appear, so the workers now declare no healthcheck rather than a
false one.

**Reversible redaction has a sharp edge.** The model must see redacted text; the tool must
receive the real value; the customer must receive the real value. Three different
requirements over one string, and the boundary between them is exactly where the bug was.

**The mock provider paid for itself several times over.** 119 tests and a 30-case
evaluation suite that run in three seconds with no key and no network meant I could
refactor the agent loop repeatedly without hesitation. It also forced the provider
abstraction to be real — you cannot fake an interface with one implementation.

**Escalation rate is a design output, not a failure count.** Early on I found myself
trying to push auto-resolution up. That is the wrong objective. The right one is to
maximise auto-resolution *subject to* zero incorrect autonomous actions, and those two
targets pull in opposite directions. Making that trade explicit — via thresholds in
configuration rather than judgement in a prompt — is most of what makes the system
defensible.

## 6. What I would do differently

**Start with the golden set.** I wrote the agent, then the evaluation. Reversing that
would have caught the intent taxonomy problems on day one — `complaint` versus
`delivery_issue` versus `other` is genuinely ambiguous and I discovered it late.

**A dedicated third-party-enquiry intent.** Right now the data-protection case reaches the
correct outcome for the weaker reason of low confidence. It works, but it works by
accident, and the golden case says so rather than hiding it.

**Retrieval evaluation separately from end-to-end evaluation.** When a case fails today I
cannot immediately tell whether retrieval missed the passage or drafting ignored it. A
small recall@k set over the policy corpus would separate those.

**A cost ceiling per tenant, not only per ticket.** The per-ticket budget stops one
runaway ticket. It does not stop a thousand of them.

## 7. Relationship to my other work

This is a deliberate continuation rather than a change of subject. The
[payment-recovery orchestrator](../../v2/payment-recovery-orchestrator/) rebuilds a
system I originally built at work for recovering failed e-commerce payments; it is where
the durable-queue and idempotency thinking comes from, and its audit findings —
in-memory queues, at-most-once delivery, SSRF in webhook forwarding — are answered
one-for-one in this architecture.

Resolve is the same domain, one layer up: instead of executing a workflow a human
decided on, it decides which workflow to run and is held to account for the decision.
