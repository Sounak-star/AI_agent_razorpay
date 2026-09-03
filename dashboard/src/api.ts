/*
  API types and fetch helpers.

  Plain fetch. No client cache, no query library. Every field below is returned
  by the FastAPI server verbatim — in particular elapsed_ms, the policy split,
  the unauthorised-movement count, latency and cost are all computed server-side
  in server/api/analytics.py. The client renders them; it does not derive them.
*/

// ── Sessions ──────────────────────────────────────────────────────────────────

export type SessionStatus =
  | 'active'
  | 'captured'
  | 'failed'
  | 'escalated'
  | 'refunded'
  | 'refund_pending'
  | 'refund_failed'
  | string

export interface SessionRow {
  session_id: string
  buyer_id: string
  merchant_id: string
  goal: string | null
  budget_paise: number
  status: SessionStatus
  has_pending_escalation: boolean
  event_count: number
  elapsed_ms: number | null
  /** Offer outcome for this session, or null when none was recorded. */
  offer?: 'offered' | 'accepted' | 'declined' | 'withheld' | null
  /** Where elapsed_ms came from: the closing entry, or the ledger span. */
  elapsed_source?: 'session_closed' | 'ledger_span' 
  created_at: string | null
  razorpay_payment_id: string | null
  razorpay_refund_id: string | null
}

export interface SessionsResponse {
  count: number
  sessions: SessionRow[]
}

// ── Ledger ────────────────────────────────────────────────────────────────────

export interface LedgerRow {
  seq: number
  ts: string
  session_id: string
  event_type: string
  payload: Record<string, unknown> | null
  hash: string | null
  prev_hash: string
  replayed_from_fixture: boolean
}

export interface LedgerResponse {
  total: number
  offset: number
  entries: LedgerRow[]
}

export interface VerifyResponse {
  valid: boolean
  broken_at_seq: number | null
  entries: number
}

// ── Escalations ───────────────────────────────────────────────────────────────

export interface DiffRow {
  field: string
  authorised: unknown
  proposed: unknown
  /** The two sides are not the same. */
  differs: boolean
  /** This is a field the firing rule actually examined. Independent of differs. */
  triggered: boolean
  kind: 'text' | 'paise' | 'count' | 'list' | string
  note: string | null
}

export interface EvidenceRow {
  label: string
  value: unknown
  /** True for the values that actually drove the rule. */
  flag: boolean
  kind?: string
}

/** What a history-based rule examined, recorded when it fired. */
export interface Evidence {
  kind: 'history'
  summary: string
  rows: EvidenceRow[]
}

/** The two values the firing rule actually weighed. */
export interface RuleComparison {
  left_label: string
  left_paise: number
  right_label: string
  right_paise: number
  threshold_label: string
  threshold_paise: number
}

/**
 * A real Razorpay payment opened for an approved escalation.
 *
 * Read back off the ORDER_CREATED ledger entry, so a link on screen is a link
 * the chain records — never one the client assembled.
 */
export interface EscalationPayment {
  short_url: string | null
  qr_url: string | null
  razorpay_order_id: string | null
  razorpay_payment_id?: string | null
  amount_paise: number | null
  state: 'awaiting_capture' | 'captured' | 'failed'
  detail?: string | null
  seq: number
  resolved_seq?: number
  /** ISO timestamp of ORDER_CREATED — the moment the wait began. */
  opened_at?: string
  /** How long the poller will wait, in seconds. */
  timeout_seconds?: number
}

export interface Escalation {
  id: string
  session_id: string
  /** Null until an approval opens a live payment. */
  payment: EscalationPayment | null
  goal: string | null
  reason_code: string
  /** One-line plain-English cause for the reason code, from the server. */
  cause: string
  /** True when the rule judged transaction history rather than the cart. */
  history_based: boolean
  /** Present for cart-derived rules: the exact comparison that fired. */
  comparison: RuleComparison | null
  /** Present for history-based rules; the cart diff is not the evidence there. */
  evidence: Evidence | null
  detail: string
  status: string
  resolved_by: string | null
  resolved_at: string | null
  created_at: string | null
  intent_snapshot: Record<string, unknown>
  cart_snapshot: Record<string, unknown>
  diff: DiffRow[]
}

export interface EscalationsResponse {
  escalations: Escalation[]
  /** Same escalations grouped by rule + merchant; decisions stay per session. */
  groups: EscalationGroup[]
}

/** One rule firing for one merchant, across however many sessions. */
export interface EscalationGroup {
  key: string
  reason_code: string
  merchant_id: string
  cause: string
  history_based: boolean
  comparison: RuleComparison | null
  evidence: Evidence | null
  diff: DiffRow[]
  created_at: string | null
  session_count: number
  escalations: Escalation[]
}

// ── Narrative (operator view) ─────────────────────────────────────────────────

/** One priced line of a cart, exactly as CART_BUILT recorded it. */
export interface CartLine {
  sku_id: string
  name?: string
  category?: string
  quantity: number
  unit_price_paise: number
  line_total_paise?: number
}

/** Structured data lifted from a ledger payload for a narrative line. */
export interface NarrativeDetail {
  kind: 'cart' | 'considered' | string
  items?: CartLine[]
  total_paise?: number
  returned?: string[]
  chosen?: string[]
  not_chosen?: string[]
  query?: string | null
}

export interface NarrativeLine {
  text: string
  /** The ledger entry this sentence was derived from. */
  seq: number
  tone: 'neutral' | 'good' | 'bad' | 'hold' | string
  detail?: NarrativeDetail
}

