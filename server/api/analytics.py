"""
Dashboard analytics — pure functions over ledger rows and session records.

Design rule for Phase 9: every number the dashboard renders is computed here,
server-side, from recorded facts. The client does no arithmetic. That means a
figure on the projector can always be traced back to a specific set of ledger
entries, which is the whole point of the rail.

Where there is no data to compute an average from, these functions return a
sample count of zero and a value of None rather than a plausible-looking
number. "No samples" is a true statement; a fabricated mean is not.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from server.config import settings
from server.db.models import LedgerEntry, SessionRecord
from server.ledger.events import EventType


# Events that represent value leaving the buyer's account. Refunds are excluded
# deliberately — a refund returns money, so an unauthorised refund is a
# different (and less severe) failure than an unauthorised charge.
MONEY_MOVEMENT_EVENTS = frozenset({
    EventType.ORDER_CREATED.value,
    EventType.PAYMENT_CAPTURED.value,
    EventType.PAYMENT_SIMULATED.value,
})

# A money movement is authorised if one of these appeared earlier in the same
# session (an ALLOW verdict is handled separately, on the payload).
AUTHORISING_EVENTS = frozenset({
    EventType.HUMAN_APPROVED.value,
})

# A session whose clock has stopped. "stale" belongs here — a hung session must
# not keep ticking upward in the rail as though it were still working.
#
# "refund_pending" is deliberately absent: a refund the provider deferred until
# settlement is still owed and still due a retry, so its clock has not stopped.
# "refund_failed" is present — that one is over.
TERMINAL_SESSION_STATUSES = frozenset(
    {"captured", "refunded", "failed", "refund_failed", "error", "rejected",
     "stale", "no_cart", "rate_limited"}
)

# A session that actually reached an outcome. "stale" is excluded: it never
# finished, so folding it into a mean duration would misreport a hang as a
# completion time.
SETTLED_SESSION_STATUSES = frozenset(
    # rate_limited is excluded on purpose: the session never reached a verdict,
    # so folding it into settled outcomes would report a provider quota as a
    # decision this system made.
    {"captured", "refunded", "failed", "refund_failed", "error", "rejected"}
)


# ── Timestamps ────────────────────────────────────────────────────────────────

def parse_ts(ts: str | None) -> datetime | None:
    """Parse a ledger ISO-8601 timestamp into an aware UTC datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SessionRecord timestamps are naive UTC (server_default=now())."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# ── Per-session spans ─────────────────────────────────────────────────────────

