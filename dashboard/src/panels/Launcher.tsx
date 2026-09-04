import { useEffect, useState } from 'react'
import { api } from '../api'

/*
  Start a session from the dashboard.

  Two calls, not one. POST /sessions signs the intent and creates the record —
  it runs nothing, and a session left there has a single INTENT_SIGNED entry
  until the reconciler marks it stale. POST /sessions/{id}/run drives the same
  pipeline demo/run.py drives. Both are needed; only the pair produces a
  session with a story.

  Budget is entered in rupees because that is what a person types, and converted
  once, here, at the boundary. Everything below this line is paise.
*/

const RUPEES_TO_PAISE = 100

export function Launcher({
  onLaunched,
  disabled,
}: {
  onLaunched: (sessionId: string) => void
  disabled?: boolean
}) {
  const [goal, setGoal] = useState('')
  const [budget, setBudget] = useState('')
  const [category, setCategory] = useState('')
  const [categories, setCategories] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // From the catalogue, so the dropdown cannot offer a category no SKU is in.
  useEffect(() => {
    let cancelled = false
    void api
      .catalogCategories()
      .then((r) => {
        if (!cancelled) setCategories(r.categories)
      })
      .catch(() => {
        // A failed lookup leaves the dropdown empty rather than guessing a
        // list; the field is optional, so the launcher still works.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const rupees = Number(budget)
  const budgetValid = budget.trim() !== '' && Number.isFinite(rupees) && rupees > 0
  const canRun = !busy && !disabled && goal.trim().length > 0 && budgetValid

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!canRun) return

    setBusy(true)
    setError(null)
    try {
      const created = await api.createSession({
        goal: goal.trim(),
        budget_paise: Math.round(rupees * RUPEES_TO_PAISE),
        categories: category ? [category] : categories.length ? categories : ['grocery'],
      })

      // Selected before the run starts, so the narrative fills in as the
      // pipeline writes rather than appearing all at once at the end.
      onLaunched(created.session_id)
      await api.runSession(created.session_id)

      setGoal('')
      setBudget('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="flex shrink-0 flex-col gap-1.5 border-b border-ink-700 bg-ink-950/40 px-3 py-2"
    >
      <input
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        placeholder="what do you want to buy?"
        disabled={busy || disabled}
        className="t-transition t-focus w-full border border-ink-700 bg-ink-900 px-2 py-1 t-meta text-ink-100 placeholder:text-ink-400 focus:border-accent disabled:opacity-50"
      />

      <div className="flex gap-2">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute top-1/2 left-1.5 -translate-y-1/2 t-meta text-ink-400">
            ₹
          </span>
          <input
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            inputMode="numeric"
            placeholder="budget"
            disabled={busy || disabled}
            className="t-transition t-focus tabular w-full border border-ink-700 bg-ink-900 py-1 pr-2 pl-5 t-meta text-ink-100 placeholder:text-ink-400 focus:border-accent disabled:opacity-50"
          />
        </div>

        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          disabled={busy || disabled || categories.length === 0}
          title="Optional. Left blank, the intent authorises every category in the catalogue."
          className="t-transition t-focus min-w-0 flex-1 border border-ink-700 bg-ink-900 px-1 py-1 t-meta text-ink-300 focus:border-accent disabled:opacity-50"
        >
          <option value="">any category</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>

        <button
          type="submit"
          disabled={!canRun}
          className="t-transition t-focus shrink-0 border border-accent/60 px-2 py-1 t-meta font-semibold tracking-[0.1em] text-accent enabled:hover:bg-accent/15 disabled:border-ink-700 disabled:text-ink-400"
        >
          {busy ? 'RUN…' : 'RUN'}
        </button>
      </div>

      {error ? (
        <div className="border border-state-deny/40 bg-state-deny/10 px-1 py-0.5 t-meta text-state-deny">
          {error}
        </div>
      ) : null}
    </form>
  )
}
