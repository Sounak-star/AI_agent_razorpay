import { Fragment } from 'react'
import type { LedgerResponse, VerifyResponse } from '../api'
import { Panel, PanelMessage } from '../components/Panel'
import { DecisionChip } from '../components/StatusChip'
import { decisionOf, formatTime, reasonCodeOf, shortHash, shortId } from '../format'
import { LedgerRowDetail } from './LedgerRowDetail'
import { TraceStrip } from './TraceStrip'

/*
  Centre panel — the hash-chained audit trail.

  Four things this panel has to do without anyone reading a number:

  1. Chain integrity is a badge at the top, green or red. When the chain is
     broken the offending row is filled red and every row after it is dimmed.

  2. Any row expands to show its full payload and the hash arithmetic, with the
     hash re-derived in the browser. Hashes you cannot check are decoration;
     this is what makes the panel evidence.

  3. The trace strip says how far the selected session got, before any row is
     read.

  4. Every count states its scope. The badge counts the whole chain, the pager
     counts the current filter, and both say so — they legitimately differ, and
     unlabelled they looked like a contradiction.
*/

export const PAGE_SIZE = 16

// ── Chain integrity badge ─────────────────────────────────────────────────────

function ChainBadge({
  verify,
  error,
}: {
  verify: VerifyResponse | null
  error: string | null
}) {
  if (!verify) {
    return (
      <div className="flex h-7 shrink-0 items-center border-b border-ink-700 bg-ink-850 px-2 text-[11px] text-ink-400">
        <span className={error ? 'text-state-deny' : 'pulse'}>
          {error ? `CHAIN STATUS UNAVAILABLE — ${error}` : 'VERIFYING CHAIN…'}
        </span>
      </div>
    )
  }

  if (!verify.valid) {
    return (
      <div className="flex h-7 shrink-0 items-center gap-2 border-b border-state-deny/50 bg-state-deny/15 px-2">
        <span className="size-2 shrink-0 bg-state-deny" />
        <span className="text-[12px] font-bold tracking-[0.08em] text-state-deny">
          CHAIN BROKEN AT SEQ {verify.broken_at_seq}
        </span>
        <span className="truncate text-[10px] text-state-deny/80">
          · global chain · entries after this point are not trustworthy
        </span>
      </div>
    )
  }

  return (
    <div className="flex h-7 shrink-0 items-center gap-2 border-b border-state-allow/30 bg-state-allow/10 px-2">
      <span className="size-2 shrink-0 bg-state-allow" />
      <span className="text-[12px] font-bold tracking-[0.08em] text-state-allow">
        CHAIN VERIFIED · {verify.entries} ENTRIES
      </span>
      {/* Scope label: this counts the entire ledger, not the current filter. */}
      <span className="truncate text-[10px] text-ink-400">
        · global · all sessions · every hash re-derived server-side
      </span>
    </div>
  )
}

// ── Tags ──────────────────────────────────────────────────────────────────────

/*
  Provenance badge.

  REPLAYED and SYNTHETIC are different claims and must not be conflated.
  REPLAYED means the identifiers came from a recorded real Razorpay capture;
  SYNTHETIC means they were fabricated because no such recording exists. Badging
  on the event name alone labelled fabricated ids as "replayed", which asserts a
  real payment happened somewhere. It hadn't — evals/fixtures/razorpay_capture.json
  has never been written.

  So the badge reads the payload's own provenance fields, and only the row flag
  the server sets from a real fixture can produce REPLAYED.
*/
type Provenance = 'replayed' | 'synthetic' | null

function provenanceOf(entry: {
  replayed_from_fixture: boolean
  event_type: string
  payload: Record<string, unknown> | null
}): Provenance {
  const payload = entry.payload ?? {}

  // The authoritative signal: set by the server only when a real recorded
  // capture was used.
  if (entry.replayed_from_fixture === true) return 'replayed'
  if (payload.replayed_from_fixture === true) return 'replayed'

  if (payload.synthetic === true) return 'synthetic'

  // A simulated leg with no provenance either way is still not live money.
  if (entry.event_type.includes('SIMULATED')) return 'synthetic'

  return null
}

