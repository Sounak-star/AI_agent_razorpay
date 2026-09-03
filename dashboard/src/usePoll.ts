import { useCallback, useEffect, useRef, useState } from 'react'

/*
  Polling on a fixed interval with plain useState/useEffect.

  Behaviour that matters for a live demo:
    - The previous value is kept while a refetch is in flight, so panels never
      blank out between ticks.
    - A failed poll sets `error` but does NOT clear the last good data. If the
      server hiccups mid-demo the screen holds its last known state instead of
      going empty, and the header shows the link is down.
    - Responses that arrive after the dependencies changed are dropped, so a
      slow request for the previous selection can't overwrite the current one.
*/

export interface PollState<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** Force an immediate refetch — used after a mutation. */
  refresh: () => void
}

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  // Held in a ref so changing the fetcher identity every render doesn't
  // restart the interval.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      try {
        const next = await fetcherRef.current()
        if (cancelled) return
        setData(next)
        setError(null)
      } catch (err) {
        if (cancelled) return
        // Deliberately leave `data` alone — see the note above.
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    setLoading(true)
    void tick()
    const id = window.setInterval(tick, intervalMs)

    return () => {
      cancelled = true
      window.clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, nonce, ...deps])

  return { data, error, loading, refresh }
}
