import type { Metrics, VerifyResponse } from '../api'

/*
  The centre panel before anything is selected.

  This is the first thing on screen and, for most of a demo, the only thing a
  viewer reads. It replaces "select a session on the left", which told them what
  to do without telling them what they were looking at.

  Every figure comes from /metrics or /ledger/verify. Nothing here is written
  into the markup — a landing screen quoting a session count it invented would
  undercut the one claim the whole system exists to make. While the numbers are
  still loading the tiles show a dash rather than a zero, because "0 sessions"
  and "not loaded yet" are different statements.
*/

function Stat({
  value,
  label,
  tone = 'normal',
  title,
}: {
  value: string
  label: string
  tone?: 'normal' | 'good'
  title?: string
}) {
  return (
    <div
      title={title}
      className="flex min-w-0 flex-col gap-0.5 border border-ink-700 bg-ink-900 px-3 py-2"
    >
      <span
        className={`tabular truncate text-[20px] leading-[22px] font-semibold ${
          tone === 'good' ? 'text-state-allow' : 'text-ink-050'
        }`}
      >
        {value}
      </span>
      <span className="truncate text-[9px] leading-[11px] tracking-[0.12em] text-ink-400 uppercase">
        {label}
      </span>
    </div>
  )
}

const DASH = '—'

export function Landing({
  metrics,
  verify,
}: {
  metrics: Metrics | null
  verify: VerifyResponse | null
}) {
  const num = (n: number | null | undefined) =>
    typeof n === 'number' ? n.toLocaleString() : DASH

  const sessions = metrics?.sessions.total
  const entries = metrics?.ledger.total_events
  const unauthorised = metrics?.unauthorised_money_movement.count
  const rules = metrics?.coverage?.policy_rules
  const attacks = metrics?.coverage?.adversarial_attacks

  // The chain is only claimed verified when the endpoint actually said so.
  const chainKnown = verify != null
  const chainOk = verify?.valid === true

  return (
    <div className="flex min-h-0 flex-1 flex-col justify-center gap-5 overflow-y-auto px-8 py-4">
      <div className="flex flex-col gap-2">
        <h1 className="text-[19px] leading-[24px] font-bold tracking-[0.01em] text-ink-050">
          Every money action explainable, bounded and gated.
        </h1>
        <p className="max-w-[62ch] text-[11px] leading-[17px] text-ink-300">
          An AI agent shops on Razorpay test-mode APIs. A deterministic policy
          engine, not the model, decides whether money moves. Every decision is
          hash-chained and verifiable.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-1.5">
        <Stat
          value={num(sessions)}
          label="sessions"
          title="Every session in the database"
        />
        <Stat
          value={num(entries)}
          label="ledger entries"
          title="Hash-chained, append-only"
        />
        <Stat
          value={num(unauthorised)}
          label="unauthorised movements"
          tone={unauthorised === 0 ? 'good' : 'normal'}
          title="Money movements not preceded by an ALLOW verdict or a human approval"
        />
        <Stat
          value={num(rules)}
          label="policy rules"
          title="Counted from the rule list the engine iterates"
        />
        <Stat
          value={num(attacks)}
          label="adversarial attacks"
          title="Counted from the files in evals/attacks"
        />
        <Stat
          value={chainKnown ? (chainOk ? 'verified ✓' : 'BROKEN') : DASH}
          label="hash chain"
          tone={chainOk ? 'good' : 'normal'}
          title={
            chainKnown
              ? chainOk
                ? `Every hash re-derived and linked, ${verify?.entries ?? 0} entries`
                : `Chain broken at seq ${verify?.broken_at_seq}`
              : 'Not yet checked'
          }
        />
      </div>

      <p className="text-[11px] leading-[17px] text-ink-300">
        Select a session to see its full story, with a link to the proof for
        every line.
      </p>
    </div>
  )
}
