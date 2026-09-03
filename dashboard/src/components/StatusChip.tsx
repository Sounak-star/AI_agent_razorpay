import type { SessionStatus } from '../api'

/*
  Status colour is the system's one use of colour, so the mapping lives in
  exactly one place.

    running (active)  neutral grey — nothing has been decided yet
    allowed (captured) green
    denied  (failed)   red
    escalated          amber — waiting on a human
    refunded           blue  — money went back
    refund due         blue dashed — owed, deferred until settlement
*/

const STATUS_STYLES: Record<string, { label: string; className: string }> = {
  active: { label: 'RUNNING', className: 'border-ink-600 text-ink-300' },
  captured: {
    label: 'ALLOWED',
    className: 'border-state-allow/40 text-state-allow bg-state-allow/10',
  },
  failed: {
    label: 'DENIED',
    className: 'border-state-deny/40 text-state-deny bg-state-deny/10',
  },
  escalated: {
    label: 'ESCALATED',
    className: 'border-state-escalate/40 text-state-escalate bg-state-escalate/10',
  },
  refunded: {
    label: 'REFUNDED',
    className: 'border-state-refund/40 text-state-refund bg-state-refund/10',
  },
  // Owed a refund the provider deferred until settlement. Dashed, because it
  // is not finished: the buyer has not been repaid and a retry is still due.
  // It must not wear the REFUNDED chip — that would say the money went back.
  refund_pending: {
    label: 'REFUND DUE',
    className:
      'border-dashed border-state-refund/60 text-state-refund bg-state-refund/5',
  },
  // The provider refused outright. Over, and not in the buyer's favour.
  refund_failed: {
    label: 'REFUND FAILED',
    className: 'border-state-deny/40 text-state-deny bg-state-deny/10',
  },
  // A session that stopped making progress before reaching any outcome. It gets
  // the escalate colour and a dashed border: not a clean failure, not live —
  // something a human needs to look at.
  stale: {
    label: 'STALE',
    className:
      'border-dashed border-state-escalate/60 text-state-escalate bg-state-escalate/5',
  },
}

const UNKNOWN = { label: '', className: 'border-ink-600 text-ink-400' }

export function StatusChip({ status }: { status: SessionStatus }) {
  const style = STATUS_STYLES[status] ?? {
    ...UNKNOWN,
    label: String(status).toUpperCase(),
  }
  return (
    <span
      title={`session status: ${status}`}
      className={`inline-block shrink-0 border px-1 text-[9px] leading-[14px] font-semibold tracking-[0.08em] ${style.className}`}
    >
      {style.label}
    </span>
  )
}

/** Verdict chip for a POLICY_EVALUATED ledger row. */
export function DecisionChip({ decision }: { decision: string }) {
  const className =
    decision === 'ALLOW'
      ? 'text-state-allow'
      : decision === 'DENY'
        ? 'text-state-deny'
        : decision === 'ESCALATE'
          ? 'text-state-escalate'
          : 'text-ink-400'
  return <span className={`font-semibold ${className}`}>{decision}</span>
}
