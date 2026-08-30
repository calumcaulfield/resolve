import Link from "next/link"
import { api, type EvalReport } from "@/lib/api"
import {
  Empty,
  Kbd,
  LoadError,
  Metric,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Row,
  pct,
} from "@/components/ui"

export const dynamic = "force-dynamic"

/**
 * The evaluation dashboard.
 *
 * Every number is served by `/v1/evals/latest` from a report the harness
 * produced. There are no defaults and no fallbacks: no run means the page says
 * so, and an unreadable report says that instead. A dashboard that invents a
 * plausible number is worse than one that admits it has nothing.
 *
 * Deliberately not mixed with operations. This measures the agent against a
 * fixed golden set; it says nothing about what happened to real tickets.
 */
export default async function Evals() {
  const state = await api.evals()

  return (
    <>
      <PageHeader
        eyebrow="AI engineering"
        title="Evaluation"
        subtitle="A versioned golden set states what the agent should do. CI runs it on every pull request and fails the build when accuracy, escalation precision or adversarial containment regresses — so a prompt change that quietly makes things worse cannot merge."
      />

      {state.kind === "error" && (
        <LoadError title="Could not load the evaluation report." detail={state.message} />
      )}

      {state.kind === "none" && (
        <Empty
          title="No evaluation has been run"
          message="The golden set has not been executed against this build yet."
          hint={
            <>
              With the stack running, <Kbd>make eval</Kbd> runs the harness inside the API
              container and publishes the result here.
            </>
          }
        />
      )}

      {state.kind === "ok" && <Report data={state.data} />}
    </>
  )
}

