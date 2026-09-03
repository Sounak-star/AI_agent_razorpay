import { useCallback, useEffect, useState } from 'react'
import { api, type Escalation } from './api'
import { StubBanner } from './components/StubBanner'
import { ViewToggle, type View } from './components/ViewToggle'
import { Escalations } from './panels/Escalations'
import { Landing } from './panels/Landing'
import { Launcher } from './panels/Launcher'
import { Ledger, PAGE_SIZE } from './panels/Ledger'
import { MetricsStrip } from './panels/MetricsStrip'
import { OperatorView } from './panels/OperatorView'
import { SessionStream } from './panels/SessionStream'
import { usePoll } from './usePoll'

/*
  Single page, no routing. Four panels on one 1280x720 frame:

    ┌──────── headline: money moved without authorisation ────────┐
    ├──────────────────── metrics strip ──────────────────────────┤
    │ sessions │ ledger ......................... │ escalations   │
    └─────────────────────────────────────────────────────────────┘

  Everything polls on a 2s tick. The layout is fixed-height with each panel
  scrolling internally, so the frame never reflows during a demo.
*/

const POLL_MS = 2000

// Health changes only when the process is restarted, so it is polled slowly.
const HEALTH_POLL_MS = 30_000

// ── Poll bindings ─────────────────────────────────────────────────────────────

const usePollSessions = () => usePoll(() => api.sessions(), POLL_MS)
const usePollMetrics = () => usePoll(() => api.metrics(), POLL_MS)
const usePollEscalations = () => usePoll(() => api.escalations(), POLL_MS)
const usePollVerify = () => usePoll(() => api.verify(), POLL_MS)
const usePollHealth = () => usePoll(() => api.health(), HEALTH_POLL_MS)

const usePollLedger = (
  selectedId: string | null,
  offset: number,
  aroundSeq: number | null,
) =>
  usePoll(
    () =>
      selectedId
        ? api.sessionLedger(selectedId, PAGE_SIZE, offset, aroundSeq)
        : api.ledger(PAGE_SIZE, offset),
    POLL_MS,
    [selectedId, offset, aroundSeq],
  )

// Narrative only matters when a session is selected; it is cheap and static
// once the session settles, so it polls on the same tick as everything else.
const usePollNarrative = (selectedId: string | null) =>
  usePoll(
    () => (selectedId ? api.narrative(selectedId) : Promise.resolve(null)),
    POLL_MS,
    [selectedId],
  )