function ProvenanceTag({ kind }: { kind: Exclude<Provenance, null> }) {
  if (kind === 'replayed') {
    return (
      <span
        title="Identifiers replayed from a recorded real Razorpay capture (evals/fixtures/razorpay_capture.json). No live API call was made in this run."
        className="ml-1 shrink-0 border border-state-refund/50 bg-state-refund/10 px-1 text-[9px] font-semibold tracking-[0.06em] text-state-refund"
      >
        REPLAYED
      </span>
    )
  }
  return (
    <span
      title="Identifiers were generated locally. No real Razorpay payment exists behind this entry, and none has ever been recorded."
      className="ml-1 shrink-0 border border-state-escalate/50 bg-state-escalate/10 px-1 text-[9px] font-semibold tracking-[0.06em] text-state-escalate"
    >
      SYNTHETIC
    </span>
  )
}

// ── Panel ─────────────────────────────────────────────────────────────────────

interface Props {
  ledger: LedgerResponse | null
  verify: VerifyResponse | null
  ledgerError: string | null
  verifyError: string | null
  selectedId: string | null
  selectedStatus: string | null
  offset: number
  onOffsetChange: (offset: number) => void
  expandedSeq: number | null
  onToggleRow: (seq: number) => void
}

// Sized to fit the centre panel at 1280px without a horizontal scrollbar.
//
// prev_hash is deliberately absent. Two truncated hex columns side by side
// invite the eye to match strings it cannot verify by looking; the linkage is
// checked in the expanded row, where the hash is actually re-derived. Dropping
// it returns the width to event_type and reason_code, which are read.
const COLUMNS =
  'grid grid-cols-[12px_34px_82px_minmax(190px,1fr)_150px_72px_58px] gap-1.5'

