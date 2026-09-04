import type { ReactNode } from 'react'
import type { SessionRow } from '../api'
import { Panel, PanelMessage } from '../components/Panel'
import { StatusChip } from '../components/StatusChip'
import { formatElapsed, formatPaise, shortId } from '../format'

/*
  Offer state at a glance. Four outcomes, and they are not interchangeable:
  "withheld" means the headroom guard stopped the offer, which is the rail
  making a decision, while "declined" is the buyer making one.
*/
const OFFER_CHIP: Record<string, { label: string; className: string }> = {
  accepted: { label: 'OFFER+', className: 'border-state-allow/40 text-state-allow' },
  declined: { label: 'OFFER-', className: 'border-ink-600 text-ink-400' },
  offered: { label: 'OFFER', className: 'border-ink-600 text-ink-300' },
  withheld: {
    label: 'OFFER×',
    className: 'border-state-escalate/40 text-state-escalate',
  },
}

/*
  Left rail. One row per session, polled every 2s.

  Clicking a row filters the ledger to that session; clicking it again clears
  the filter. `elapsed` is whatever the server sent on the most recent poll —
  the rail ticks because the server recomputes it, not because the browser runs
  a clock.
*/

interface Props {
  sessions: SessionRow[] | null
  error: string | null
  selectedId: string | null
  onSelect: (sessionId: string | null) => void
  /** Launch control, rendered above the list. */
  launcher?: ReactNode
}

export function SessionStream({
  sessions,
  error,
  selectedId,
  onSelect,
  launcher,
}: Props) {
  return (
    <Panel
      title="Session stream"
      aside={
        <>
          {selectedId ? (
            <button
              onClick={() => onSelect(null)}
              className="border border-accent/50 px-1 t-meta text-accent hover:bg-accent/10"
            >
              CLEAR FILTER
            </button>
          ) : null}
          <span className="tabular">{sessions ? sessions.length : '–'}</span>
        </>
      }
      caption="One row per session. Click to filter the ledger to it."
      className="w-[248px] shrink-0"
    >
      {launcher}

      {!sessions ? (
        <PanelMessage tone={error ? 'error' : 'muted'}>
          {error ? `sessions unavailable — ${error}` : <span className="pulse">loading…</span>}
        </PanelMessage>
      ) : sessions.length === 0 ? (
        <PanelMessage>
          no sessions yet
          <br />
          <span className="text-ink-400">
            describe something to buy above and press RUN
          </span>
        </PanelMessage>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions.map((s) => {
            const selected = s.session_id === selectedId
            return (
              <button
                key={s.session_id}
                onClick={() => onSelect(selected ? null : s.session_id)}
                title={`${s.session_id}\nbuyer: ${s.buyer_id}\nbudget: ${formatPaise(
                  s.budget_paise,
                )}\nevents: ${s.event_count}`}
                className={`t-transition t-focus block w-full border-b border-ink-700 px-3 py-0.5 text-left ${
                  selected
                    // The accent is the left edge, not a wash across the card.
                    // A filled row competed with the status chip inside it and
                    // tinted every value it contained.
                    ? 'border-l-2 border-l-accent pl-[10px] bg-ink-800/40'
                    : 'border-l-2 border-l-transparent pl-[10px] hover:bg-ink-800'
                }`}
              >
                {/* Two lines, not three.

                    Every field the card carried is still here; the id and the
                    buyer share the top line, and the offer chip moves next to
                    the elapsed time. Three 16px lines plus padding made a 75px
                    card, and eight of those need 600px of a rail that has
                    about 350 — so the rail showed three sessions and the
                    operator scrolled to find anything. */}
                <div className="flex items-center justify-between gap-1">
                  <span className="flex min-w-0 flex-1 items-baseline gap-1">
                    <span
                      className={`tabular shrink-0 t-meta ${
                        selected ? 'text-ink-050' : 'text-ink-300'
                      }`}
                    >
                      {shortId(s.session_id)}
                    </span>
                    <span className="truncate t-meta text-ink-400">
                      {s.buyer_id}
                    </span>
                  </span>
                  <StatusChip
                    status={
                      // A session still marked active but blocked on a human is
                      // shown as escalated — that is what the operator has to
                      // act on, and the queue should be visible in the rail.
                      s.has_pending_escalation && s.status === 'active'
                        ? 'escalated'
                        : s.status
                    }
                  />
                </div>

                <div className="mt-0.5 flex items-baseline justify-between gap-2">
                  <span className="min-w-0 flex-1 truncate t-meta text-ink-100">
                    {s.goal ?? <span className="text-ink-400">(no goal)</span>}
                  </span>
                  <span className="flex shrink-0 items-center gap-1 t-meta text-ink-400">
                    {s.offer && OFFER_CHIP[s.offer] ? (
                      <span
                        title={`Upsell ${s.offer}`}
                        className={`rounded-[2px] border px-1 t-meta font-semibold ${
                          OFFER_CHIP[s.offer]!.className
                        }`}
                      >
                        {OFFER_CHIP[s.offer]!.label}
                      </span>
                    ) : null}
                    <span className="tabular">{formatElapsed(s.elapsed_ms)}</span>
                  </span>
                </div>

              </button>
            )
          })}
        </div>
      )}
    </Panel>
  )
}
