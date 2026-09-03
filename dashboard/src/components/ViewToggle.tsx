export type View = 'operator' | 'forensic'

/*
  OPERATOR | FORENSIC.

  Two readings of the same recorded facts, never two sources of truth. Operator
  is the default because most people looking at this screen want to know what
  happened; forensic is unchanged from what it always was, and every operator
  line links into it.
*/
export function ViewToggle({
  view,
  onChange,
}: {
  view: View
  onChange: (view: View) => void
}) {
  return (
    <div className="flex shrink-0 items-stretch border border-ink-600">
      {(['operator', 'forensic'] as const).map((v) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          title={
            v === 'operator'
              ? 'Plain-English account of the selected session'
              : 'Raw hash-chained ledger with per-entry hash re-derivation'
          }
          className={`px-2 py-[1px] text-[10px] font-semibold tracking-[0.12em] transition-colors ${
            view === v
              ? 'bg-accent text-ink-950'
              : 'text-ink-400 hover:text-ink-100'
          }`}
        >
          {v.toUpperCase()}
        </button>
      ))}
    </div>
  )
}