export function Ledger({
  ledger,
  verify,
  ledgerError,
  verifyError,
  selectedId,
  selectedStatus,
  offset,
  onOffsetChange,
  expandedSeq,
  onToggleRow,
}: Props) {
  const brokenAt = verify && !verify.valid ? verify.broken_at_seq : null

  const total = ledger?.total ?? 0
  const entries = ledger?.entries ?? []
  const pageStart = total === 0 ? 0 : offset + 1
  const pageEnd = offset + entries.length
  const canPrev = offset > 0
  const canNext = pageEnd < total

  return (
    <Panel
      title={selectedId ? `Ledger · session ${shortId(selectedId)}` : 'Ledger · all sessions'}
      caption="Append-only, hash-chained. Any edit breaks every subsequent hash."
      className="min-w-0 flex-1"
      aside={
        <>
          {/* Scope label: this pager counts only what the current filter shows. */}
          <span className="tabular" title={
            selectedId
              ? 'entries in the selected session'
              : 'entries across all sessions'
          }>
            {pageStart}–{pageEnd} of {total}{' '}
            <span className="text-ink-600">
              {selectedId ? 'in session' : 'all sessions'}
            </span>
          </span>
          <button
            disabled={!canPrev}
            onClick={() => onOffsetChange(Math.max(0, offset - PAGE_SIZE))}
            className="border border-ink-600 px-1.5 text-[10px] enabled:hover:border-accent enabled:hover:text-accent disabled:opacity-30"
          >
            PREV
          </button>
          <button
            disabled={!canNext}
            onClick={() => onOffsetChange(offset + PAGE_SIZE)}
            className="border border-ink-600 px-1.5 text-[10px] enabled:hover:border-accent enabled:hover:text-accent disabled:opacity-30"
          >
            NEXT
          </button>
        </>
      }
    >
      <ChainBadge verify={verify} error={verifyError} />

      {selectedId ? (
        <TraceStrip entries={entries} sessionStatus={selectedStatus} />
      ) : null}

      {/* Column heads */}
      <div
        className={`${COLUMNS} shrink-0 border-b border-ink-700 bg-ink-850 px-2 py-1 text-[9px] font-semibold tracking-[0.1em] text-ink-400 uppercase`}
      >
        <span />
        <span className="text-right">seq</span>
        <span>ts</span>
        <span>event_type</span>
        <span>reason_code</span>
        <span>hash</span>
        <span className="text-right">session</span>
      </div>

      {!ledger ? (
        <PanelMessage tone={ledgerError ? 'error' : 'muted'}>
          {ledgerError ? (
            `ledger unavailable — ${ledgerError}`
          ) : (
            <span className="pulse">loading ledger…</span>
          )}
        </PanelMessage>
      ) : entries.length === 0 ? (
        <PanelMessage>
          {selectedId
            ? 'no ledger entries for this session'
            : 'ledger is empty — no events recorded yet'}
        </PanelMessage>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {entries.map((e) => {
            const isBreak = brokenAt !== null && e.seq === brokenAt
            const isAfterBreak = brokenAt !== null && e.seq > brokenAt
            const expanded = expandedSeq === e.seq
            const reason = reasonCodeOf(e.payload)
            const decision = decisionOf(e.payload)

            return (
              <Fragment key={e.seq}>
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => onToggleRow(e.seq)}
                  onKeyDown={(ev) => {
                    if (ev.key === 'Enter' || ev.key === ' ') {
                      ev.preventDefault()
                      onToggleRow(e.seq)
                    }
                  }}
                  title="Click to expand: full payload, hash inputs, and an independent re-derivation"
                  className={`${COLUMNS} cursor-pointer border-b border-ink-700/60 px-2 py-[3px] text-[11px] ${
                    isBreak
                      ? 'border-state-deny/50 bg-state-deny/20'
                      : expanded
                        ? 'bg-ink-800'
                        : isAfterBreak
                          ? // Dimmed, not hidden: these entries still exist,
                            // they just can no longer be trusted.
                            'opacity-30 hover:bg-ink-800'
                          : 'hover:bg-ink-800'
                  }`}
                >
                  <span
                    className={`select-none ${
                      expanded ? 'text-accent' : 'text-ink-600'
                    }`}
                  >
                    {expanded ? '▾' : '▸'}
                  </span>

                  <span
                    className={`tabular text-right ${
                      isBreak ? 'font-bold text-state-deny' : 'text-ink-400'
                    }`}
                  >
                    {e.seq}
                  </span>

                  <span className="tabular truncate text-ink-400" title={e.ts}>
                    {formatTime(e.ts)}
                  </span>

                  <span className="flex min-w-0 items-center">
                    <span
                      className={`min-w-0 truncate ${
                        isBreak ? 'text-state-deny' : 'text-ink-100'
                      }`}
                    >
                      {e.event_type}
                    </span>
                    {provenanceOf(e) ? <ProvenanceTag kind={provenanceOf(e)!} /> : null}
                  </span>

                  <span className="truncate">
                    {decision ? (
                      <>
                        <DecisionChip decision={decision} />
                        {reason && reason !== decision ? (
                          <span className="text-ink-300"> · {reason}</span>
                        ) : null}
                      </>
                    ) : reason ? (
                      <span className="text-ink-300">{reason}</span>
                    ) : (
                      <span className="text-ink-600">—</span>
                    )}
                  </span>

                  <span
                    className="tabular cursor-help truncate text-ink-300"
                    title={e.hash ?? 'no hash recorded'}
                  >
                    {shortHash(e.hash)}
                  </span>

                  <span
                    className="tabular cursor-help truncate text-right text-ink-400"
                    title={e.session_id}
                  >
                    {shortId(e.session_id)}
                  </span>
                </div>

                {expanded ? <LedgerRowDetail entry={e} /> : null}
              </Fragment>
            )
          })}
        </div>
      )}
    </Panel>
  )
}
