/*
  Client-side hash re-derivation.

  The point of this file is that the browser checks the chain itself. It takes
  the payload the API returned, rebuilds the exact byte string the server
  hashed, runs SHA-256 over it, and compares the result to the stored hash. The
  server's own verify endpoint is never consulted here — if the server were
  lying about integrity, this is the code that would catch it.

  The preimage must match server/ledger/chain.py::_entry_dict_for_hashing byte
  for byte:

      json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

  over {seq, ts, session_id, event_type, payload, prev_hash,
  replayed_from_fixture}.

  Known limit, stated plainly: JSON cannot distinguish Python's 1.0 from 1, so
  a float with an integral value in a payload would serialise as "1.0"
  server-side and "1" here, and this check would report a mismatch that is
  really a serialisation difference. Every amount in this system is an integer
  number of paise and no payload currently contains a float, so the check is
  exact in practice. If a mismatch ever appears, the panel shows both hashes and
  the full preimage so the cause can be identified rather than guessed at.
*/

export type VerifyState = 'verified' | 'mismatch' | 'unavailable' | 'pending'

export interface Rederivation {
  state: VerifyState
  /** The exact string that was hashed. Shown so a viewer can audit the input. */
  preimage: string
  /** SHA-256 of the preimage, or null when it could not be computed. */
  computed: string | null
  /** The hash the server stored on the entry. */
  stored: string | null
  /** Why the check could not run, when state is 'unavailable'. */
  reason?: string
}

/** Sort by Unicode code point, matching Python's sort_keys. */
function byCodePoint(a: string, b: string): number {
  const ac = [...a]
  const bc = [...b]
  const n = Math.min(ac.length, bc.length)
  for (let i = 0; i < n; i++) {
    const d = ac[i]!.codePointAt(0)! - bc[i]!.codePointAt(0)!
    if (d !== 0) return d
  }
  return ac.length - bc.length
}

/**
 * Serialise a value the way Python's json.dumps does with sorted keys, no
 * whitespace and ensure_ascii=False.
 *
 * JSON.stringify already matches Python's string escaping (same control-char
 * escapes, same quote/backslash handling, no escaping of non-ASCII), so the
 * only work here is ordering keys and dropping separators' spaces.
 */
export function canonicalJson(value: unknown): string {
  if (value === null || value === undefined) return 'null'

  const t = typeof value
  if (t === 'boolean') return value ? 'true' : 'false'
  if (t === 'number') return JSON.stringify(value)
  if (t === 'string') return JSON.stringify(value)

  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`
  }

  if (t === 'object') {
    const obj = value as Record<string, unknown>
    const keys = Object.keys(obj).sort(byCodePoint)
    const parts = keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`)
    return `{${parts.join(',')}}`
  }

  return 'null'
}

export interface HashableEntry {
  seq: number
  ts: string
  session_id: string
  event_type: string
  payload: Record<string, unknown> | null
  prev_hash: string
  hash: string | null
  replayed_from_fixture: boolean
}

/** Rebuild the exact preimage the server hashed for this entry. */
export function preimageFor(entry: HashableEntry): string {
  return canonicalJson({
    seq: entry.seq,
    ts: entry.ts,
    session_id: entry.session_id,
    event_type: entry.event_type,
    payload: entry.payload ?? {},
    prev_hash: entry.prev_hash,
    replayed_from_fixture: entry.replayed_from_fixture,
  })
}

async function sha256Hex(input: string): Promise<string> {
  const bytes = new TextEncoder().encode(input)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * Re-derive one entry's hash in the browser and compare it to the stored value.
 *
 * crypto.subtle only exists in a secure context. Served from localhost it is
 * available; served over plain http from a LAN address for a projector it is
 * not. In that case this reports 'unavailable' with the reason, rather than a
 * mismatch — claiming the chain is broken because the browser withheld an API
 * would be worse than saying nothing.
 */
export async function rederive(entry: HashableEntry): Promise<Rederivation> {
  const preimage = preimageFor(entry)

  if (typeof crypto === 'undefined' || !crypto.subtle) {
    return {
      state: 'unavailable',
      preimage,
      computed: null,
      stored: entry.hash,
      reason: 'SHA-256 needs a secure context (https or localhost)',
    }
  }

  try {
    const computed = await sha256Hex(preimage)
    return {
      state: computed === entry.hash ? 'verified' : 'mismatch',
      preimage,
      computed,
      stored: entry.hash,
    }
  } catch (err) {
    return {
      state: 'unavailable',
      preimage,
      computed: null,
      stored: entry.hash,
      reason: err instanceof Error ? err.message : String(err),
    }
  }
}
