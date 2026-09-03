/*
  Display formatting only.

  These functions change how a number is written, never what it is. Unit
  conversions here (paise -> rupees, micro-USD -> USD, ms -> s) are exact
  restatements of a value the API returned, and every one of them keeps the raw
  figure available in a title attribute so it can be checked on the spot.

  Nothing in this file computes a statistic. If a number is not on an API
  response, it does not get rendered.
*/

const DASH = '—'

/** Truncate a 64-char sha256 to the leading 8. Full value goes in `title`. */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return DASH
  return hash.slice(0, 8)
}

/** Paise (integer) -> rupees, always 2dp with thousands separators. */
export function formatPaise(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return DASH
  return `₹${(paise / 100).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

/** Milliseconds -> the coarsest unit that still reads precisely. */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return DASH
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.floor((ms % 60_000) / 1000)
  return `${minutes}m${String(seconds).padStart(2, '0')}s`
}

/**
 * Elapsed time for the session rail.
 *
 * Sub-second durations are shown in milliseconds. A settled session here
 * typically completes in tens of milliseconds, and formatting everything as
 * mm:ss rounded all of them to "00:00" — the rail reported that nothing had
 * taken any time at all.
 */
export function formatElapsed(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return DASH
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`

  const total = Math.floor(ms / 1000)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const mm = String(minutes).padStart(2, '0')
  const ss = String(seconds).padStart(2, '0')
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`
}

/**
 * Micro-USD -> dollars. Model calls cost fractions of a cent, so this keeps
 * four decimal places rather than rounding a real cost down to $0.00.
 */
export function formatUsdMicros(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return DASH
  return `$${(micros / 1_000_000).toFixed(4)}`
}

/** ISO-8601 -> HH:MM:SS.mmm in local time. Date lives in the title attribute. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms = String(d.getMilliseconds()).padStart(3, '0')
  return `${hh}:${mm}:${ss}.${ms}`
}

/** First 8 chars of a UUID — enough to tell sessions apart on screen. */
export function shortId(id: string | null | undefined): string {
  if (!id) return DASH
  return id.slice(0, 8)
}

/**
 * The reason code carried by an entry, if it has one.
 *
 * This reads a field off the payload the API returned — POLICY_EVALUATED stores
 * it as `code`, escalation events as `reason_code`. It is a lookup, not a
 * derivation: no code is inferred for entries that don't carry one.
 */
export function reasonCodeOf(payload: Record<string, unknown> | null): string | null {
  if (!payload) return null
  const code = payload.code ?? payload.reason_code
  return typeof code === 'string' ? code : null
}

/** The policy decision on a POLICY_EVALUATED entry, if present. */
export function decisionOf(payload: Record<string, unknown> | null): string | null {
  if (!payload) return null
  const decision = payload.decision
  return typeof decision === 'string' ? decision : null
}

export { DASH }
