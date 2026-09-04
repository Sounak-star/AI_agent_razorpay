import type { ReactNode } from 'react'
import type { Metrics } from '../api'
import type { View } from '../components/ViewToggle'
import { DASH, formatMs, formatUsdMicros } from '../format'

/*
  Top strip.

  The headline claim gets the top row to itself. "Money moved without
  authorisation: 0" is the strongest thing this system says, and at the same
  size as mean latency it read as one metric among five. It now spans the full
  width with the supporting counts beside it, so the first thing on screen is
  the claim and the evidence for it.

  Everything here comes straight off GET /metrics. The stacked bar is drawn by
  handing each segment a flex-grow equal to its raw count, so the browser's flex
  solver produces the proportions and no percentage is ever computed or shown.

  Every count states its population. The verdict split counts *sessions that
  reached a verdict*, which is why it can be smaller than the session total —
  unlabelled, that looked like a bug.
*/

/*
  Each cell stacks three lines: label, value, provenance. Their line boxes are
  pinned explicitly (11 / 18 / 11 px) and the row height is sized to fit all
  three plus padding. Leaving the heights implicit let the stack grow past a
  fixed 46px row, which pushed the provenance line out of the bottom of the
  strip on the three single-figure cells.
*/
function Cell({
  label,
  children,
  className = '',
  title,
}: {
  label: string
  children: ReactNode
  className?: string
  title?: string
}) {
  return (
    <div
      title={title}
      className={`flex min-w-0 flex-col justify-center gap-1 overflow-hidden border-r border-ink-700 px-3 py-2 ${className}`}
    >
      <div className="shrink-0 truncate t-meta font-semibold tracking-[0.14em] text-ink-400 uppercase">
        {label}
      </div>
      {children}
    </div>
  )
}

/** The sub-line under a figure: what it was measured over. Never wraps. */
function Provenance({ children }: { children: ReactNode }) {
  return (
    <div className="shrink-0 truncate t-meta whitespace-nowrap text-ink-400">
      {children}
    </div>
  )
}

const SEGMENTS = [
  { key: 'ALLOW', label: 'ALLOW', bar: 'bg-state-allow', text: 'text-state-allow' },
  { key: 'DENY', label: 'DENY', bar: 'bg-state-deny', text: 'text-state-deny' },
  {
    key: 'ESCALATE',
    label: 'ESC',
    bar: 'bg-state-escalate',
    text: 'text-state-escalate',
  },
] as const

// ── The headline ──────────────────────────────────────────────────────────────

function Headline({ metrics }: { metrics: Metrics }) {
  const u = metrics.unauthorised_money_movement
  const breached = u.count > 0

  return (
    <div
      className={`flex h-[72px] shrink-0 items-center gap-4 border px-4 ${
        breached
          ? 'border-state-deny bg-state-deny/15'
          : 'border-ink-700 bg-ink-900'
      }`}
    >
      <span
        className={`tabular shrink-0 t-headline leading-none font-bold ${
          breached ? 'text-state-deny' : 'text-state-allow'
        }`}
      >
        {u.count}
      </span>

      <div className="min-w-0 flex-1">
        <div
          className={`t-body font-bold tracking-[0.14em] uppercase ${
            breached ? 'text-state-deny' : 'text-ink-050'
          }`}
        >
          Money moved without authorisation
        </div>
        <div
          className={`truncate t-meta ${
            breached ? 'text-state-deny' : 'text-ink-300'
          }`}
        >
          across {u.movements_checked} money movement
          {u.movements_checked === 1 ? '' : 's'}, {u.policy_denials} policy denial
          {u.policy_denials === 1 ? '' : 's'}, {u.escalations_raised} escalation
          {u.escalations_raised === 1 ? '' : 's'}
        </div>
      </div>

      {breached ? (
        <span className="shrink-0 border border-state-deny bg-state-deny/20 px-2 py-1 t-meta font-bold tracking-[0.1em] text-state-deny">
          BREACH · SEQ {u.offending_entries.map((e) => e.seq).join(', ')}
        </span>
      ) : (
        <span
          className="shrink-0 text-right t-meta leading-tight text-ink-400"
          title="Every money-movement entry was preceded, in its own session, by an ALLOW verdict or an explicit human approval."
        >
          every movement preceded by an
          <br />
          ALLOW verdict or human approval
        </span>
      )}
    </div>
  )
}

/*
  The claim in one sentence, for someone who has never seen this screen.

  The figures are substituted from /metrics rather than written into the string,
  so the sentence cannot drift out of step with the numbers directly above it.
*/
function HeadlineExplainer({ metrics }: { metrics: Metrics }) {
  const sessions = metrics.sessions.total
  return (
    <div className="shrink-0 border border-t-0 border-ink-700 bg-ink-900 px-4 py-1">
      <p className="truncate t-meta leading-tight text-ink-300">
        An AI agent shopped {sessions} time{sessions === 1 ? '' : 's'}. Every rupee it
        moved was pre-authorised by a signed spending limit and cleared by a rule
        engine. Nothing moved without one.
      </p>
    </div>
  )
}

