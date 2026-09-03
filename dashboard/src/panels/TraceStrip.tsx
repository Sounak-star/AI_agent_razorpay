import type { LedgerRow } from '../api'

/*
  Session trace — a horizontal step rail above the ledger table.

  Reading nine rows of a table to work out how far a session got is slow. This
  says it in one glance, and it puts the two gates where they belong: POLICY and
  MANDATE are drawn with a distinct glyph because they are the steps that can
  stop the session, not just record it.

  Every step's state is derived from ledger entries that exist. A step is
  complete because its event is in the chain, never because a later step
  implies it — a session missing CART_SIGNED shows a hollow MANDATE gate, which
  is exactly the signal worth seeing.
*/

interface Step {
  key: string
  label: string
  /** Any of these events marks the step reached. */
  events: string[]
  /** Gates can stop the session; they get a distinct glyph. */
  gate?: boolean
  /** Events that mean this step was reached but failed. */
  failEvents?: string[]
}

const STEPS: Step[] = [
  { key: 'intent', label: 'INTENT', events: ['INTENT_SIGNED'] },
  { key: 'catalog', label: 'CATALOG', events: ['CATALOG_QUERIED'] },
  { key: 'quote', label: 'QUOTE', events: ['QUOTE_ISSUED'] },
  { key: 'cart', label: 'CART', events: ['CART_BUILT'] },
  {
    key: 'policy',
    label: 'POLICY',
    events: ['POLICY_EVALUATED'],
    gate: true,
    failEvents: ['ESCALATED', 'HUMAN_REJECTED'],
  },
  { key: 'mandate', label: 'MANDATE', events: ['CART_SIGNED'], gate: true },
  { key: 'order', label: 'ORDER', events: ['ORDER_CREATED'] },
  {
    key: 'payment',
    label: 'PAYMENT',
    events: ['PAYMENT_CAPTURED', 'PAYMENT_SIMULATED'],
    failEvents: ['FULFILMENT_FAILED', 'REFUND_INITIATED', 'REFUND_SIMULATED', 'REFUND_CONFIRMED'],
  },
  {
    key: 'closed',
    label: 'CLOSED',
    events: ['SESSION_CLOSED'],
    failEvents: ['SESSION_STALE'],
  },
]

type StepState = 'complete' | 'current' | 'pending' | 'failed' | 'held'

/**
 * The state of one step.
 *
 * The gates get special treatment, because the gate is the point of the strip.
 * A denied session is stopped *at* POLICY — marking POLICY complete and
 * painting CLOSED red instead would show the outcome while hiding which gate
 * produced it. So the verdict on the POLICY_EVALUATED entry decides that step:
 * DENY is a failure at the gate, ESCALATE is held at the gate (amber, not red —
 * nothing failed, a human was asked), and SESSION_CLOSED stays complete because
 * closing cleanly is what it did.
 */
function stateFor(
  step: Step,
  present: Set<string>,
  lastIndex: number,
  index: number,
  policyDecision: string | null,
  sessionStalled: boolean,
): StepState {
  const reached = step.events.some((e) => present.has(e))
  const failedHere = (step.failEvents ?? []).some((e) => present.has(e))

  if (step.key === 'policy' && reached) {
    if (policyDecision === 'DENY') return 'failed'
    if (policyDecision === 'ESCALATE' || present.has('ESCALATED')) return 'held'
    return 'complete'
  }

  if (step.key === 'closed') {
    if (present.has('SESSION_STALE')) return 'failed'
    if (reached) return 'complete'
  }

  if (failedHere && !reached) return 'failed'
  if (reached) return 'complete'

  const halted = policyDecision === 'DENY' || present.has('ESCALATED') || sessionStalled
  if (index === lastIndex + 1 && !halted) return 'current'
  return 'pending'
}

const DOT: Record<StepState, string> = {
  complete: 'bg-state-allow border-state-allow',
  current: 'bg-accent border-accent pulse',
  failed: 'bg-state-deny border-state-deny',
  held: 'bg-state-escalate border-state-escalate',
  pending: 'bg-transparent border-ink-600',
}

const TEXT: Record<StepState, string> = {
  complete: 'text-ink-300',
  current: 'text-accent',
  failed: 'text-state-deny',
  held: 'text-state-escalate',
  pending: 'text-ink-600',
}

export function TraceStrip({
  entries,
  sessionStatus,
}: {
  entries: LedgerRow[]
  sessionStatus: string | null
}) {
  const present = new Set(entries.map((e) => e.event_type))
  const sessionStalled = sessionStatus === 'stale'

  // The verdict recorded on this session's most recent POLICY_EVALUATED entry.
  // Read from the payload the API returned, not inferred from session status.
  const policyDecision =
    entries
      .filter((e) => e.event_type === 'POLICY_EVALUATED')
      .map((e) => {
        const d = (e.payload ?? {}).decision
        return typeof d === 'string' ? d : null
      })
      .filter(Boolean)
      .pop() ?? null

  // The furthest step with an event actually in the chain.
  let lastIndex = -1
  STEPS.forEach((step, i) => {
    if (step.events.some((e) => present.has(e))) lastIndex = i
  })

  return (
    <div className="flex shrink-0 items-center gap-0 border-b border-ink-700 bg-ink-850 px-2 py-1.5">
      <span className="mr-2 shrink-0 text-[9px] font-semibold tracking-[0.12em] text-ink-400 uppercase">
        Trace
      </span>

      {STEPS.map((step, i) => {
        const state = stateFor(
          step, present, lastIndex, i, policyDecision, sessionStalled,
        )
        return (
          <div key={step.key} className="flex min-w-0 flex-1 items-center">
            <div
              className="flex min-w-0 flex-col items-center gap-0.5"
              title={
                step.gate
                  ? `${step.label} — gate: this step can stop the session (${state})`
                  : `${step.label} — ${state}`
              }
            >
              {/* Gates are diamonds, ordinary steps are squares. */}
              <span
                className={`size-2 shrink-0 border ${DOT[state]} ${
                  step.gate ? 'rotate-45' : ''
                }`}
              />
              <span
                className={`truncate text-[8px] tracking-[0.06em] ${TEXT[state]} ${
                  step.gate ? 'font-bold' : ''
                }`}
              >
                {step.gate ? `◆${step.label}` : step.label}
              </span>
            </div>

            {i < STEPS.length - 1 ? (
              <span
                className={`mx-0.5 h-px min-w-2 flex-1 ${
                  i < lastIndex ? 'bg-state-allow/50' : 'bg-ink-700'
                }`}
              />
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