export interface NarrativeResponse {
  session_id: string
  status: string
  buyer_id: string
  goal: string | null
  lines: NarrativeLine[]
  /** Raw event enum -> operator-facing label. */
  event_labels: Record<string, string>
}

// ── Metrics ───────────────────────────────────────────────────────────────────

/** A wait excluded from engine latency, reported on its own. */
export interface WaitStat {
  mean_ms: number | null
  p95_ms: number | null
  samples: number
}

export interface Metrics {
  sessions: {
    total: number
    active: number
    captured: number
    failed: number
    error: number
    escalated: number
    refunded: number
    /** Owed a refund the provider deferred until settlement; retry still due. */
    refund_pending: number
    refund_failed: number
    stale: number
  }
  /** Counted server-side from the rule list and the attacks directory. */
  coverage?: {
    policy_rules: number
    adversarial_attacks: number
  }
  policy: {
    ALLOW: number
    DENY: number
    ESCALATE: number
    /** Sessions that reached a verdict. ALLOW + DENY + ESCALATE always equals this. */
    total: number
    /** Raw POLICY_EVALUATED entry count, kept distinct from the session count. */
    verdict_entries: number
    sessions_total: number
    sessions_without_verdict: number
  }
  reason_codes: Record<string, number>
  upsell: {
    offered: number
    accepted: number
    declined: number
    withheld_no_headroom: number
    /** null when nothing was offered — a rate with no denominator. */
    attach_rate: number | null
    /** True while acceptance is decided by seed config rather than a buyer. */
    acceptance_is_simulated: boolean
    acceptance_basis: string
  }
  unauthorised_money_movement: {
    count: number
    movements_checked: number
    policy_denials: number
    escalations_raised: number
    offending_entries: {
      seq: number
      session_id: string
      event_type: string
      amount_paise: number | null
    }[]
  }
  latency: {
    mean_ms: number | null
    p95_ms: number | null
    /** Wall clock minus the model, the provider and the human. */
    engine_mean_ms: number | null
    engine_p95_ms: number | null
    samples: number
    /** Time an escalation sat waiting on a person. */
    human_wait: WaitStat
    /** Time a payment link sat waiting to be paid. */
    provider_wait: WaitStat
  }
  cost: {
    mean_usd_micros_per_session: number | null
    total_usd_micros: number
    llm_calls: number
    unpriced_calls: number
    samples: number
    /** Real usage from LLM_CALL payloads, reported whether or not a rate is set. */
    usage: {
      llm_calls: number
      input_tokens: number
      output_tokens: number
      total_tokens: number
      sessions_with_calls: number
      mean_tokens_per_session: number | null
      models: string[]
      primary_model: string | null
      provider: string | null
    }
  }
  ledger: { total_events: number; replayed_entries: number }
  escalations: { pending: number; resolved: number }
}

export interface Health {
  status: string
  razorpay_key_prefix: string
  stub_mode: boolean
  /** synthetic | replay | live — known before a click, for the popup pre-open. */
  payments_mode?: string
  tamper_enabled: boolean
  llm?: {
    configured: boolean
    provider?: string
    model?: string
    base_url?: string
    timeout_seconds?: number
    reason?: string
  }
}

// ── Transport ─────────────────────────────────────────────────────────────────

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    // FastAPI puts the useful message in `detail`; fall back to the status.
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export const api = {
  sessions: () => request<SessionsResponse>('/sessions'),

  ledger: (limit: number, offset: number) =>
    request<LedgerResponse>(`/ledger?limit=${limit}&offset=${offset}`),

  sessionLedger: (
    sessionId: string,
    limit: number,
    offset: number,
    aroundSeq?: number | null,
  ) =>
    request<LedgerResponse>(
      `/sessions/${encodeURIComponent(sessionId)}/ledger?limit=${limit}&offset=${offset}` +
        (aroundSeq != null ? `&around_seq=${aroundSeq}` : ''),
    ),

  narrative: (sessionId: string) =>
    request<NarrativeResponse>(`/sessions/${encodeURIComponent(sessionId)}/narrative`),

  verify: () => request<VerifyResponse>('/ledger/verify'),

  escalations: () => request<EscalationsResponse>('/escalations?status=pending'),

  metrics: () => request<Metrics>('/metrics'),

  health: () => request<Health>('/health'),

  catalogCategories: () =>
    request<{ categories: string[] }>('/catalog/categories'),

  /** Signs the intent and creates the record. Runs nothing on its own. */
  createSession: (body: {
    goal: string
    budget_paise: number
    categories: string[]
  }) =>
    request<{ session_id: string; intent_jti: string }>('/sessions', {
      method: 'POST',
      body: JSON.stringify({ buyer_id: 'dashboard_operator', ...body }),
    }),

  /** Drives the session through the existing pipeline. Returns 202. */
  runSession: (sessionId: string) =>
    request<{ status: string; session_id: string }>(
      `/sessions/${encodeURIComponent(sessionId)}/run`,
      { method: 'POST' },
    ),

  decideEscalation: (
    sessionId: string,
    escalationId: string,
    decision: 'approve' | 'reject',
  ) =>
    request<{
      status: string
      escalation_id: string
      /** Present only when approval opened a live payment. */
      payment_link_url?: string
      qr_url?: string | null
      awaiting_capture?: boolean
      payment_error?: string
    }>(
      `/sessions/${encodeURIComponent(sessionId)}/escalations/${encodeURIComponent(
        escalationId,
      )}/${decision}`,
      {
        method: 'POST',
        body: JSON.stringify({ resolved_by: 'dashboard_operator' }),
      },
    ),
}