// ── Strip ─────────────────────────────────────────────────────────────────────

export function MetricsStrip({
  metrics,
  error,
  view,
}: {
  metrics: Metrics | null
  error: string | null
  view: View
}) {
  if (!metrics) {
    return (
      <div className="flex h-[182px] shrink-0 items-center border border-ink-700 bg-ink-900 px-3 t-meta text-ink-400">
        {error ? (
          <span className="text-state-deny">metrics unavailable — {error}</span>
        ) : (
          <span className="pulse">loading metrics…</span>
        )}
      </div>
    )
  }

  const { policy, latency, cost, upsell } = metrics

  return (
    <div className="flex shrink-0 flex-col">
      <Headline metrics={metrics} />
      <HeadlineExplainer metrics={metrics} />

      {/* Two strips, one per view.

          The operator strip answers questions a merchant has: how much ran,
          what the engine decided, how many offers landed. The forensic strip
          carries the evidence that the run itself was real and measured —
          latency, model, tokens, ledger size. Both were on screen at once,
          which put "mean latency" beside "money moved without authorisation"
          at the same weight and invited a merchant to read engine telemetry as
          a business figure. Nothing is deleted; it moves. */}
      {view === 'forensic' ? (
        <div className="mt-1.5 flex items-center gap-2 px-1">
          <span className="t-meta font-semibold tracking-[0.16em] text-ink-400 uppercase">
            Run telemetry
          </span>
          <span className="t-meta text-ink-400">
            describes this run, not the merchant&apos;s business
          </span>
          <span className="h-px flex-1 bg-ink-700" />
        </div>
      ) : null}

      <div
        className={`mt-1.5 grid h-[80px] shrink-0 border border-ink-700 bg-ink-900 ${
          view === 'operator'
            ? 'grid-cols-[150px_minmax(196px,1fr)_minmax(210px,240px)]'
            : 'grid-cols-[150px_minmax(190px,1fr)_186px_172px_146px]'
        }`}
      >
        {/* ── Sessions ── */}
        <Cell label="Sessions run" title="All sessions in the database">
          <div className="flex items-baseline gap-1.5">
            <span className="tabular t-header font-semibold text-ink-050">
              {metrics.sessions.total}
            </span>
            <span className="truncate t-meta text-ink-400">
              {metrics.sessions.active} live
              {metrics.sessions.stale > 0 ? (
                <span className="text-state-escalate"> · {metrics.sessions.stale} stale</span>
              ) : null}
              {metrics.sessions.error > 0 ? (
                <span className="text-state-deny"> · {metrics.sessions.error} error</span>
              ) : null}
            </span>
          </div>
          <Provenance>
            {/* Money movements is the merchant-facing denominator: how many
                times money actually moved. The ledger entry count is a
                property of the run, so it moves to forensic. */}
            {view === 'operator'
              ? `${metrics.unauthorised_money_movement.movements_checked} money movements`
              : `${metrics.ledger.total_events} ledger entries`}
          </Provenance>
        </Cell>

        {/* ── Verdict split, explicitly scoped ── */}
        <Cell
          label="Policy verdicts · by session"
          title="Each session counted once, by its most recent POLICY_EVALUATED verdict"
        >
          {policy.total === 0 ? (
            <div className="t-meta text-ink-400">no verdicts recorded</div>
          ) : (
            <>
              <div className="flex h-2.5 w-full shrink-0 overflow-hidden border border-ink-700">
                {SEGMENTS.map((s) => {
                  const count = policy[s.key]
                  if (count === 0) return null
                  return (
                    <div
                      key={s.key}
                      // flex-grow carries the raw count: proportions are solved
                      // by the layout engine, not calculated here.
                      style={{ flexGrow: count }}
                      className={`${s.bar} h-full`}
                      title={`${s.key}: ${count} of ${policy.total} sessions that reached a verdict`}
                    />
                  )
                })}
              </div>
              <div className="flex items-center gap-2.5 overflow-hidden t-meta whitespace-nowrap">
                {SEGMENTS.map((s) => (
                  <span key={s.key} className={`${s.text} tabular`}>
                    {s.label} {policy[s.key]}
                  </span>
                ))}
                <span className="tabular truncate text-ink-400">
                  / {policy.total} of {policy.sessions_total} sessions reached a verdict
                </span>
              </div>
            </>
          )}
        </Cell>

        {/* ── Offers (operator only) ──
            Relabelled from "attach rate". A merchant reads a percentage with a
            SIM badge as a growth number and the badge as decoration; "Offers
            accepted 5 of 6" is the same fact stated so the denominator and the
            simulation are part of the sentence rather than a footnote. */}
        {view === 'operator' ? (
          <Cell
            label="Offers"
            className="border-r-0"
            title={
              'Upsells accepted / sessions offered one. Sessions never offered ' +
              'an upsell are not in the denominator. Acceptance basis: ' +
              upsell.acceptance_basis
            }
          >
            {upsell.offered === 0 ? (
              <div className="t-meta text-ink-400">
                no offers made
              </div>
            ) : (
              <div className="truncate t-body font-semibold text-ink-050">
                Offers accepted{' '}
                <span className="tabular">
                  {upsell.accepted} of {upsell.offered}
                </span>
              </div>
            )}
            <Provenance>
              {upsell.offered === 0 ? (
                'nothing was offered'
              ) : upsell.acceptance_is_simulated ? (
                <span className="text-state-escalate">
                  simulated acceptance
                  {upsell.withheld_no_headroom > 0
                    ? ` · ${upsell.withheld_no_headroom} withheld`
                    : ''}
                </span>
              ) : (
                <>
                  accepted by buyers
                  {upsell.withheld_no_headroom > 0
                    ? ` · ${upsell.withheld_no_headroom} withheld`
                    : ''}
                </>
              )}
            </Provenance>
          </Cell>
        ) : null}

        {/* ── RUN TELEMETRY (forensic only) ── */}
        {view === 'forensic' ? (
          <>
        {/* ── Latency ── */}
        <Cell
          label="Mean latency"
          title="Wall clock is first to last ledger event, settled sessions only. Engine latency is wall clock minus three waits this system does not control: the model API (per LLM_CALL), the payment provider, and the human deciding an escalation. Each excluded wait is reported beneath rather than folded in. There is no artificial delay anywhere in the session path."
        >
          {/* Engine time is the headline: it is the part this system controls.
              Wall clock sits beside it rather than under it, so the cell keeps
              to three lines and neither figure is hidden. */}
          <div className="flex items-baseline gap-1.5">
            <span className="tabular t-header font-semibold text-ink-050">
              {formatMs(latency.engine_mean_ms)}
            </span>
            <span className="truncate t-meta text-ink-400">engine</span>
          </div>
          <Provenance>
            {latency.samples === 0 ? (
              'no settled sessions yet'
            ) : (
              <>
                {formatMs(latency.mean_ms)} wall · n={latency.samples}
                {/* Naming what was taken out. An engine figure that silently
                    excludes a six-minute human wait is not more honest than one
                    that includes it — it is just quieter about being wrong. */}
                {latency.human_wait.samples > 0 ? (
                  <>
                    {' · '}
                    <span title={`${latency.human_wait.samples} escalation(s) waited on a person; excluded from engine time`}>
                      {formatMs(latency.human_wait.mean_ms)} human
                    </span>
                  </>
                ) : null}
                {latency.provider_wait.samples > 0 ? (
                  <>
                    {' · '}
                    <span title={`${latency.provider_wait.samples} payment(s) waited on the provider; excluded from engine time`}>
                      {formatMs(latency.provider_wait.mean_ms)} provider
                    </span>
                  </>
                ) : null}
              </>
            )}
          </Provenance>
        </Cell>

        {/* ── Cost ── */}
        <Cell
          label={
            // Name the model that actually ran, so the figure is attributable.
            cost.usage.primary_model
              ? `Model · ${cost.usage.primary_model.split('/').pop()}`
              : 'Mean cost / session'
          }
          title={
            cost.usage.llm_calls === 0
              ? 'No model calls have been recorded.'
              : `${cost.usage.llm_calls} calls to ${cost.usage.primary_model}` +
                `${cost.usage.provider ? ` via ${cost.usage.provider}` : ''}. ` +
                `${cost.usage.input_tokens} input / ${cost.usage.output_tokens} output tokens, ` +
                'as reported by the provider. Cost needs LLM_PRICE_*_USD_PER_MTOK set.'
          }
        >
          <div className="tabular t-header font-semibold text-ink-050">
            {/* Cost when a rate is configured; otherwise the real measured
                usage, which exists either way. Never a fabricated price. */}
            {cost.samples > 0
              ? formatUsdMicros(cost.mean_usd_micros_per_session)
              : cost.usage.mean_tokens_per_session !== null
                ? `${cost.usage.mean_tokens_per_session.toLocaleString()} tok`
                : DASH}
          </div>
          <Provenance>
            {cost.samples === 0 ? (
              // "No calls" and "calls made but unpriced" are different facts.
              // Reporting the second as the first made several seconds of real
              // model latency look unexplained.
              cost.llm_calls > 0 ? (
                <span className="text-state-escalate">
                  per session · {cost.llm_calls} calls, no rate
                </span>
              ) : (
                <span className="text-ink-400">{DASH} no model calls recorded</span>
              )
            ) : (
              <>
                {cost.llm_calls} calls · n={cost.samples} sessions
              </>
            )}
          </Provenance>
        </Cell>

        {/* Ledger size. A property of the run, not of the merchant's trade. */}
        <Cell label="Ledger" className="border-r-0" title="Hash-chained append-only entries">
          <div className="tabular t-header font-semibold text-ink-050">
            {metrics.ledger.total_events.toLocaleString()}
          </div>
          <Provenance>entries · {metrics.sessions.total} sessions</Provenance>
        </Cell>
          </>
        ) : null}
      </div>
    </div>
  )
}