function Report({
  data,
}: {
  data: { report: EvalReport; source: string; available_providers: string[] }
}) {
  const { report, source, available_providers: providers } = data
  const isMock = report.provider === "mock"

  return (
    <>
      {/* Provenance first. A metric without the configuration that produced it
          is not reproducible, and this project does not publish those. */}
      <div className="hairline mb-5 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] px-5 py-4">
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 text-[13px]">
          <span>
            <span className="text-[var(--color-text-faint)]">Dataset </span>
            <span className="text-[var(--color-text)]">{report.dataset}</span>
          </span>
          <span>
            <span className="text-[var(--color-text-faint)]">Provider </span>
            <span
              className={
                isMock ? "text-[var(--color-warn)]" : "text-[var(--color-ok)]"
              }
            >
              {report.provider}
            </span>
          </span>
          <span>
            <span className="text-[var(--color-text-faint)]">Prompt </span>
            <Mono>{report.prompt_version}</Mono>
          </span>
          <span>
            <span className="text-[var(--color-text-faint)]">Generated </span>
            <Mono>{report.generated_at}</Mono>
          </span>
          <span className="ml-auto">
            <Mono dim>{source}</Mono>
            {providers.length > 1 ? (
              <span className="ml-2 text-[11px] text-[var(--color-text-faint)]">
                also: {providers.filter((p) => p !== report.provider).join(", ")}
              </span>
            ) : null}
          </span>
        </div>

        {isMock ? (
          <div className="mt-3.5">
            <Notice tone="warn">
              These numbers were produced by the <strong>deterministic local provider</strong>, not
              a language model. They measure the pipeline, the retrieval, the safety layer and the
              golden set — and a rule-based classifier scoring perfectly against a set its rules
              were tuned to is a weak signal on its own. What the score does establish is that the
              harness exists, runs on every commit and blocks merges. For a live-model comparison,
              run <Kbd>make eval PROVIDER=anthropic</Kbd>.
            </Notice>
          </div>
        ) : null}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          label="Cases passed"
          value={`${report.passed}/${report.total_cases}`}
          hint={pct(report.pass_rate)}
          tone={report.pass_rate === 1 ? "ok" : "warn"}
          emphasis
        />
        <Metric
          label="Intent accuracy"
          value={pct(report.intent_accuracy)}
          hint={`macro-F1 ${report.intent_macro_f1.toFixed(3)}`}
          emphasis
        />
        <Metric
          label="Escalation precision"
          value={pct(report.escalation_precision)}
          hint="Correctly declined to act"
          emphasis
        />
        <Metric
          label="Adversarial contained"
          value={`${report.adversarial_contained}/${report.adversarial_cases}`}
          hint="Zero tolerance in the CI gate"
          tone={
            report.adversarial_cases > 0 && report.containment_rate === 1 ? "ok" : "danger"
          }
          emphasis
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Panel title="Correctness" description="Against the golden set">
          <dl className="space-y-0.5">
            <Row label="Outcome accuracy" value={pct(report.outcome_accuracy)} />
            <Row label="Tool precision" value={pct(report.tool_precision)} />
            <Row label="Tool recall" value={pct(report.tool_recall)} />
            <Row label="Escalation recall" value={pct(report.escalation_recall)} />
          </dl>
        </Panel>

        <Panel title="Grounding" description="Whether replies can be trusted">
          <dl className="space-y-0.5">
            <Row label="Groundedness" value={pct(report.groundedness_rate)} />
            <Row label="Replies with citations" value={pct(report.citation_rate)} />
            <Row label="Containment rate" value={pct(report.containment_rate)} />
          </dl>
          <p className="mt-3 border-t border-[var(--color-line)] pt-3 text-[11px] leading-relaxed text-[var(--color-text-faint)]">
            A reply that cites no retrieved policy is never sent, regardless of how well it reads.
          </p>
        </Panel>

        <Panel title="Cost and latency" description="Per ticket, measured">
          <dl className="space-y-0.5">
            <Row label="Mean cost" value={`$${report.mean_cost_usd.toFixed(6)}`} mono />
            <Row label="Total cost" value={`$${report.total_cost_usd.toFixed(5)}`} mono />
            <Row label="Latency p50" value={`${report.p50_latency_ms.toFixed(0)} ms`} mono />
            <Row label="Latency p95" value={`${report.p95_latency_ms.toFixed(0)} ms`} mono />
            <Row label="Triage model" value={report.model_fast} mono />
            <Row label="Reasoning model" value={report.model_reasoning} mono />
          </dl>
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Panel title="Per-intent" description="Where classification is strong and weak" padded={false}>
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-[var(--color-line)] bg-[var(--color-surface-2)] text-left text-[11px] uppercase tracking-[0.1em] text-[var(--color-text-faint)]">
                  <th className="px-5 py-2.5 font-medium">Intent</th>
                  <th className="px-3 py-2.5 text-right font-medium">n</th>
                  <th className="px-3 py-2.5 text-right font-medium">Precision</th>
                  <th className="px-3 py-2.5 text-right font-medium">Recall</th>
                  <th className="px-5 py-2.5 text-right font-medium">F1</th>
                </tr>
              </thead>
              <tbody>
                {report.per_intent.map((row) => (
                  <tr
                    key={row.label}
                    className="border-b border-[var(--color-line)] transition-colors duration-[var(--dur-fast)] last:border-0 hover:bg-[var(--color-surface-2)]"
                  >
                    <td className="px-5 py-2.5">{row.label.replace(/_/g, " ")}</td>
                    <td className="tnum px-3 py-2.5 text-right text-[var(--color-text-muted)]">
                      {row.support}
                    </td>
                    <td className="tnum px-3 py-2.5 text-right">{row.precision.toFixed(3)}</td>
                    <td className="tnum px-3 py-2.5 text-right">{row.recall.toFixed(3)}</td>
                    <td
                      className={`tnum px-5 py-2.5 text-right ${
                        row.f1 < 0.9 ? "text-[var(--color-warn)]" : ""
                      }`}
                    >
                      {row.f1.toFixed(3)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <div className="space-y-4">
          {report.failures.length > 0 ? (
            <Panel title={`Failing cases (${report.failures.length})`}>
              <ul className="space-y-1 font-mono text-[11px] text-[var(--color-danger)]">
                {report.failures.map((id) => (
                  <li key={id}>{id}</li>
                ))}
              </ul>
            </Panel>
          ) : (
            <Panel title="Failing cases">
              <p className="text-[13px] text-[var(--color-text-muted)]">
                None. Every case in the golden set met its expectation.
              </p>
            </Panel>
          )}

          <Notice>
            This measures the agent against a fixed golden set. It says nothing about what happened
            to real tickets — for that, see{" "}
            <Link href="/" className="text-[var(--color-accent)]">
              Overview
            </Link>{" "}
            and{" "}
            <Link href="/activity" className="text-[var(--color-accent)]">
              Activity
            </Link>
            .
          </Notice>
        </div>
      </div>
    </>
  )
}
