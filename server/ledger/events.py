"""
Ledger event types — the full vocabulary of state transitions.

All 15 required events from the spec are present.
PAYMENT_SIMULATED / REFUND_SIMULATED are used exclusively by the eval harness
(Option B path) with replayed_from_fixture=True in the ledger row.
"""

from enum import Enum


class EventType(str, Enum):
    # ── Mandate lifecycle ─────────────────────────────────────────────────────
    INTENT_SIGNED = "INTENT_SIGNED"
    CART_SIGNED = "CART_SIGNED"

    # ── Merchant surface ──────────────────────────────────────────────────────
    CATALOG_QUERIED = "CATALOG_QUERIED"
    QUOTE_ISSUED = "QUOTE_ISSUED"

    # ── Buyer side ────────────────────────────────────────────────────────────
    CART_BUILT = "CART_BUILT"

    # ── Policy ────────────────────────────────────────────────────────────────
    POLICY_EVALUATED = "POLICY_EVALUATED"
    ESCALATED = "ESCALATED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"

    # ── Payment lifecycle (real Razorpay calls) ───────────────────────────────
    ORDER_CREATED = "ORDER_CREATED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"   # from real webhook
    FULFILMENT_FAILED = "FULFILMENT_FAILED"
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUND_CONFIRMED = "REFUND_CONFIRMED"

    # A real refund was attempted against the provider and rejected. Recorded
    # with the verbatim response so the failure is inspectable rather than
    # inferred, and never replaced by a synthetic refund — a compensation that
    # did not happen must not appear to have happened.
    REFUND_FAILED = "REFUND_FAILED"

    # A refund the provider refused *for now*: the payment is captured but not
    # yet settled, so there is no balance to refund from. Kept distinct from
    # REFUND_FAILED because the two demand opposite responses — this one is
    # retried after settlement, a REFUND_FAILED is not. Collapsing them would
    # either strand a buyer who is owed money or bury a real fault in a retry
    # loop. The session stays unresolved until the retry succeeds.
    REFUND_PENDING_SETTLEMENT = "REFUND_PENDING_SETTLEMENT"
    # A retry of a pending-settlement refund was attempted.
    REFUND_RETRY_SCHEDULED = "REFUND_RETRY_SCHEDULED"

    # ── Payment lifecycle (eval harness, replayed_from_fixture=True) ──────────
    # These never appear in a live run; harness uses them instead of calling
    # the real Razorpay API with fabricated IDs.
    PAYMENT_SIMULATED = "PAYMENT_SIMULATED"
    REFUND_SIMULATED = "REFUND_SIMULATED"

    # ── Upsell ────────────────────────────────────────────────────────────────
    UPSELL_PROPOSED = "UPSELL_PROPOSED"
    UPSELL_ACCEPTED = "UPSELL_ACCEPTED"
    UPSELL_REJECTED = "UPSELL_REJECTED"

    # ── Agent instrumentation ─────────────────────────────────────────────────
    # One row per LLM call: model, token usage, priced cost, wall-clock latency.
    # The dashboard's cost/latency numbers are sums over these rows — nothing
    # is estimated client-side.
    LLM_CALL = "LLM_CALL"

    # The agent finished without producing a usable cart. Recorded as its own
    # event rather than folded into a generic failure: "the model returned
    # nothing we could buy" is a different fault from "the payment failed", and
    # only one of them is the agent's.
    NO_CART_BUILT = "NO_CART_BUILT"

    # A model call exceeded its deadline and the session continued without it.
    # Recorded rather than swallowed: the offer that was not made is a fact
    # about this session, and a silent timeout is indistinguishable from a
    # model that simply had nothing to suggest.
    LLM_TIMEOUT = "LLM_TIMEOUT"

    # The provider refused the call outright because the account is over its
    # rate limit. Distinct from a timeout: nothing was computed, the request
    # never ran, and the fix is to wait or to send less — not to try harder.
    # Recorded rather than retried, because a silent retry against a token
    # ceiling turns one refusal into several and hides the real constraint.
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"

    # ── Terminal ──────────────────────────────────────────────────────────────
    SESSION_CLOSED = "SESSION_CLOSED"

    # Written by the reconciler when a session stops making progress before
    # reaching any terminal state. A hung session must never keep presenting
    # itself as live, so the stall is recorded as a fact like anything else.
    SESSION_STALE = "SESSION_STALE"