export default function App() {
  const [view, setView] = useState<View>('operator')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const [expandedSeq, setExpandedSeq] = useState<number | null>(null)
  // Set when an operator line is clicked: the server returns whichever page
  // holds that seq, and the client adopts the offset it comes back with.
  const [aroundSeq, setAroundSeq] = useState<number | null>(null)

  // Optimistic escalation state: what is in flight, and what came back failed.
  const [pending, setPending] = useState<Record<string, 'approve' | 'reject'>>({})
  const [failures, setFailures] = useState<Record<string, string>>({})

  const sessions = usePollSessions()
  const metrics = usePollMetrics()
  const escalations = usePollEscalations()
  const verify = usePollVerify()
  const health = usePollHealth()
  const ledger = usePollLedger(selectedId, offset, aroundSeq)
  const narrative = usePollNarrative(selectedId)

  // Changing the session filter starts a new pagination context; keeping the
  // old offset would land the operator on an empty page, and an expanded row
  // from the previous filter is no longer on screen.
  useEffect(() => {
    setOffset(0)
    setExpandedSeq(null)
    setAroundSeq(null)
  }, [selectedId])

  // Drop a selection whose session no longer exists.
  //
  // Re-seeding the database (demo/seed.py --reset) replaces every session with
  // fresh ids. An open dashboard kept polling the id it had, so the narrative
  // endpoint answered 404 twice a second forever and the panel sat on an error
  // until someone reloaded by hand — during a demo, exactly when nobody wants
  // to be reaching for the keyboard.
  useEffect(() => {
    const list = sessions.data?.sessions
    if (!list || !selectedId) return
    if (!list.some((s) => s.session_id === selectedId)) {
      setSelectedId(null)
    }
  }, [sessions.data, selectedId])

  // Adopt the page the server chose for a seq jump, then drop the request so
  // ordinary paging works again.
  useEffect(() => {
    if (aroundSeq === null || !ledger.data) return
    if (ledger.data.offset !== offset) setOffset(ledger.data.offset)
    setAroundSeq(null)
  }, [aroundSeq, ledger.data, offset])

  // The link that makes the operator layer worth having: jump from a sentence
  // to the signed entry it was read from.
  const jumpToSeq = useCallback((sessionId: string, seq: number) => {
    setView('forensic')
    setSelectedId(sessionId)
    setExpandedSeq(seq)
    setAroundSeq(seq)
  }, [])

  const toggleRow = useCallback((seq: number) => {
    setExpandedSeq((current) => (current === seq ? null : seq))
  }, [])

  const selectedSession =
    sessions.data?.sessions.find((s) => s.session_id === selectedId) ?? null
  const selectedStatus = selectedSession?.status ?? null

  const handleDecide = useCallback(
    async (escalation: Escalation, decision: 'approve' | 'reject') => {
      // Optimistic: hide the card now.
      setPending((p) => ({ ...p, [escalation.id]: decision }))
      setFailures((f) => {
        const { [escalation.id]: _removed, ...rest } = f
        return rest
      })

      try {
        await api.decideEscalation(escalation.session_id, escalation.id, decision)
        // Pull the authoritative state rather than patching it locally: the
        // decision also writes a ledger entry and moves the session status.
        escalations.refresh()
        ledger.refresh()
        sessions.refresh()
        metrics.refresh()
        verify.refresh()
      } catch (err) {
        // Revert: the card comes back, carrying the reason it failed.
        setFailures((f) => ({
          ...f,
          [escalation.id]: err instanceof Error ? err.message : String(err),
        }))
      } finally {
        setPending((p) => {
          const { [escalation.id]: _done, ...rest } = p
          return rest
        })
      }
    },
    [escalations, ledger, sessions, metrics, verify],
  )

  const linkDown =
    sessions.error ?? ledger.error ?? metrics.error ?? escalations.error ?? verify.error

  return (
    <div className="flex h-full flex-col gap-1.5 p-1.5">
      <Header linkDown={linkDown} view={view} onViewChange={setView} />

      <StubBanner health={health.data} />

      <MetricsStrip metrics={metrics.data} error={metrics.error} view={view} />

      <main className="flex min-h-0 flex-1 gap-1.5">
        <SessionStream
          sessions={sessions.data?.sessions ?? null}
          error={sessions.error}
          selectedId={selectedId}
          onSelect={setSelectedId}
          launcher={
            <Launcher
              onLaunched={(id) => {
                // Selected immediately, before the run starts, so the operator
                // watches the narrative fill in rather than waiting for a
                // finished session to appear in the list.
                setView('operator')
                setSelectedId(id)
                sessions.refresh()
              }}
            />
          }
        />

        {view === 'operator' ? (
          <OperatorView
            session={selectedSession}
            narrative={narrative.data}
            error={narrative.error}
            onJumpToSeq={jumpToSeq}
            landing={<Landing metrics={metrics.data} verify={verify.data} />}
          />
        ) : (
          <Ledger
            ledger={ledger.data}
            verify={verify.data}
            ledgerError={ledger.error}
            verifyError={verify.error}
            selectedId={selectedId}
            selectedStatus={selectedStatus}
            offset={offset}
            onOffsetChange={setOffset}
            expandedSeq={expandedSeq}
            onToggleRow={toggleRow}
          />
        )}

        <Escalations
          groups={escalations.data?.groups ?? null}
          error={escalations.error}
          pending={pending}
          failures={failures}
          onDecide={handleDecide}
          selectedId={selectedId}
          onClearFilter={() => setSelectedId(null)}
        />
      </main>
    </div>
  )
}

// ── Header ────────────────────────────────────────────────────────────────────

function Header({
  linkDown,
  view,
  onViewChange,
}: {
  linkDown: string | null
  view: View
  onViewChange: (view: View) => void
}) {
  return (
    <header className="flex h-5 shrink-0 items-center justify-between px-1">
      <div className="flex items-baseline gap-2">
        <span className="text-[13px] font-bold tracking-[0.22em] text-ink-050">
          TOLLGATE
        </span>
        <span className="text-[10px] tracking-[0.12em] text-ink-400 uppercase">
          governed agentic-commerce rail
        </span>
      </div>
      <div className="flex items-center gap-2 text-[10px] text-ink-400">
        <ViewToggle view={view} onChange={onViewChange} />
        {linkDown ? (
          <span className="text-state-deny">API LINK DOWN — {linkDown}</span>
        ) : (
          <span>
            <span className="mr-1 inline-block size-1.5 translate-y-[-1px] bg-state-allow" />
            LIVE · {POLL_MS / 1000}s POLL
          </span>
        )}
      </div>
    </header>
  )
}