def session_spans(entries: list[LedgerEntry]) -> dict[str, tuple[datetime, datetime]]:
    """Map session_id -> (first_event_ts, last_event_ts) over the given entries."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    for e in entries:
        ts = parse_ts(e.ts)
        if ts is None:
            continue
        existing = spans.get(e.session_id)
        if existing is None:
            spans[e.session_id] = (ts, ts)
        else:
            first, last = existing
            spans[e.session_id] = (min(first, ts), max(last, ts))
    return spans


def elapsed_ms_for(
    session: SessionRecord,
    span: tuple[datetime, datetime] | None,
    now: datetime,
) -> int | None:
    """
    Elapsed time for one session.

    Terminal sessions report the settled duration (first event -> last event).
    Live sessions report a running clock (first event -> now), so the left rail
    ticks upward on each poll without the client doing any clock arithmetic.
    """
    start = span[0] if span else _as_utc(session.created_at)
    if start is None:
        return None
    end = span[1] if (span and session.status in TERMINAL_SESSION_STATUSES) else now
    return max(0, int((end - start).total_seconds() * 1000))


# ── Policy decision split ─────────────────────────────────────────────────────

def policy_split(entries: list[LedgerEntry], total_sessions: int | None = None) -> dict:
    """
    ALLOW / DENY / ESCALATE counted per session, not per ledger entry.

    A session that escalates, gets approved and is re-evaluated produces two
    POLICY_EVALUATED entries but is one session with one outcome, so the split
    keys on each session's *last* verdict. Counting entries instead made the
    strip self-contradictory: it could report more verdicts than sessions, with
    no label saying which population either number described.

    `total` is therefore "sessions that reached a verdict", and the returned
    figures always satisfy:

        ALLOW + DENY + ESCALATE == total <= sessions_total

    `verdict_entries` keeps the raw entry count available for anyone who wants
    it, clearly named so it cannot be mistaken for the session count.
    """
    last_by_session: dict[str, str] = {}
    verdict_entries = 0

    for e in entries:
        if e.event_type != EventType.POLICY_EVALUATED.value:
            continue
        decision = (e.payload or {}).get("decision")
        if decision not in ("ALLOW", "DENY", "ESCALATE"):
            continue
        verdict_entries += 1
        last_by_session[e.session_id] = decision   # entries arrive seq-ordered

    counts = {"ALLOW": 0, "DENY": 0, "ESCALATE": 0}
    for decision in last_by_session.values():
        counts[decision] += 1

    counts["total"] = len(last_by_session)
    counts["verdict_entries"] = verdict_entries
    counts["sessions_total"] = total_sessions if total_sessions is not None else 0
    counts["sessions_without_verdict"] = max(
        0, (total_sessions or 0) - len(last_by_session)
    )
    return counts


def reason_code_split(entries: list[LedgerEntry]) -> dict[str, int]:
    """Count each reason code seen on a non-ALLOW verdict."""
    counts: dict[str, int] = {}
    for e in entries:
        if e.event_type != EventType.POLICY_EVALUATED.value:
            continue
        payload = e.payload or {}
        if payload.get("decision") == "ALLOW":
            continue
        code = payload.get("code")
        if code:
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ── The headline number ───────────────────────────────────────────────────────

def unauthorised_money_movements(entries: list[LedgerEntry]) -> dict:
    """
    Count money-movement events that were not preceded, in the same session, by
    an ALLOW verdict or an explicit human approval.

    This is the claim the whole system exists to make, so it is computed by
    walking the chain rather than by trusting a session status field. It should
    read 0. If it ever doesn't, the offending entries are named so the failure
    can be inspected instead of argued about.

    `entries` must be ordered by seq: authorisation has to precede the movement
    it authorises, and a single forward pass is what enforces that.
    """
    authorised_sessions: set[str] = set()
    offenders: list[dict] = []

    for e in entries:
        payload = e.payload or {}

        if e.event_type == EventType.POLICY_EVALUATED.value:
            if payload.get("decision") == "ALLOW":
                authorised_sessions.add(e.session_id)
            continue

        if e.event_type in AUTHORISING_EVENTS:
            authorised_sessions.add(e.session_id)
            continue

        if e.event_type in MONEY_MOVEMENT_EVENTS:
            if e.session_id not in authorised_sessions:
                offenders.append({
                    "seq": e.seq,
                    "session_id": e.session_id,
                    "event_type": e.event_type,
                    "amount_paise": payload.get("amount_paise"),
                })

    # The headline tile's subtext names what the guarantee was tested against:
    # movements that had to be authorised, plus the interventions that stopped
    # or held the ones that weren't.
    return {
        "count": len(offenders),
        "movements_checked": sum(
            1 for e in entries if e.event_type in MONEY_MOVEMENT_EVENTS
        ),
        "policy_denials": sum(
            1
            for e in entries
            if e.event_type == EventType.POLICY_EVALUATED.value
            and (e.payload or {}).get("decision") == "DENY"
        ),
        "escalations_raised": sum(
            1 for e in entries if e.event_type == EventType.ESCALATED.value
        ),
        "offending_entries": offenders[:20],
    }


# ── Latency and cost ──────────────────────────────────────────────────────────

from server.ledger.chain import PROVENANCE_BEARING_EVENTS  # noqa: F401


def _wait_intervals(session_entries: list[LedgerEntry]) -> tuple[float, float]:
    """
    Time this session spent waiting on something that is not the engine.

    Returns (human_wait_ms, provider_wait_ms).

    Both are measured between the ledger entries that bracket the wait, because
    that is the only record of when it started and stopped. A session parked on
    an escalation overnight has not been computing overnight, and a payment link
    polled for five minutes was not five minutes of policy evaluation.
    """
    by_type: dict[str, LedgerEntry] = {}
    for e in session_entries:
        by_type.setdefault(e.event_type, e)

    def ts(entry: LedgerEntry | None):
        if entry is None or entry.ts is None:
            return None
        raw = entry.ts
        if isinstance(raw, str):
            try:
                raw = datetime.fromisoformat(raw)
            except ValueError:
                return None
        return raw

    def gap(a: LedgerEntry | None, b: LedgerEntry | None) -> float:
        t0, t1 = ts(a), ts(b)
        if t0 is None or t1 is None:
            return 0.0
        return max(0.0, (t1 - t0).total_seconds() * 1000)

    # Held for a human: from the escalation being raised to it being decided.
    human = gap(
        by_type.get(EventType.ESCALATED.value),
        by_type.get(EventType.HUMAN_APPROVED.value)
        or by_type.get(EventType.HUMAN_REJECTED.value),
    )

    # Waiting on the provider: from the order being opened to the capture
    # landing. Only counted when the order was actually left open awaiting one —
    # a settlement recorded in the same breath as its order waited on nothing.
    provider = 0.0
    order = by_type.get(EventType.ORDER_CREATED.value)
    if order is not None and (order.payload or {}).get("awaiting_capture"):
        provider = gap(order, by_type.get(EventType.PAYMENT_CAPTURED.value))
    elif order is not None:
        # The confirmer recorded exactly how long it waited; prefer that.
        captured = by_type.get(EventType.PAYMENT_CAPTURED.value)
        waited = (captured.payload or {}).get("waited_seconds") if captured else None
        if isinstance(waited, (int, float)):
            provider = float(waited) * 1000

    return human, provider


def latency_stats(sessions: list[SessionRecord], entries: list[LedgerEntry]) -> dict:
    """
    How long sessions take, split by who was actually spending the time.

    `mean_ms` is wall clock, first to last ledger event. `engine_mean_ms` is what
    this system did: wall clock minus the model, minus the provider, minus the
    human. Those three waits are reported on their own rather than folded in.

    Keeping them together made the headline number meaningless. Approving two
    escalations by hand pushed the reported engine mean from 245ms to 34.4s —
    not because anything got slower, but because a person took six minutes to
    click a button and that was being counted as compute.
    """
    spans = session_spans(entries)

    by_session: dict[str, list[LedgerEntry]] = {}
    for e in entries:
        by_session.setdefault(e.session_id, []).append(e)

    llm_latencies: dict[str, int] = {}
    for e in entries:
        if e.event_type == EventType.LLM_CALL.value:
            ms = (e.payload or {}).get("latency_ms", 0)
            llm_latencies[e.session_id] = llm_latencies.get(e.session_id, 0) + ms

    durations: list[float] = []
    engine_durations: list[float] = []
    human_waits: list[float] = []
    provider_waits: list[float] = []

    for s in sessions:
        if s.status not in SETTLED_SESSION_STATUSES:
            continue
        span = spans.get(s.id)
        if not span:
            continue

        total_ms = (span[1] - span[0]).total_seconds() * 1000
        human_ms, provider_ms = _wait_intervals(by_session.get(s.id, []))
        llm_ms = llm_latencies.get(s.id, 0)

        durations.append(total_ms)
        engine_durations.append(max(0.0, total_ms - llm_ms - human_ms - provider_ms))
        if human_ms > 0:
            human_waits.append(human_ms)
        if provider_ms > 0:
            provider_waits.append(provider_ms)

    def summarise(values: list[float]) -> dict:
        if not values:
            return {"mean_ms": None, "p95_ms": None, "samples": 0}
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {
            "mean_ms": round(sum(values) / len(values)),
            "p95_ms": round(ordered[idx]),
            "samples": len(values),
        }

    if not durations:
        return {
            "mean_ms": None, "p95_ms": None,
            "engine_mean_ms": None, "engine_p95_ms": None, "samples": 0,
            "human_wait": summarise([]), "provider_wait": summarise([]),
        }

    total = summarise(durations)
    engine = summarise(engine_durations)
    return {
        "mean_ms": total["mean_ms"],
        "p95_ms": total["p95_ms"],
        "engine_mean_ms": engine["mean_ms"],
        "engine_p95_ms": engine["p95_ms"],
        "samples": len(durations),
        # Reported, not hidden: excluding a wait from the engine number is only
        # honest if the wait is still visible somewhere.
        "human_wait": summarise(human_waits),
        "provider_wait": summarise(provider_waits),
    }


def cost_stats(entries: list[LedgerEntry]) -> dict:
    """
    Mean LLM cost per session, in micro-USD, summed from LLM_CALL entries.

    Stub and harness runs make no model calls and so record no LLM_CALL rows.
    In that case this reports zero samples and a null mean, and the dashboard
    renders a dash. It does not invent a per-session cost.
    """
    per_session: dict[str, int] = {}
    tokens_per_session: dict[str, int] = {}
    total_micros = 0
    calls = 0
    unpriced_calls = 0
    input_tokens = 0
    output_tokens = 0
    models: dict[str, int] = {}

    for e in entries:
        if e.event_type != EventType.LLM_CALL.value:
            continue
        payload = e.payload or {}
        calls += 1

        # Real usage, as the provider reported it. Recorded whether or not a
        # rate is configured, so "how much did the agent think" is answerable
        # even when "how much did it cost" is not.
        tin = int(payload.get("input_tokens") or 0)
        tout = int(payload.get("output_tokens") or 0)
        input_tokens += tin
        output_tokens += tout
        tokens_per_session[e.session_id] = (
            tokens_per_session.get(e.session_id, 0) + tin + tout
        )
        model = payload.get("model")
        if model:
            models[model] = models.get(model, 0) + 1

        cost = payload.get("cost_usd_micros")
        if cost is None:
            unpriced_calls += 1
            continue
        total_micros += int(cost)
        per_session[e.session_id] = per_session.get(e.session_id, 0) + int(cost)

    # Usage is reported separately from cost. A model that ran but has no
    # published rate configured produced real tokens; saying "no model calls"
    # because the price is unknown conflates two different unknowns.
    usage = {
        "llm_calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "sessions_with_calls": len(tokens_per_session),
        "mean_tokens_per_session": (
            round(sum(tokens_per_session.values()) / len(tokens_per_session))
            if tokens_per_session else None
        ),
        "models": sorted(models, key=lambda m: -models[m]),
        "primary_model": max(models, key=models.get) if models else None,
        "provider": settings.LLM_PROVIDER or None,
    }

    if not per_session:
        return {
            "mean_usd_micros_per_session": None,
            "total_usd_micros": total_micros,
            "llm_calls": calls,
            "unpriced_calls": unpriced_calls,
            "samples": 0,
            "usage": usage,
        }

    return {
        "mean_usd_micros_per_session": round(total_micros / len(per_session)),
        "total_usd_micros": total_micros,
        "llm_calls": calls,
        "unpriced_calls": unpriced_calls,
        "samples": len(per_session),
        "usage": usage,
    }


def upsell_stats(entries: list[LedgerEntry]) -> dict:
    """
    Attach rate: offers accepted over sessions that were actually offered one.

    The denominator is sessions *offered* an upsell, not all sessions — an
    attach rate diluted by sessions that never saw an offer measures the offer
    engine's reach rather than its persuasiveness, and the two move
    independently. Withheld offers are counted separately: they are a decision
    the headroom guard made, not a customer saying no.
    """
    offered: set[str] = set()
    withheld: set[str] = set()
    accepted: set[str] = set()
    declined: set[str] = set()

    for e in entries:
        payload = e.payload or {}
        if e.event_type == EventType.UPSELL_PROPOSED.value:
            (withheld if payload.get("blocked") else offered).add(e.session_id)
        elif e.event_type == EventType.UPSELL_ACCEPTED.value:
            accepted.add(e.session_id)
        elif e.event_type == EventType.UPSELL_REJECTED.value:
            if payload.get("reason") != "exceeded_remaining_headroom":
                declined.add(e.session_id)

    # Whether an offer is accepted is currently decided by a flag on the
    # scenario, not by a buyer or a model. The number is therefore a property
    # of the seed file, not a measurement of anything, and it is labelled that
    # way everywhere it appears. An unqualified attach rate would read as a
    # growth result, which is the one thing it is not.
    simulated = any(
        (e.payload or {}).get("decided_by") == "buyer"
        for e in entries
        if e.event_type in (
            EventType.UPSELL_ACCEPTED.value, EventType.UPSELL_REJECTED.value
        )
    )

    return {
        "offered": len(offered),
        "accepted": len(accepted),
        "declined": len(declined),
        "withheld_no_headroom": len(withheld),
        # None rather than 0 when nothing was offered: a rate with no
        # denominator is not zero, it is undefined.
        "attach_rate": (len(accepted) / len(offered)) if offered else None,
        "acceptance_is_simulated": simulated,
        "acceptance_basis": (
            "seed configuration (_accept_upsell); no buyer or model decides this"
            if simulated else "none recorded"
        ),
    }


# ── Escalation diff ───────────────────────────────────────────────────────────

def _cart_categories(cart: dict) -> list[str]:
    seen: list[str] = []
    for item in cart.get("items", []) or []:
        category = item.get("category")
        if category and category not in seen:
            seen.append(category)
    return seen


# What each rule actually examined, and how to say it in one line.
#
# The card leads with the trigger, so the reason code has to carry both the
# plain-English cause and the specific fields the rule looked at. Without this
# the operator sees a set of differing fields and has to guess which one made
# the engine stop — and a field can differ without being the reason.
REASON_CODE_META: dict[str, dict] = {
    "FIRST_CONTACT_BUYER": {
        "cause": "this buyer has never settled a transaction with this merchant",
        "fields": ["merchant"],
    },
    # Retained so ledger entries written before the rename still render with a
    # cause rather than falling back to the generic text.
    "NEW_MERCHANT": {
        "cause": "this buyer has never settled a transaction with this merchant",
        "fields": ["merchant"],
    },
    "PRICE_DRIFT": {
        "cause": "cart total exceeds the buyer's estimate by more than 5%",
        "fields": ["estimate"],
    },
    "VELOCITY": {
        "cause": "too many transactions from this buyer in the last hour",
        "fields": [],          # a rate, not a field of the cart
    },
    "PER_TXN_CAP": {
        "cause": "cart total exceeds the authorised budget",
        "fields": ["total"],
    },
    "DAILY_CAP": {
        "cause": "this cart would push today's spend past the daily cap",
        "fields": ["total"],
    },
    "CATEGORY_DENY": {
        "cause": "cart contains a category the mandate does not allow",
        "fields": ["categories"],
    },
    "ITEM_COUNT": {
        "cause": "cart has more line items than the mandate permits",
        "fields": ["item_count"],
    },
    "MANDATE_INVALID": {
        "cause": "the cart mandate failed signature or replay verification",
        "fields": ["merchant"],
    },
}


def rule_comparison(reason_code: str, intent: dict, cart: dict) -> dict | None:
    """
    The two values the rule actually weighed against each other.

    A card that lists every field of the mandate invites the reader to compare
    the wrong pair. PRICE_DRIFT weighs the cart total against the buyer's
    *estimate*, but a full diff also shows total-vs-budget — and with a ₹999
    cart under a ₹5,000 budget that row reads as comfortably within limits while
    the text says 899% over. Both were true; only one was the rule's.
    """
    total = cart.get("total_paise")
    if total is None:
        return None

    if reason_code == "PRICE_DRIFT":
        estimate = intent.get("estimate_paise")
        if estimate is None:
            return None
        return {
            "left_label": "buyer's estimate",
            "left_paise": estimate,
            "right_label": "cart total",
            "right_paise": total,
            "threshold_label": "drift ceiling (estimate + 5%)",
            "threshold_paise": int(estimate * 1.05),
        }

    if reason_code == "PER_TXN_CAP":
        budget = intent.get("budget_paise")
        if budget is None:
            return None
        return {
            "left_label": "authorised budget",
            "left_paise": budget,
            "right_label": "cart total",
            "right_paise": total,
            "threshold_label": "budget ceiling",
            "threshold_paise": budget,
        }

    return None


def reason_code_cause(reason_code: str) -> str:
    """One-line plain-English cause for a reason code."""
    meta = REASON_CODE_META.get(reason_code)
    return meta["cause"] if meta else "policy engine required human review"


# Rules that judge transaction history rather than the contents of the cart.
# For these the cart diff is the wrong evidence entirely: the cart can be
# perfectly conforming and the rule still fires, so showing an all-matching
# diff invites the reviewer to conclude nothing is wrong.
HISTORY_BASED_CODES = frozenset(
    {"FIRST_CONTACT_BUYER", "NEW_MERCHANT", "VELOCITY", "DAILY_CAP"}
)


def is_history_based(reason_code: str) -> bool:
    return reason_code in HISTORY_BASED_CODES


def build_history_evidence(
    *,
    reason_code: str,
    cart_merchant_id: str,
    history: list,
    now_iso: str,
    merchant_settled_count: int | None = None,
    buyer_id: str | None = None,
) -> dict | None:
    """
    Capture what a history-based rule actually examined, at the moment it fired.

    Called from the saga while the history list the rule was handed is still in
    scope, and stored on the ESCALATED ledger entry. It is deliberately not
    recomputed when the card is rendered: the harness and seeder supply history
    that never reaches the sessions table, so a read-time recount would show a
    reviewer different numbers from the ones the engine judged.

    Returns None for cart-derived rules, which keep the diff.
    """
    if not is_history_based(reason_code):
        return None

    settled = [h for h in history if getattr(h, "settled", False)]

    if reason_code in ("FIRST_CONTACT_BUYER", "NEW_MERCHANT"):
        with_merchant = [h for h in settled if h.merchant_id == cart_merchant_id]
        seen_by_buyer = len(with_merchant)

        # Two counts, both labelled with the population they cover.
        #
        # The rule is scoped to one buyer, so it can legitimately read zero on a
        # merchant that has settled dozens of transactions for other buyers.
        # Reporting only the buyer-scoped figure, under the unscoped label
        # "prior settled txns", made that look like a contradiction against the
        # captured sessions on screen — and would equally have hidden a genuine
        # settlement failure, where the merchant-wide count is also stuck at
        # zero. Showing both makes the difference between those two situations
        # visible instead of silent.
        rows = [
            {"label": "merchant_id", "value": cart_merchant_id, "flag": True},
            {
                "label": "settled — this buyer",
                "value": seen_by_buyer,
                "flag": True,
            },
        ]

        if merchant_settled_count is not None:
            rows.append({
                "label": "settled — all buyers",
                "value": merchant_settled_count,
                # Flagged only when it is also zero: that is the case where
                # nothing has ever settled anywhere, which is a real fault
                # rather than an ordinary first-time buyer.
                "flag": merchant_settled_count == 0,
            })

        rows.append({"label": "first seen", "value": now_iso, "flag": False})
        rows.append({
            "label": "settlement history",
            "value": "none for this buyer",
            "flag": True,
        })

        if merchant_settled_count == 0:
            summary = (
                "No settlement history at all — this merchant has never settled "
                "a transaction for any buyer."
            )
        elif merchant_settled_count is not None:
            summary = (
                f"First transaction for this buyer. The merchant has settled "
                f"{merchant_settled_count} transaction"
                f"{'' if merchant_settled_count == 1 else 's'} for other buyers."
            )
        else:
            summary = "No settlement history with this merchant."

        return {"kind": "history", "summary": summary, "rows": rows}

    if reason_code == "VELOCITY":
        window = settings.VELOCITY_WINDOW_SECONDS
        cutoff = time.time() - window
        recent = [h for h in history if getattr(h, "ts", 0) >= cutoff]
        return {
            "kind": "history",
            "summary": (
                f"{len(recent)} transactions in the last {window}s "
                f"against a limit of {settings.VELOCITY_MAX_TXN}."
            ),
            "rows": [
                {"label": "txns in window", "value": len(recent), "flag": True},
                {"label": "window", "value": f"{window}s", "flag": False},
                {
                    "label": "threshold",
                    "value": settings.VELOCITY_MAX_TXN,
                    "flag": False,
                },
            ],
        }

    if reason_code == "DAILY_CAP":
        spent = sum(h.total_paise for h in settled)
        return {
            "kind": "history",
            "summary": "Today's settled spend plus this cart exceeds the daily cap.",
            "rows": [
                {"label": "settled today", "value": spent, "kind": "paise", "flag": True},
                {
                    "label": "daily cap",
                    "value": settings.DAILY_SPEND_CAP_PAISE,
                    "kind": "paise",
                    "flag": False,
                },
                {"label": "settled txns", "value": len(settled), "flag": False},
            ],
        }

    return None


def escalation_diff(intent: dict, cart: dict, reason_code: str = "") -> list[dict]:
    """
    Build the AUTHORISED-vs-PROPOSED rows the escalation card renders.

    The server decides which fields differ so the client never has to compare a
    mandate against a cart itself. Each row carries two independent flags:

      differs    the two sides are not the same
      triggered  this is a field the firing rule actually examined

    They are genuinely different things. A cart can differ from its mandate in a
    field no rule cared about, and a rule can fire on a field whose two sides
    look identical here (VELOCITY is about a rate, not a value in the cart).
    Collapsing them would tell the operator the wrong thing about why the
    session stopped, so both are reported and the client styles them apart.
    """
    intent = intent or {}
    cart = cart or {}

    total_paise = cart.get("total_paise")
    items = cart.get("items", []) or []
    cart_categories = _cart_categories(cart)
    allowed_categories = intent.get("categories", []) or []
    out_of_scope = [c for c in cart_categories if c not in allowed_categories]

    budget = intent.get("budget_paise")
    estimate = intent.get("estimate_paise")
    # Line items, not units — see rule_item_count.
    max_items = intent.get("max_line_items", intent.get("max_items"))

    triggering_fields = set(REASON_CODE_META.get(reason_code, {}).get("fields", []))

    rows = [
        {
            "field": "merchant",
            "authorised": intent.get("aud"),
            "proposed": cart.get("merchant_id"),
            "differs": bool(intent.get("aud")) and intent.get("aud") != cart.get("merchant_id"),
            "kind": "text",
            "note": None,
        },
        {
            "field": "total",
            "authorised": budget,
            "proposed": total_paise,
            "differs": budget is not None and total_paise is not None and total_paise > budget,
            "kind": "paise",
            "note": "budget ceiling vs cart total",
        },
        {
            "field": "estimate",
            "authorised": estimate,
            "proposed": total_paise,
            # PRICE_DRIFT fires above 1.05x, so mirror that threshold exactly
            # rather than flagging any deviation at all.
            "differs": (
                estimate is not None
                and total_paise is not None
                and total_paise > estimate * 1.05
            ),
            "kind": "paise",
            "note": "expected spend vs cart total",
        },
        {
            "field": "line_items",
            "authorised": max_items,
            "proposed": len(items),
            "differs": max_items is not None and len(items) > max_items,
            "kind": "count",
            "note": None,
        },
        {
            "field": "categories",
            "authorised": allowed_categories,
            "proposed": cart_categories,
            "differs": bool(out_of_scope),
            "kind": "list",
            "note": f"out of scope: {', '.join(out_of_scope)}" if out_of_scope else None,
        },
    ]

    for row in rows:
        row["triggered"] = row["field"] in triggering_fields

    # Triggering fields first, then anything that merely differs, then the rest.
    # The operator's first question is "what stopped this", so the field the
    # rule examined has to be at the top of the table, not wherever it happens
    # to fall in a fixed field order.
    rows.sort(key=lambda r: (not r["triggered"], not r["differs"]))
    return rows


def coverage_counts() -> dict:
    """
    How many rules and adversarial cases exist, counted from the source.

    Both figures are things a reader is invited to trust, so neither is written
    down anywhere it could drift: the rules come from the list the engine
    actually iterates, and the attacks from the files the harness actually runs.
    """
    from pathlib import Path

    from server.policy.rules import ORDERED_RULES

    attacks_dir = Path(__file__).parent.parent.parent / "evals" / "attacks"
    attacks = len(list(attacks_dir.glob("*.json"))) if attacks_dir.is_dir() else 0

    return {
        # ORDERED_RULES holds the cart/history rules; mandate_invalid is applied
        # in engine.evaluate() because it needs the VerifyResult, so it is not
        # in that list and has to be counted alongside it.
        "policy_rules": len(ORDERED_RULES) + 1,
        "adversarial_attacks": attacks,
    }
