import { useEffect, useMemo, useState } from 'react'
import type { LedgerRow } from '../api'
import { preimageFor, rederive, type Rederivation } from '../canonical'

/*
  The expanded body of a ledger row.

  A viewer looking at a truncated hash has to take it on faith. This panel
  removes the faith: it shows the full payload, the exact byte string that was
  hashed, the hash this browser computed from that string, and the hash the
  server stored — then says whether they match.

  The verdict is computed here, in the browser, from the payload the API
  returned. It is not the server's verify endpoint restated. That distinction is
  the whole point: these two checks can disagree, and if they ever do, the one
  on this screen is the one that was independently derived.
*/

function Field({
  label,
  value,
  tone = 'normal',
  wrap = true,
}: {
  label: string
  value: string
  tone?: 'normal' | 'good' | 'bad'
  wrap?: boolean
}) {
  const color =
    tone === 'good' ? 'text-state-allow' : tone === 'bad' ? 'text-state-deny' : 'text-ink-300'
  return (
    <div className="flex gap-2">
      <span className="w-[104px] shrink-0 t-meta tracking-[0.06em] text-ink-400 uppercase">
        {label}
      </span>
      <span
        className={`tabular min-w-0 flex-1 t-meta ${color} ${
          wrap ? 'break-all' : 'truncate'
        }`}
      >
        {value}
      </span>
    </div>
  )
}

function VerdictBadge({ result }: { result: Rederivation | null }) {
  if (!result) {
    return (
      <span className="border border-ink-600 px-1.5 py-0.5 t-meta text-ink-400">
        RE-DERIVING…
      </span>
    )
  }
  if (result.state === 'verified') {
    return (
      <span
        title="This browser hashed the payload shown below and got the stored hash."
        className="border border-state-allow/50 bg-state-allow/10 px-1.5 py-0.5 t-meta font-bold tracking-[0.06em] text-state-allow"
      >
        RE-DERIVED ✓
      </span>
    )
  }
  if (result.state === 'mismatch') {
    return (
      <span
        title="The hash computed here does not match the stored hash. Compare the values below."
        className="border border-state-deny/60 bg-state-deny/15 px-1.5 py-0.5 t-meta font-bold tracking-[0.06em] text-state-deny"
      >
        MISMATCH ✗
      </span>
    )
  }
  return (
    <span
      title={result.reason ?? 'could not compute'}
      className="border border-state-escalate/50 bg-state-escalate/10 px-1.5 py-0.5 t-meta font-bold tracking-[0.06em] text-state-escalate"
    >
      CANNOT VERIFY
    </span>
  )
}

export function LedgerRowDetail({ entry }: { entry: LedgerRow }) {
  const [result, setResult] = useState<Rederivation | null>(null)
  const [showPreimage, setShowPreimage] = useState(false)

  // The preimage is the dependency, not a list of fields.
  //
  // Listing fields by hand missed `payload`, so a row whose payload had been
  // tampered with kept showing the previous RE-DERIVED tick beside its new
  // contents — the panel vouching for an entry it had never actually checked.
  // The preimage is derived from every hashed field, so it cannot fall out of
  // step with what is on screen.
  const preimage = useMemo(() => preimageFor(entry), [entry])

  useEffect(() => {
    let cancelled = false
    setResult(null)
    void rederive(entry).then((r) => {
      if (!cancelled) setResult(r)
    })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preimage, entry.hash])

  return (
    <div className="border-b border-ink-700 bg-ink-950/60 px-3 py-2">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4">
        {/* ── Payload ── */}
        <div className="min-w-0">
          <div className="mb-1 t-meta font-semibold tracking-[0.1em] text-ink-400 uppercase">
            Payload
          </div>
          <pre className="max-h-40 overflow-auto border border-ink-700 bg-ink-900 p-2 t-meta leading-relaxed whitespace-pre text-ink-100">
            {JSON.stringify(entry.payload ?? {}, null, 2)}
          </pre>
        </div>

        {/* ── Chain evidence ── */}
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="t-meta font-semibold tracking-[0.1em] text-ink-400 uppercase">
              Hash chain
            </span>
            <VerdictBadge result={result} />
          </div>

          <div className="flex flex-col gap-1 border border-ink-700 bg-ink-900 p-2">
            <Field label="prev_hash" value={entry.prev_hash} />
            <Field
              label="stored hash"
              value={entry.hash ?? '(none recorded)'}
            />
            <Field
              label="computed"
              value={result?.computed ?? (result ? '—' : 'computing…')}
              tone={
                result?.state === 'verified'
                  ? 'good'
                  : result?.state === 'mismatch'
                    ? 'bad'
                    : 'normal'
              }
            />
          </div>

          {/* The preimage is the tallest block on the screen and the least
              read. It is proof, so it stays — behind a toggle, so the drill-down
              is a deliberate step rather than the default state. */}
          <div>
            <button
              onClick={() => setShowPreimage((v) => !v)}
              className="w-full border border-ink-700 px-1.5 py-0.5 text-left t-meta text-ink-400 hover:border-accent/50 hover:text-accent"
            >
              {showPreimage ? '▾' : '▸'} show hash input (canonical JSON, sorted keys)
            </button>
            {showPreimage ? (
              <pre className="mt-1 max-h-24 overflow-auto border border-ink-700 bg-ink-900 p-2 t-meta leading-relaxed break-all whitespace-pre-wrap text-ink-400">
                {result?.preimage ?? '…'}
              </pre>
            ) : null}
          </div>

          <p className="t-meta leading-snug text-ink-400">
            SHA-256 computed in this browser from the payload above — not read
            from the server's verify endpoint.
            {result?.state === 'unavailable' && result.reason
              ? ` Unavailable: ${result.reason}.`
              : null}
          </p>
        </div>
      </div>
    </div>
  )
}
