import { useState } from 'react'
import type {
  DiffRow,
  Escalation,
  EscalationGroup,
  EscalationPayment,
  Evidence,
  RuleComparison,
} from '../api'
import { Panel, PanelMessage } from '../components/Panel'
import { formatPaise, shortId } from '../format'

/*
  Right rail. One card per pending escalation.

  The card leads with the trigger, because the operator's first question is
  "what stopped this", not "what is different". The reason code and a one-line
  cause come first; then the diff, with the field the rule actually examined
  pulled to the top and marked TRIGGER.

  `differs` and `triggered` are deliberately separate. A cart can differ from
  its mandate in a field no rule cared about — that gets a muted "changed, not
  triggering" treatment so it stays visible without competing with the actual
  cause. And a rule can fire on something no cart field shows at all: VELOCITY
  is a rate, so its card has a TRIGGER banner and no triggered row, which is the
  honest rendering.
*/

/**
 * Render two values so their difference is visible even when a naive truncation
 * would make them identical.
 *
 * Two merchant ids that share a long prefix truncate to the same string, which
 * shows the operator two apparently equal values on a row flagged as different.
 * When that happens this drops the common prefix and shows the segment where
 * they actually diverge.
 */
function distinguish(a: string, b: string): { a: string; b: string; elided: boolean } {
  const LIMIT = 22
  if (a === b || (a.length <= LIMIT && b.length <= LIMIT)) {
    return { a, b, elided: false }
  }

  let prefix = 0
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) prefix++

  // Both fit once the shared head is removed: show where they part company.
  if (prefix > 4 && (a.length - prefix > 0 || b.length - prefix > 0)) {
    const keepFrom = Math.max(0, prefix - 3)
    return {
      a: `…${a.slice(keepFrom)}`,
      b: `…${b.slice(keepFrom)}`,
      elided: true,
    }
  }
  return { a, b, elided: false }
}

function formatValue(value: unknown, kind: string): string {
  if (value === null || value === undefined) return '—'
  if (kind === 'paise' && typeof value === 'number') return formatPaise(value)
  if (kind === 'list' && Array.isArray(value)) return value.length ? value.join(', ') : 'none'
  return String(value)
}

function DiffTable({ diff, label }: { diff: DiffRow[]; label?: string }) {
  if (!diff.length) return null
  return (
    <div className="mt-1.5 border border-ink-700">
      {label ? (
        <div className="border-b border-ink-700 bg-ink-850 px-1.5 py-0.5 text-[9px] tracking-[0.08em] text-ink-500 uppercase">
          {label}
        </div>
      ) : null}
      <div className="grid grid-cols-[76px_1fr_1fr] gap-1 border-b border-ink-700 bg-ink-850 px-1.5 py-0.5 text-[9px] font-semibold tracking-[0.08em] text-ink-400 uppercase">
        <span>field</span>
        <span>authorised</span>
        <span>proposed</span>
      </div>

      {diff.map((row) => {
        const rawA = formatValue(row.authorised, row.kind)
        const rawB = formatValue(row.proposed, row.kind)
        // Only disambiguate rows that actually differ — collapsing a matching
        // pair would invent a distinction that isn't there.
        const shown = row.differs
          ? distinguish(rawA, rawB)
          : { a: rawA, b: rawB, elided: false }

        const tone = row.triggered
          ? 'bg-state-escalate/15'
          : row.differs
            ? 'bg-ink-850/60'
            : ''

        return (
          <div
            key={row.field}
            title={row.note ?? undefined}
            className={`grid grid-cols-[76px_1fr_1fr] gap-1 border-b border-ink-700/60 px-1.5 py-0.5 text-[10px] last:border-b-0 ${tone}`}
          >
            <span className="flex min-w-0 flex-col">
              <span
                className={`truncate ${
                  row.triggered
                    ? 'font-semibold text-state-escalate'
                    : row.differs
                      ? 'text-ink-300'
                      : 'text-ink-400'
                }`}
              >
                {row.field}
              </span>
              {row.triggered ? (
                <span
                  title="The rule that fired examined this field"
                  className="w-fit bg-state-escalate px-0.5 text-[8px] leading-[11px] font-bold tracking-[0.08em] text-ink-950"
                >
                  TRIGGER
                </span>
              ) : row.differs ? (
                <span className="text-[8px] leading-[11px] text-ink-600">changed</span>
              ) : null}
            </span>

            <span
              className={`min-w-0 truncate ${
                row.triggered ? 'text-ink-200' : 'text-ink-400'
              }`}
              title={rawA}
            >
              {shown.a}
            </span>

            <span
              className={`min-w-0 truncate ${
                row.triggered
                  ? 'font-semibold text-state-escalate'
                  : row.differs
                    ? 'text-ink-300'
                    : 'text-ink-400'
              }`}
              title={rawB}
            >
              {shown.b}
            </span>
          </div>
        )
      })}
    </div>
  )
}

/**
 * What a history-based rule examined, in place of the cart diff.
 *
 * NEW_MERCHANT and VELOCITY judge transaction history, so a cart diff is the
 * wrong evidence for them: the cart can match its mandate in every field and
 * the rule still fires. Showing an all-matching diff invited the reviewer to
 * conclude nothing was wrong with a session the engine had just stopped.
 */
function EvidenceTable({ evidence }: { evidence: Evidence }) {
  return (
    <div className="mt-1.5 border border-ink-700">
      <div className="border-b border-ink-700 bg-ink-850 px-1.5 py-0.5 text-[9px] font-semibold tracking-[0.08em] text-ink-400 uppercase">
        Evidence
      </div>
      {evidence.rows.map((row) => (
        <div
          key={row.label}
          className={`grid grid-cols-[124px_1fr] gap-1 border-b border-ink-700/60 px-1.5 py-0.5 text-[10px] last:border-b-0 ${
            row.flag ? 'bg-state-escalate/15' : ''
          }`}
        >
          <span
            className={`min-w-0 truncate ${
              row.flag ? 'font-semibold text-state-escalate' : 'text-ink-400'
            }`}
          >
            {row.label}
          </span>
          <span
            className={`min-w-0 truncate ${
              row.flag ? 'font-semibold text-state-escalate' : 'text-ink-300'
            }`}
            title={String(row.value)}
          >
            {row.kind === 'paise' && typeof row.value === 'number'
              ? formatPaise(row.value)
              : String(row.value)}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * The comparison the rule actually made, stated as two named sides.
 *
 * Shown above the diff so the reader weighs the right pair. A full diff of a
 * PRICE_DRIFT escalation also shows cart-total against budget, which for a
 * ₹999 cart under a ₹5,000 budget reads as comfortably within limits while the
 * cause text says 899% over. Both rows were accurate; only one was the rule's.
 */
function ComparisonPanel({ c }: { c: RuleComparison }) {
  return (
    <div className="mt-1.5 border border-state-escalate/40 bg-state-escalate/10">
      <div className="border-b border-state-escalate/30 px-1.5 py-0.5 text-[9px] font-semibold tracking-[0.08em] text-state-escalate uppercase">
        The comparison that fired
      </div>
      <div className="flex items-stretch">
        <div className="flex-1 border-r border-state-escalate/20 px-1.5 py-1">
          <div className="truncate text-[9px] text-ink-400">{c.left_label}</div>
          <div className="tabular text-[12px] text-ink-200">
            {formatPaise(c.left_paise)}
          </div>
        </div>
        <div className="flex items-center px-1 text-[11px] text-state-escalate">→</div>
        <div className="flex-1 px-1.5 py-1">
          <div className="truncate text-[9px] text-ink-400">{c.right_label}</div>
          <div className="tabular text-[12px] font-bold text-state-escalate">
            {formatPaise(c.right_paise)}
          </div>
        </div>
      </div>
      <div className="border-t border-state-escalate/20 px-1.5 py-0.5 text-[9px] text-ink-400">
        {c.threshold_label}: {formatPaise(c.threshold_paise)}
      </div>
    </div>
  )
}

/** Mandate fields the rule did not weigh. Collapsed, and muted when open. */
function RestOfDiff({ rows }: { rows: DiffRow[] }) {
  const [open, setOpen] = useState(false)
  const changed = rows.filter((r) => r.differs).length
  return (
    <div className="mt-1">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full border border-ink-700 px-1.5 py-0.5 text-left text-[9px] text-ink-500 hover:border-ink-600 hover:text-ink-400"
      >
        {open ? '▾' : '▸'} {rows.length} other mandate field
        {rows.length === 1 ? '' : 's'} — not part of this rule
        {changed ? ` (${changed} changed)` : ''}
      </button>
      {open ? (
        <div className="opacity-60">
          <DiffTable diff={rows} />
        </div>
      ) : null}
    </div>
  )
}

function DiffLegend({ diff }: { diff: DiffRow[] }) {
  const changedNotTriggering = diff.filter((r) => r.differs && !r.triggered).length
  if (!changedNotTriggering) return null
  return (
    <div className="mt-1 text-[9px] text-ink-400">
      {changedNotTriggering} field{changedNotTriggering === 1 ? '' : 's'} changed but did
      not trigger this rule
    </div>
  )
}

/*
  The live payment on an approved escalation.

  Everything here comes off the ORDER_CREATED ledger entry the server read back:
  the URL, the QR, the order id. The QR is rendered only when Razorpay actually
  returned one — an absent QR is left absent rather than generated locally, so
  what is on screen is what the provider sent.
*/
function PaymentPanel({ payment }: { payment: EscalationPayment }) {
  const awaiting = payment.state === 'awaiting_capture'
  const captured = payment.state === 'captured'

  return (
    <div
      className={`mb-1 border px-1.5 py-1 ${
        captured
          ? 'border-state-allow/40 bg-state-allow/5'
          : awaiting
            ? 'border-accent/40 bg-accent/5'
            : 'border-state-deny/40 bg-state-deny/5'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={`text-[9px] font-semibold tracking-[0.1em] ${
            captured ? 'text-state-allow' : awaiting ? 'text-accent' : 'text-state-deny'
          }`}
        >
          {captured
            ? 'PAID'
            : awaiting
              ? 'AWAITING PAYMENT'
              : 'PAYMENT FAILED'}
        </span>
        <span className="tabular text-[9px] text-ink-400">
          {payment.amount_paise != null ? formatPaise(payment.amount_paise) : ''}
        </span>
      </div>

      {awaiting && payment.short_url ? (
        <>
          <a
            href={payment.short_url}
            target="_blank"
            rel="noreferrer noopener"
            className="tabular mt-1 block truncate text-[10px] text-accent underline decoration-accent/40 hover:decoration-accent"
            title={payment.short_url}
          >
            {payment.short_url}
          </a>
          {payment.qr_url ? (
            <img
              src={payment.qr_url}
              alt="Razorpay payment QR"
              className="mt-1 size-24 bg-white p-1"
            />
          ) : null}
          <div className="mt-0.5 text-[9px] text-ink-400">
            Polling every 2s · 5 min timeout
          </div>
        </>
      ) : null}

      {captured && payment.razorpay_payment_id ? (
        <div className="tabular mt-0.5 truncate text-[9px] text-ink-300">
          {payment.razorpay_payment_id}
        </div>
      ) : null}

      {payment.state === 'failed' && payment.detail ? (
        <div className="mt-0.5 text-[9px] text-state-deny">{payment.detail}</div>
      ) : null}

      <div className="tabular mt-0.5 truncate text-[9px] text-ink-400">
        {payment.razorpay_order_id} · seq {payment.seq}
      </div>
    </div>
  )
}


function Card({
  group,
  pending,
  failures,
  onDecide,
}: {
  group: EscalationGroup
  pending: Record<string, 'approve' | 'reject'>
  failures: Record<string, string>
  onDecide: (escalation: Escalation, decision: 'approve' | 'reject') => void
}) {
  const escalation = group.escalations[0]!
  const diff = group.diff ?? []
  // History-based rules show the evidence they used; only cart-derived rules
  // show the cart diff.
  const showEvidence = group.history_based && !!group.evidence

  return (
    <div className="border-b border-ink-700 p-2">
      {/* ── Lead with the trigger, then name the merchant it concerns ── */}
      <div className="flex items-start justify-between gap-2">
        <span className="border border-state-escalate/50 bg-state-escalate/15 px-1 text-[10px] font-bold tracking-[0.06em] text-state-escalate">
          {group.reason_code}
        </span>
        <span
          className="shrink-0 text-[10px] text-ink-400"
          title={
            group.session_count > 1
              ? 'The same rule fired for the same merchant across these sessions'
              : undefined
          }
        >
          {group.session_count > 1
            ? `${group.session_count} sessions`
            : shortId(escalation.session_id)}
        </span>
      </div>

      <div
        className="mt-0.5 truncate text-[9px] text-ink-400"
        title={group.merchant_id}
      >
        merchant: {group.merchant_id}
      </div>

      <div className="mt-1 text-[10px] leading-snug text-ink-200">{group.cause}</div>

      {showEvidence ? (
        <div className="mt-1 border-l-2 border-state-escalate/50 pl-1.5 text-[9px] text-ink-400">
          This rule examines transaction history, not the cart.
        </div>
      ) : null}

      <div className="mt-1 text-[9px] leading-snug text-ink-400">{escalation.detail}</div>

      {showEvidence ? (
        <EvidenceTable evidence={group.evidence!} />
      ) : escalation.history_based ? (
        // Escalations raised before evidence capture existed have none to show.
        // Saying so beats presenting a cart diff the rule never looked at.
        <div className="mt-1.5 border border-ink-700 px-1.5 py-1 text-[10px] text-ink-400">
          Evidence for this rule was not recorded when it fired.
        </div>
      ) : (
        <>
          {/* The rule's own comparison first. The remaining mandate fields are
              real but were not what fired, and shown at equal weight they
              invite the reader to draw a conclusion from the wrong pair. */}
          {group.comparison ? <ComparisonPanel c={group.comparison} /> : null}

          {(() => {
            const relevant = diff.filter((r) => r.triggered)
            const rest = diff.filter((r) => !r.triggered)
            return (
              <>
                {group.comparison ? null : <DiffTable diff={relevant} />}
                {rest.length ? (
                  <RestOfDiff rows={rest} />
                ) : null}
                <DiffLegend diff={diff} />
              </>
            )
          })()}
        </>
      )}

      {/* ── One decision row per session ──
          Grouping is a presentation choice; authority is not. Each session is
          approved or rejected on its own, so one click can never release money
          for a session the operator did not look at. */}
      <div className="mt-1.5 flex flex-col gap-1">
        {group.escalations.map((e) => {
          const inFlight = pending[e.id] ?? null
          const failure = failures[e.id] ?? null
          return (
            <div key={e.id} className={inFlight ? 'opacity-50' : ''} aria-busy={!!inFlight}>
              {/* An approved escalation with a live link has nothing left to
                  decide — it has something to pay. The buttons give way to the
                  link so the card shows the one action that is still open. */}
              {e.payment ? <PaymentPanel payment={e.payment} /> : null}
              {group.session_count > 1 ? (
                <div
                  className="tabular truncate text-[9px] text-ink-400"
                  title={`${e.session_id}\n${e.goal ?? ''}`}
                >
                  {shortId(e.session_id)} · {e.goal ?? 'no goal'}
                </div>
              ) : null}

              {failure ? (
                <div className="mb-0.5 border border-state-deny/40 bg-state-deny/10 px-1 py-0.5 text-[10px] text-state-deny">
                  {failure}
                </div>
              ) : null}

              <div className={`flex gap-1.5 ${e.payment ? 'hidden' : ''}`}>
                <button
                  disabled={inFlight !== null}
                  onClick={() => onDecide(e, 'approve')}
                  className="flex-1 border border-state-allow/50 py-0.5 text-[10px] font-semibold tracking-[0.08em] text-state-allow enabled:hover:bg-state-allow/15 disabled:opacity-40"
                >
                  {inFlight === 'approve' ? 'APPROVING…' : 'APPROVE'}
                </button>
                <button
                  disabled={inFlight !== null}
                  onClick={() => onDecide(e, 'reject')}
                  className="flex-1 border border-state-deny/50 py-0.5 text-[10px] font-semibold tracking-[0.08em] text-state-deny enabled:hover:bg-state-deny/15 disabled:opacity-40"
                >
                  {inFlight === 'reject' ? 'REJECTING…' : 'REJECT'}
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface Props {
  groups: EscalationGroup[] | null
  error: string | null
  pending: Record<string, 'approve' | 'reject'>
  failures: Record<string, string>
  onDecide: (escalation: Escalation, decision: 'approve' | 'reject') => void
  /** When set, the rail shows only this session's escalations. */
  selectedId: string | null
  onClearFilter: () => void
}

export function Escalations({
  groups,
  error,
  pending,
  failures,
  onDecide,
  selectedId,
  onClearFilter,
}: Props) {
  // Optimistic: a session whose decision is in flight drops out immediately.
  // If the POST fails, App puts it back with the error attached. A group with
  // no sessions left disappears entirely.
  const live = (groups ?? [])
    .map((g) => ({
      ...g,
      escalations: g.escalations.filter((e) => !pending[e.id] || failures[e.id]),
    }))
    .filter((g) => g.escalations.length > 0)

  // Follow the session selection. Reading one session's ledger while the rail
  // shows three unrelated escalations puts three sessions on screen at once and
  // leaves the reader to work out which card belongs to what they are looking
  // at. The rail subordinates to the selection instead.
  const visible = (selectedId
    ? live
        .map((g) => ({
          ...g,
          escalations: g.escalations.filter((e) => e.session_id === selectedId),
        }))
        .filter((g) => g.escalations.length > 0)
    : live
  ).map((g) => ({ ...g, session_count: g.escalations.length }))

  const pendingCount = visible.reduce((n, g) => n + g.escalations.length, 0)
  const totalPending = live.reduce((n, g) => n + g.escalations.length, 0)
  const elsewhere = totalPending - pendingCount

  return (
    <Panel
      title="Escalations"
      caption="Policy verdicts a human must resolve before money moves."
      className="w-[330px] shrink-0"
      aside={
        <span className="tabular">
          {groups ? pendingCount : '–'} pending
          {selectedId ? <span className="text-ink-600"> · this session</span> : null}
        </span>
      }
    >
      {!groups ? (
        <PanelMessage tone={error ? 'error' : 'muted'}>
          {error ? (
            `escalations unavailable — ${error}`
          ) : (
            <span className="pulse">loading…</span>
          )}
        </PanelMessage>
      ) : visible.length === 0 ? (
        <PanelMessage>
          {selectedId ? (
            <>
              No escalations for this session
              {elsewhere > 0 ? (
                <>
                  <br />
                  <span className="text-ink-400">
                    {elsewhere} pending on other session{elsewhere === 1 ? '' : 's'}
                  </span>
                  <br />
                  <button
                    onClick={onClearFilter}
                    className="mt-1 border border-accent/50 px-1.5 py-0.5 text-[10px] text-accent hover:bg-accent/10"
                  >
                    SHOW ALL
                  </button>
                </>
              ) : (
                <>
                  <br />
                  <span className="text-ink-400">nothing is waiting on a human</span>
                </>
              )}
            </>
          ) : (
            <>
              no escalations pending
              <br />
              <span className="text-ink-400">nothing is waiting on a human</span>
            </>
          )}
        </PanelMessage>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {visible.map((g) => (
            <Card
              key={g.key}
              group={g}
              pending={pending}
              failures={failures}
              onDecide={onDecide}
            />
          ))}
        </div>
      )}
    </Panel>
  )
}
