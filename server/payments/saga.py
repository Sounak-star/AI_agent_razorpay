"""
Payment saga — one implementation, for every path.

run_saga() is the single money-movement path. A live demo and a seeded or eval
run differ only in the arguments passed to it:

  payments=LIVE     create a real Razorpay payment link, wait for the webhook
  payments=REPLAY   replay recorded ids, or synthesise them and label them so
  cart_token=...    supply a client mandate, or let the saga sign one
  offer_upsell=...  make an offer, or don't

There were previously two implementations. They drifted, and the drift landed
on the control the whole system rests on: the harness path never called
verify_cart_mandate, so every eval result was produced by a path that skipped
mandate enforcement entirely — a full attack run recorded zero rows in
mandate_jtis. Anything that must hold on every path now has exactly one place
to be wrong.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from server.config import settings
from server.db.models import EscalationRequest, LedgerEntry, SessionRecord
from server.ledger.chain import append
from server.ledger.events import EventType
from server.mandate.issuer import sign_cart
from server.mandate.schema import Cart, IntentMandate
from server.mandate.verifier import verify_cart_mandate, record_intent_jti
from server.payments import fixtures
from server.payments.settlement import is_pending_settlement, settlement_state
from server.payments.confirm import CaptureResult
from server.payments.razorpay_client import (
    create_payment_link,
    create_refund,
    RazorpayError,
    StubNotImplemented,
)
from server.policy.engine import evaluate
from server.policy.rules import TxnHistoryItem

log = logging.getLogger(__name__)

_FIXTURE_PATH = Path(__file__).parent.parent.parent / "evals" / "fixtures" / "razorpay_capture.json"


class SagaError(Exception):
    """Non-retryable saga failure."""
    pass


class SagaEscalated(Exception):
    """Policy engine said ESCALATE; human approval required."""
    def __init__(self, escalation_id: str, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.escalation_id = escalation_id
        self.reason_code = reason_code
        self.detail = detail


def _session_duration_ms(db: Session, session_id: str) -> int | None:
    """
    Wall-clock time from this session's first ledger entry to now.

    Derived from the chain rather than from session timestamps so the closing
    entry's duration is measured against the same clock as every other entry in
    the trail it closes.
    """
    first = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .first()
    )
    if first is None:
        return None
    try:
        started = datetime.fromisoformat(first.ts)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


def close_session(
    db: Session,
    session: SessionRecord,
    *,
    status: str,
    reason: str,
    final_total_paise: int | None = None,
    replayed_from_fixture: bool = False,
) -> None:
    """
    Move a session to a terminal state and record SESSION_CLOSED.

    Every terminal transition goes through here so that a session can never end
    without a closing entry — the gap that left completed sessions looking
    indistinguishable from abandoned ones. Idempotent: a session that already
    carries a SESSION_CLOSED entry is not closed twice.

    Carries the final total and the session's duration so the closing entry
    answers "what did this cost and how long did it take" without a reader
    having to join it against anything else.
    """
    already_closed = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session.id,
            LedgerEntry.event_type == EventType.SESSION_CLOSED.value,
        )
        .first()
    )
    duration_ms = _session_duration_ms(db, session.id)
    session.status = status
    db.commit()

    if already_closed:
        return

    append(db, session.id, EventType.SESSION_CLOSED, {
        "final_status": status,
        "terminal_state": status,
        "reason": reason,
        "final_total_paise": final_total_paise,
        "duration_ms": duration_ms,
        "razorpay_order_id": session.razorpay_order_id,
        "razorpay_payment_id": session.razorpay_payment_id,
        "razorpay_refund_id": session.razorpay_refund_id,
    }, replayed_from_fixture=replayed_from_fixture)


def _merchant_settled_count(db: Session, merchant_id: str) -> int | None:
    """
    How many transactions this merchant has ever settled, across all buyers.

    Recorded alongside the buyer-scoped figure the rule actually read, so a
    first-time buyer at a busy merchant can be told apart from a merchant where
    nothing has ever settled. Those look identical from the rule's point of
    view and are very different problems.
    """
    try:
        return (
            db.query(SessionRecord)
            .filter(
                SessionRecord.merchant_id == merchant_id,
                SessionRecord.status.in_(["captured", "refunded"]),
            )
            .count()
        )
    except Exception as exc:                          # noqa: BLE001
        log.warning("[saga] merchant settled count failed: %s", exc)
        return None


def _escalation_evidence(
    reason_code: str, cart: Cart, history, db: Session, buyer_id: str | None = None
) -> dict | None:
    """
    Capture what a history-based rule examined, while its inputs are still here.

    Stored on the ESCALATED ledger entry rather than recomputed when the card is
    rendered. The history a rule is handed is not always reconstructable later —
    the harness and seeder pass synthetic prior transactions that never reach
    the sessions table — so a read-time recount would show the reviewer
    different numbers from the ones the engine actually judged.
    """
    from server.api.analytics import build_history_evidence

    try:
        return build_history_evidence(
            reason_code=reason_code,
            cart_merchant_id=cart.merchant_id,
            history=history,
            now_iso=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            merchant_settled_count=_merchant_settled_count(db, cart.merchant_id),
            buyer_id=buyer_id,
        )
    except Exception as exc:                          # noqa: BLE001
        # Evidence capture must never be able to fail an escalation.
        log.warning("[saga] evidence capture failed for %s: %s", reason_code, exc)
        return None


def _guard_non_empty_cart(db: Session, session: SessionRecord, cart: Cart) -> None:
    """
    Stop an empty cart before it reaches the policy engine.

    A zero-item cart has no total to weigh against a budget and no categories to
    check, so every rule would pass it vacuously and it would arrive at payment
    "approved". It is closed as NO_CART instead, with the reason recorded.
    """
    if cart.items:
        return
    close_session(
        db, session,
        status="no_cart",
        reason="no_cart: cart contained zero items; policy evaluation skipped",
        final_total_paise=0,
    )
    raise SagaError("cart contained zero items — policy evaluation skipped")


def run_upsell(
    db: Session,
    session: SessionRecord,
    intent: IntentMandate,
    cart: Cart,
    *,
    accept: bool = True,
    stub: bool | None = None,
    replayed_from_fixture: bool = False,
) -> Cart:
    """
    Offer at most one complementary item, and record what happened either way.

    Returns the cart to proceed with: the original when nothing is offered or
    the offer is declined, a new cart including the item when it is accepted.

    The headroom guard is the whole point. An offer is only made when the item
    fits inside what the buyer already authorised, so accepting one can never
    push the session past its own mandate — the upsell rides on the existing
    authorisation rather than needing a new one. When the cheapest candidate
    does not fit, that is recorded too: an offer withheld because of the budget
    is a decision the rail made, and silence would hide it.

    Never raises. An upsell failure must not be able to fail a payment.
    """
    from server.agents.upsell import UpsellTimeout, suggest_upsell
    from server.mandate.schema import CartItem
    from server.mcp.catalog import search_skus

    headroom = intent.budget_paise - cart.total_paise

    try:
        suggestion = suggest_upsell(
            cart=cart,
            intent_budget_paise=intent.budget_paise,
            intent_categories=list(intent.categories),
            stub=stub,
            session_id=session.id,
        )

        if suggestion is None:
            # Distinguish "nothing suitable existed" from "something existed but
            # cost more than the remaining headroom". Only the second is a
            # decision worth showing an operator.
            in_cart = {i.sku_id for i in cart.items}
            candidates = [
                c for c in search_skus(
                    category=intent.categories[0] if len(intent.categories) == 1 else None,
                    limit=50,
                )
                if c.get("id") not in in_cart and isinstance(c.get("price_paise"), int)
            ]
            cheapest = min(candidates, key=lambda c: c["price_paise"], default=None)
            if cheapest is not None and cheapest["price_paise"] > max(0, headroom):
                append(db, session.id, EventType.UPSELL_PROPOSED, {
                    "blocked": True,
                    "reason": "exceeded_remaining_headroom",
                    "sku_id": cheapest["id"],
                    "name": cheapest.get("name"),
                    "price_paise": cheapest["price_paise"],
                    "headroom_paise": max(0, headroom),
                }, replayed_from_fixture=replayed_from_fixture)
                append(db, session.id, EventType.UPSELL_REJECTED, {
                    "sku_id": cheapest["id"],
                    "reason": "exceeded_remaining_headroom",
                    "decided_by": "policy",
                }, replayed_from_fixture=replayed_from_fixture)
            return cart

        append(db, session.id, EventType.UPSELL_PROPOSED, {
            "blocked": False,
            "sku_id": suggestion["sku_id"],
            "name": suggestion.get("name"),
            "category": suggestion.get("category"),
            "price_paise": suggestion["price_paise"],
            "headroom_paise": suggestion.get("headroom_paise", headroom),
            "cart_total_before_paise": cart.total_paise,
            "stub": suggestion.get("stub", False),
        }, replayed_from_fixture=replayed_from_fixture)

        if not accept:
            append(db, session.id, EventType.UPSELL_REJECTED, {
                "sku_id": suggestion["sku_id"],
                "reason": "declined_by_buyer",
                "decided_by": "buyer",
            }, replayed_from_fixture=replayed_from_fixture)
            return cart

        # Accepted: rebuild the cart including the item, at catalog prices.
        new_cart = Cart(
            merchant_id=cart.merchant_id,
            items=list(cart.items) + [CartItem(
                sku_id=suggestion["sku_id"],
                name=suggestion.get("name", suggestion["sku_id"]),
                category=suggestion.get("category", "unknown"),
                quantity=1,
                unit_price_paise=suggestion["price_paise"],
            )],
        )
        append(db, session.id, EventType.UPSELL_ACCEPTED, {
            "sku_id": suggestion["sku_id"],
            "name": suggestion.get("name"),
            "price_paise": suggestion["price_paise"],
            "cart_total_before_paise": cart.total_paise,
            "cart_total_after_paise": new_cart.total_paise,
            "headroom_remaining_paise": intent.budget_paise - new_cart.total_paise,
            "decided_by": "buyer",
        }, replayed_from_fixture=replayed_from_fixture)

        # Deliberately does not sign here. The saga signs once, after this
        # step, so exactly one CART_SIGNED exists and it covers the total that
        # is actually paid. Signing in both places produced two signatures,
        # the first of them over a cart that was already superseded.
        return new_cart

    except UpsellTimeout as exc:
        # Deterministic fallback: no offer. The session continues to payment on
        # the cart the buyer already authorised, and the wait is recorded so a
        # slow model is visible rather than merely absent.
        append(db, session.id, EventType.LLM_TIMEOUT, {
            "stage": "upsell_suggest",
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
            "fallback": "no_upsell_offered",
            "detail": str(exc)[:200],
        }, replayed_from_fixture=replayed_from_fixture)
        log.warning("[saga] upsell timed out for %s; continuing without an offer",
                    session.id)
        return cart

    except Exception as exc:                          # noqa: BLE001
        log.warning("[saga] upsell step failed for %s: %s", session.id, exc)
        return cart


# ── Payment settlement, injected ──────────────────────────────────────────────
#
# The one thing that genuinely differs between a live demo and a seeded run is
# where the payment identifiers come from. Everything else — mandate signing,
# mandate verification, policy evaluation, the offer step, closing — is
# identical and must stay identical, so it lives in run_saga() and nowhere else.

class PaymentMode(str, Enum):
    LIVE = "live"            # real Razorpay order + payment link, real capture
    REPLAY = "replay"        # replay a recorded capture; badged REPLAYED
    SYNTHETIC = "synthetic"  # generated locally; badged SYNTHETIC, no network


@dataclass(frozen=True)
class Settlement:
    """What the payment step produced, however it was produced."""
    order_id: str
    payment_id: str | None
    refund_id: str | None
    short_url: str | None
    qr_url: str | None
    from_real_fixture: bool      # the ONLY thing that may produce REPLAYED
    event: EventType
    awaiting_capture: bool
    raw: dict | None = None


@dataclass(frozen=True)
class LivePayment:
    """A real order and link, created and waiting to be paid."""
    order_id: str
    payment_link_id: str
    short_url: str | None
    qr_url: str | None
    amount_paise: int


def open_live_payment(
    db: Session, session: SessionRecord, cart: Cart,
) -> LivePayment:
    """
    Create the real order and the real payment link, and stop there.

    Split from waiting on purpose. Creating the link is fast and belongs in the
    request that approved the escalation; waiting for a human to pay takes as
    long as it takes, and an HTTP handler must not be holding a connection open
    for five minutes to find out. The caller decides who does the waiting.
    """
    from server.payments.razorpay_client import create_order

    receipt = f"tollgate_{session.id[:8]}"
    try:
        order = create_order(
            amount_paise=cart.total_paise,
            receipt=receipt,
            notes={"session_id": session.id},
        )
    except (RazorpayError, StubNotImplemented) as exc:
        raise SagaError(f"order creation failed: {exc}") from exc

    try:
        link = create_payment_link(
            amount_paise=cart.total_paise,
            description=f"Tollgate order - session {session.id[:8]}",
            session_id=session.id,
        )
    except (RazorpayError, StubNotImplemented) as exc:
        raise SagaError(f"payment link creation failed: {exc}") from exc

    order_id = order.get("id", "")
    link_id = link.get("id", "")

    session.razorpay_order_id = order_id
    db.commit()

    append(db, session.id, EventType.ORDER_CREATED, {
        "razorpay_order_id": order_id,
        "razorpay_payment_link_id": link_id,
        "receipt": order.get("receipt", receipt),
        "amount_paise": cart.total_paise,
        "currency": "INR",
        # What the dashboard renders. Read back off the ledger rather than
        # stored on the session: the link is a fact about this order, and the
        # ledger is where facts about this order already live.
        "short_url": link.get("short_url"),
        "qr_url": link.get("qr_code") or link.get("qr_image_url"),
        "payments": PaymentMode.LIVE.value,
        "replayed_from_fixture": False,
        "synthetic": False,
        "live": True,
        "awaiting_capture": True,
    })

    return LivePayment(
        order_id=order_id,
        payment_link_id=link_id,
        short_url=link.get("short_url"),
        qr_url=link.get("qr_code") or link.get("qr_image_url"),
        amount_paise=cart.total_paise,
    )


def await_live_capture(
    db: Session,
    session: SessionRecord,
    opened: LivePayment,
    *,
    timeout_seconds: float | None = None,
    confirmer: object | None = None,
) -> CaptureResult:
    """
    Block until the link is paid, then record the capture.

    Records PAYMENT_CAPTURED on success and returns; closing the session
    is the caller's job, because only the caller knows whether the saga has more
    to do afterwards.

    `confirmer` is the seam. Today it is a poller because polling needs no
    inbound URL and a laptop on stage has none. A webhook receiver implements
    the same wait_for_capture and drops in here; nothing else in the saga
    changes, and nothing above it knows which one ran.
    """
    from server.payments.confirm import PollingConfirmer

    conf = confirmer or PollingConfirmer(
        interval_seconds=settings.PAYMENT_POLL_INTERVAL_SECONDS
    )
    result = conf.wait_for_capture(
        opened.payment_link_id,
        timeout_seconds=(
            timeout_seconds
            if timeout_seconds is not None
            else settings.PAYMENT_CAPTURE_TIMEOUT_SECONDS
        ),
    )

    if not result.captured:
        # No money moved, so nothing is appended to the payment lifecycle. The
        # non-capture is recorded on ORDER_CREATED's counterpart — the closing
        # entry the caller writes — rather than by inventing a payment event
        # for a payment that never happened.
        log.warning(
            "[saga] no capture for %s after %.0fs (status=%s)",
            session.id, result.waited_seconds, result.status,
        )
        return result

    session.razorpay_payment_id = result.payment_id
    db.commit()
    append(db, session.id, EventType.PAYMENT_CAPTURED, {
        "razorpay_order_id": opened.order_id,
        "razorpay_payment_id": result.payment_id,
        "razorpay_payment_link_id": opened.payment_link_id,
        "amount_paise": opened.amount_paise,
        "waited_seconds": round(result.waited_seconds, 1),
        # The provider's own capture time, not ours. Settlement is counted from
        # when the payment was captured, and our ledger records when we learned
        # of it — the two differ by however long the poll took, and by more if a
        # confirmation arrives late. Deriving a settlement date from our write
        # time would put it a day out.
        "captured_at": _provider_capture_time(result.raw),
        "payments": PaymentMode.LIVE.value,
        "replayed_from_fixture": False,
        "synthetic": False,
        "live": True,
    })
    return result


def _settle_live(
    db: Session, session: SessionRecord, cart: Cart, *, wait_seconds: float,
) -> Settlement:
    """Open the payment and wait for it, in one call, for synchronous callers."""
    opened = open_live_payment(db, session, cart)
    result = await_live_capture(
        db, session, opened, timeout_seconds=wait_seconds
    )
    if not result.captured:
        raise SagaError(
            f"payment not captured: {result.detail or result.status} "
            f"(waited {result.waited_seconds:.0f}s)"
        )

    return Settlement(
        order_id=opened.order_id,
        payment_id=result.payment_id,
        refund_id=None,
        short_url=opened.short_url,
        qr_url=opened.qr_url,
        from_real_fixture=False,
        event=EventType.PAYMENT_CAPTURED,
        awaiting_capture=False,
        raw=result.raw,
    )


def _settle_replay(session: SessionRecord, simulate_refund: bool) -> Settlement:
    """
    Replay a recorded capture.

    Only fields actually present in the fixture make this REPLAYED. When the
    fixture is missing a piece, that piece falls back to synthetic and is
    labelled so — a partial recording must not lend its provenance to the parts
    it does not cover.
    """
    from server.payments import fixtures

    order = (fixtures.get("order") or {}).get("response") or {}
    link = (fixtures.get("payment_link") or {}).get("response") or {}
    payment = (fixtures.get("payment") or {}).get("response") or {}

    order_id = order.get("id") or link.get("order_id")
    payment_id = payment.get("id")
    backed = bool(order_id and payment_id)

    # An incoherent recording falls back to synthetic rather than being replayed.
    # Every field in it may be real while the set as a whole describes a payment
    # that never happened — and REPLAYED would be asserting exactly that it did.
    problem = fixtures.coherence_problem()
    if problem:
        log.warning("[saga] ignoring incoherent fixture: %s", problem)
        return _settle_synthetic(session, simulate_refund)

    if not backed:
        return _settle_synthetic(session, simulate_refund)

    return Settlement(
        order_id=order_id,
        payment_id=payment_id,
        refund_id=None,          # the recorded refund was REJECTED; see below
        short_url=link.get("short_url"),
        qr_url=link.get("qr_code"),
        from_real_fixture=True,
        event=EventType.PAYMENT_SIMULATED,
        awaiting_capture=False,
        raw=payment,
    )


def _settle_synthetic(session: SessionRecord, simulate_refund: bool) -> Settlement:
    """Locally generated identifiers. No network, never REPLAYED."""
    return Settlement(
        order_id=f"harness_order_{session.id[:8]}",
        payment_id=f"harness_pay_{session.id[:8]}",
        refund_id=f"harness_refund_{session.id[:8]}" if simulate_refund else None,
        short_url=None,
        qr_url=None,
        from_real_fixture=False,
        event=EventType.PAYMENT_SIMULATED,
        awaiting_capture=False,
    )


@dataclass(frozen=True)
class RefundOutcome:
    """
    What came back from attempting a refund.

    Three outcomes, not two. "Accepted", "refused", and "not yet" are
    operationally different things, and the middle one is the one a boolean
    would quietly turn into the wrong answer.
    """
    accepted: bool
    pending_settlement: bool
    refund_id: str | None = None
    error_code: str | None = None
    retry_after: str | None = None

    @property
    def terminal(self) -> bool:
        """True when nothing further will change this outcome."""
        return self.accepted or not self.pending_settlement


def attempt_refund(
    db: Session,
    session: SessionRecord,
    *,
    amount_paise: int,
    reason: str = "fulfilment_failed",
    is_retry: bool = False,
) -> RefundOutcome:
    """
    Attempt a real refund and record whatever the provider says.

    A rejection is recorded with the verbatim response body, the HTTP status and
    the payment id — not swallowed, not retried silently, and never replaced by
    a synthetic refund. A compensation that did not happen must not appear on
    the trail as one that did; the buyer is still owed the money, and that is
    the fact the ledger has to preserve.

    Rejections are then split in two, because Razorpay returns the same generic
    400 for both:

      REFUND_PENDING_SETTLEMENT  the payment is captured but unsettled, so there
                                 is no balance to refund from yet. Verified
                                 against /v1/settlements, not inferred from the
                                 error text. Retryable after settlement; the
                                 session stays unresolved until it succeeds.

      REFUND_FAILED              anything else. Terminal, and surfaced as such.

    The split is only made when the settlement state is actually known. An
    unverified "it will work later" is worse than an honest unknown: it converts
    a permanent failure into a silent retry loop while the buyer waits.
    """
    payment_id = session.razorpay_payment_id
    if not payment_id:
        raise SagaError("cannot refund: no payment_id on session")

    append(db, session.id, EventType.REFUND_INITIATED, {
        "razorpay_payment_id": payment_id,
        "amount_paise": amount_paise,
        "reason": reason,
        "is_retry": is_retry,
    })

    try:
        refund = create_refund(
            payment_id=payment_id,
            amount_paise=amount_paise,
            notes={"reason": reason, "session_id": session.id},
        )
    except StubNotImplemented as exc:
        append(db, session.id, EventType.REFUND_FAILED, {
            "razorpay_payment_id": payment_id,
            "amount_paise": amount_paise,
            "reason": reason,
            "message": str(exc),
            "status_code": None,
            "error_code": "stub_mode",
            "response_body": None,
        })
        return RefundOutcome(accepted=False, pending_settlement=False,
                             error_code="stub_mode")
    except RazorpayError as exc:
        state = settlement_state(_captured_at(db, session))
        pending = is_pending_settlement(
            status_code=exc.status_code, body=exc.body, state=state,
        )
        payload = {
            "razorpay_payment_id": payment_id,
            "amount_paise": amount_paise,
            "reason": reason,
            "is_retry": is_retry,
            # Verbatim, exactly as returned. A paraphrase is not evidence.
            **exc.as_payload(),
            # Why the verbatim body is not enough on its own: it is the same
            # generic 400 whether the account is short of balance or the
            # request was genuinely bad. These fields are what tell them apart.
            "settlement": state.as_payload(),
        }
        if pending:
            payload["retry_after"] = state.expected_at
            payload["provider_dashboard_reason"] = (
                "Your account does not have sufficient balance to instantly "
                "refund this payment."
            )
            append(db, session.id, EventType.REFUND_PENDING_SETTLEMENT, payload)
            log.warning(
                "[saga] refund for %s deferred: payment unsettled, expected %s",
                payment_id, state.expected_at,
            )
            return RefundOutcome(
                accepted=False, pending_settlement=True,
                error_code=exc.code, retry_after=state.expected_at,
            )

        append(db, session.id, EventType.REFUND_FAILED, payload)
        log.error("[saga] refund REJECTED for %s: %s", payment_id, exc)
        return RefundOutcome(accepted=False, pending_settlement=False,
                             error_code=exc.code)

    session.razorpay_refund_id = refund.get("id")
    db.commit()
    append(db, session.id, EventType.REFUND_CONFIRMED, {
        "razorpay_refund_id": refund.get("id"),
        "razorpay_payment_id": payment_id,
        "amount_paise": amount_paise,
        "is_retry": is_retry,
        "response_body": refund,
    })
    return RefundOutcome(accepted=True, pending_settlement=False,
                         refund_id=refund.get("id"))


def _captured_amount(db: Session, session: SessionRecord) -> int:
    """
    What this session actually took from the buyer.

    Read off the payment entry rather than the session's budget: the budget is
    a ceiling that was authorised, and refunding a ceiling returns money that
    was never collected.
    """
    entry = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session.id,
            LedgerEntry.event_type.in_([
                EventType.PAYMENT_CAPTURED.value,
                EventType.PAYMENT_SIMULATED.value,
            ]),
        )
        .order_by(LedgerEntry.seq.desc())
        .first()
    )
    amount = (entry.payload or {}).get("amount_paise") if entry else None
    if isinstance(amount, int) and amount > 0:
        return amount
    raise SagaError(
        "cannot determine the captured amount to refund: no payment entry "
        "carries amount_paise"
    )


def _provider_capture_time(raw: dict | None) -> str | None:
    """
    When the provider says the payment was captured, as an ISO string.

    Razorpay reports epoch seconds, on the payment inside the link's `payments`
    array. Absent or malformed, this returns None and the caller falls back to
    the ledger timestamp rather than inventing one.
    """
    if not isinstance(raw, dict):
        return None
    candidates = []
    for entry in raw.get("payments") or []:
        if isinstance(entry, dict) and entry.get("status") == "captured":
            candidates.append(entry.get("created_at"))
    candidates.append(raw.get("created_at"))
    for value in candidates:
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
    return None


def _captured_at(db: Session, session: SessionRecord) -> datetime | None:
    """When the payment was captured, from the ledger entry that recorded it."""
    entry = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session.id,
            LedgerEntry.event_type.in_([
                EventType.PAYMENT_CAPTURED.value,
                EventType.PAYMENT_SIMULATED.value,
            ]),
        )
        .order_by(LedgerEntry.seq.desc())
        .first()
    )
    if entry is None or entry.ts is None:
        return None
    # ts is stored as an ISO string, not a datetime — the same shape the
    # reconciler parses. A bad or missing timestamp yields None, which makes
    # the settlement state "unknown" rather than guessing a capture date.
    # The provider's capture time if it was recorded, falling back to when we
    # wrote the entry.
    provider_ts = (entry.payload or {}).get("captured_at")
    ts = provider_ts or entry.ts
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _replay_recorded_refund(
    db: Session,
    session: SessionRecord,
    *,
    amount_paise: int,
    payment_id: str | None,
) -> bool:
    """
    Replay the recorded refund attempt onto the ledger.

    Returns True only if the recorded attempt was accepted.

    The refund leg used to produce nothing at all on this path: _settle_replay
    returned refund_id=None unconditionally, so a scenario asking for a refund
    closed as `captured` with no REFUND_* entry anywhere, while the seeder still
    reported "refund 1 ok". The compensating half of the saga was untested and
    invisible.

    No settlement query is made here. Classifying a rejection as
    REFUND_PENDING_SETTLEMENT requires asking the provider what has settled, and
    this path makes no network calls — so the rejection is recorded as
    REFUND_FAILED with the verbatim body, and the payload says plainly that the
    classification was not attempted rather than implying one.
    """
    recorded = fixtures.get("refund_attempt") or {}
    body = recorded.get("response") or {}
    accepted = bool(recorded.get("ok"))
    fixture_path = fixtures.fixture_path_if_backed("refund_attempt")

    append(db, session.id, EventType.REFUND_INITIATED, {
        "razorpay_payment_id": payment_id,
        "amount_paise": amount_paise,
        "reason": "fulfilment_failed",
        "replayed_from_fixture": True,
        "fixture_path": fixture_path,
    }, replayed_from_fixture=True)

    if accepted:
        refund_id = body.get("id")
        session.razorpay_refund_id = refund_id
        db.commit()
        append(db, session.id, EventType.REFUND_CONFIRMED, {
            "razorpay_refund_id": refund_id,
            "razorpay_payment_id": payment_id,
            "amount_paise": amount_paise,
            "response_body": body,
            "replayed_from_fixture": True,
            "fixture_path": fixture_path,
        }, replayed_from_fixture=True)
        return True

    append(db, session.id, EventType.REFUND_FAILED, {
        "razorpay_payment_id": payment_id,
        "amount_paise": amount_paise,
        "reason": "fulfilment_failed",
        # Exactly what the provider returned, carried through unchanged.
        "message": body.get("message"),
        "status_code": body.get("status_code") or recorded.get("status_code"),
        "error_code": body.get("error_code"),
        "response_body": body.get("response_body", body),
        "settlement": {
            "status": "not_queried",
            "status_source": (
                "replay mode makes no network calls; the recorded attempt "
                "carries no settlement state"
            ),
            "expected_at": None,
            "expected_basis": None,
        },
        "replayed_from_fixture": True,
        "fixture_path": fixture_path,
    }, replayed_from_fixture=True)
    return False


def settle_authorised_cart(
    db: Session,
    session: SessionRecord,
    cart: Cart,
    *,
    payments: PaymentMode,
    simulate_refund: bool = False,
    capture_timeout_seconds: float = 300.0,
) -> dict:
    """
    Move the money for a cart that has already cleared policy.

    Extracted so there is exactly one settlement path. It is reached two ways:
    straight through `run_saga` on an ALLOW, and from the approve endpoint when
    a human clears an ESCALATE. Those had drifted apart — approving an
    escalation recorded HUMAN_APPROVED and then did nothing at all, so the
    session sat idle until the reconciler swept it stale sixty seconds later.
    An operator saw the card vanish and no transaction happen.

    A human approval is an authorisation to move money. It has to actually move
    it, in whatever mode is configured, or the button is decoration.
    """
    # ── Settle ─────────────────────────────────────────────────────────────
    if payments is PaymentMode.LIVE:
        # Logs its own ORDER_CREATED (it has the real order and link) and
        # blocks on the confirmer until the human pays.
        settlement = _settle_live(db, session, cart, wait_seconds=capture_timeout_seconds)
    else:
        settlement = (
            _settle_replay(session, simulate_refund)
            if payments is PaymentMode.REPLAY
            else _settle_synthetic(session, simulate_refund)
        )

        session.razorpay_order_id = settlement.order_id
        db.commit()

        append(db, session.id, EventType.ORDER_CREATED, {
            "razorpay_order_id": settlement.order_id,
            "receipt": f"tollgate_{session.id[:8]}",
            "amount_paise": cart.total_paise,
            "currency": "INR",
            "short_url": settlement.short_url,
            "payments": payments.value,
            "replayed_from_fixture": settlement.from_real_fixture,
            "synthetic": not settlement.from_real_fixture,
            "fixture_path": (
                fixtures.fixture_path_if_backed("order")
                if settlement.from_real_fixture else None
            ),
        }, replayed_from_fixture=settlement.from_real_fixture)

    result: dict[str, Any] = {
        "mode": payments.value,
        "order_id": settlement.order_id,
        "replayed_from_fixture": settlement.from_real_fixture,
        "total_paise": cart.total_paise,
    }

    # A live link is not a payment. It stays open until the webhook confirms
    # capture, and the session is deliberately left un-closed until then.
    if settlement.awaiting_capture:
        result["payment_link_url"] = settlement.short_url
        result["payment_link_id"] = settlement.order_id
        return result

    # ── 8. Replayed capture ───────────────────────────────────────────────────
    session.razorpay_payment_id = settlement.payment_id
    session.status = "captured"
    db.commit()

    if payments is not PaymentMode.LIVE:
        # The live path recorded its own capture inside await_live_capture,
        # where the real waited_seconds and provider status were in hand.
        append(db, session.id, settlement.event, {
            "razorpay_order_id": settlement.order_id,
            "razorpay_payment_id": settlement.payment_id,
            "amount_paise": cart.total_paise,
            "payments": payments.value,
            "replayed_from_fixture": settlement.from_real_fixture,
            # What the replay does and does not cover.
            #
            # The identifiers are real: that order was created and that payment
            # was captured. The amount is this scenario's cart, which is not
            # what was captured. Without this field a REPLAYED badge over a
            # Rs.479 cart reads as "Rs.479 really moved", when the recording is
            # for Rs.1. The ids are replayed; the amount is not.
            "replayed_amount_paise": (
                fixtures.recorded_amount_paise("payment")
                if settlement.from_real_fixture else None
            ),
            "replayed_fields": (
                ["razorpay_order_id", "razorpay_payment_id"]
                if settlement.from_real_fixture else []
            ),
            # Names the file only when this event was actually read from it.
            # Keying off whether the file exists instead would have a synthetic
            # run cite a recording it never opened.
            "fixture_path": (
                fixtures.fixture_path_if_backed("payment")
                if settlement.from_real_fixture else None
            ),
            "synthetic": not settlement.from_real_fixture,
        }, replayed_from_fixture=settlement.from_real_fixture)

    result["payment_id"] = settlement.payment_id

    refunded = False
    pending_settlement = False
    if simulate_refund:
        if payments is PaymentMode.LIVE:
            # The real thing. If the provider rejects it, that rejection is the
            # outcome — recorded, surfaced, and not papered over with a
            # synthetic refund the buyer never received.
            outcome = attempt_refund(
                db, session, amount_paise=cart.total_paise,
                reason="fulfilment_failed",
            )
            refunded = outcome.accepted
            pending_settlement = outcome.pending_settlement
            result["refund_accepted"] = outcome.accepted
            result["refund_pending_settlement"] = outcome.pending_settlement
            result["refund_retry_after"] = outcome.retry_after
        elif payments is PaymentMode.REPLAY and fixtures.has("refund_attempt"):
            # Replay the refund that was actually attempted against the
            # provider. It was rejected, and replaying the rejection is the
            # whole point: the recorded body is the only evidence of what the
            # provider said, and a run that quietly produced a successful
            # refund instead would be asserting money went back when it did not.
            refunded = _replay_recorded_refund(
                db, session, amount_paise=cart.total_paise,
                payment_id=settlement.payment_id,
            )
            result["refund_replayed"] = True
            result["refund_accepted"] = refunded
        elif settlement.refund_id:
            # Synthetic: no provider was involved, so the refund is generated
            # like every other identifier on this path and labelled as such.
            session.razorpay_refund_id = settlement.refund_id
            session.status = "refunded"
            db.commit()
            append(db, session.id, EventType.REFUND_INITIATED, {
                "razorpay_payment_id": settlement.payment_id,
                "amount_paise": cart.total_paise,
                "reason": "fulfilment_failed",
                "synthetic": True,
            })
            append(db, session.id, EventType.REFUND_SIMULATED, {
                "razorpay_refund_id": settlement.refund_id,
                "razorpay_payment_id": settlement.payment_id,
                "amount_paise": cart.total_paise,
                "reason": "fulfilment_failed",
                "replayed_from_fixture": False,
                "synthetic": True,
                "fixture_path": None,
            })
            result["refund_id"] = settlement.refund_id
            refunded = True

    close_session(
        db, session,
        # A rejected refund leaves the session captured, not refunded. Marking
        # it refunded would assert money went back when it did not.
        # A refund awaiting settlement leaves the session unresolved: money is
        # still owed and a retry is still due, so it must not close as though
        # the matter were finished.
        status=(
            "refunded" if refunded
            else "refund_pending" if pending_settlement
            else "captured"
        ),
        reason=(
            "refund confirmed" if refunded
            else "payment captured; refund deferred until settlement"
            if pending_settlement
            else "payment captured; refund rejected by provider" if simulate_refund
            else "payment captured"
        ),
        final_total_paise=cart.total_paise,
        replayed_from_fixture=settlement.from_real_fixture,
    )
    return result


def run_saga(
    *,
    db: Session,
    session: SessionRecord,
    intent: IntentMandate,
    cart: Cart,
    history: list[TxnHistoryItem],
    payments: PaymentMode = PaymentMode.SYNTHETIC,
    cart_token: str | None = None,
    capture_timeout_seconds: float = 300.0,   # 5 minutes on the live path
    offer_upsell: bool = True,
    accept_upsell: bool = True,
    simulate_refund: bool = False,
) -> dict:
    """
    The single money-movement path. Live and seeded runs differ only by the
    arguments passed here.

    There used to be two implementations. They drifted, and the drift landed on
    the one control that matters most: the harness path never called
    verify_cart_mandate at all, so every eval result was produced by a path
    that skipped the enforcement point the system rests on. A full attack run
    recorded zero rows in mandate_jtis. Behaviour that must hold everywhere
    cannot live in two places.

    The mandate is verified on every path. When no cart token is supplied, one
    is signed here first and then verified — so the verifier runs even for a
    caller that has no client to sign for it, and a caller *can* inject a
    forged, replayed or mismatched token to exercise it.
    """
    # ── 1. Intent replay guard ────────────────────────────────────────────────
    intent_exp = datetime.utcfromtimestamp(intent.exp)
    if not record_intent_jti(
        jti=intent.jti, expires_at=intent_exp, db=db, session_id=session.id
    ):
        raise SagaError("intent JTI already used (replay)")

    # ── 2. Nothing to judge without a cart ────────────────────────────────────
    _guard_non_empty_cart(db, session, cart)

    # ── 3. Obtain and verify the cart mandate ─────────────────────────────────
    # Self-signed when the caller has no client of its own. The verification
    # below is not skipped in that case: it is the same call, on the same code
    # path, and a caller that wants to test rejection supplies its own token.
    if cart_token is None:
        cart_token, _mandate = sign_cart(intent_jti=intent.jti, cart=cart)

    verify = verify_cart_mandate(cart_token, cart, db, session_id=session.id)

    # ── 4. Policy ─────────────────────────────────────────────────────────────
    verdict = evaluate(
        intent=intent,
        cart=cart,
        history=history,
        mandate_valid=verify.valid,
        mandate_fail_reason=verify.reason,
    )

    append(db, session.id, EventType.POLICY_EVALUATED, {
        "decision": verdict.decision.value,
        "code": verdict.code.value,
        "detail": verdict.detail,
        "mandate_valid": verify.valid,
        "mandate_reason": verify.reason,
        "payments": payments.value,
    })

    if verdict.decision.value == "DENY":
        close_session(
            db, session,
            status="failed",
            reason=f"policy DENY: {verdict.code.value}",
            final_total_paise=cart.total_paise,
        )
        raise SagaError(f"policy DENY: {verdict.code.value} — {verdict.detail}")

    if verdict.decision.value == "ESCALATE":
        esc = EscalationRequest(
            id=str(uuid.uuid4()),
            session_id=session.id,
            reason_code=verdict.code.value,
            detail=verdict.detail,
            intent_snapshot=intent.model_dump(),
            cart_snapshot={
                "merchant_id": cart.merchant_id,
                "items": [i.model_dump() for i in cart.items],
                "total_paise": cart.total_paise,
            },
            status="pending",
        )
        db.add(esc)
        db.commit()
        append(db, session.id, EventType.ESCALATED, {
            "escalation_id": esc.id,
            "reason_code": verdict.code.value,
            "detail": verdict.detail,
            "evidence": _escalation_evidence(
                verdict.code.value, cart, history, db, session.buyer_id
            ),
        })
        raise SagaEscalated(esc.id, verdict.code.value, verdict.detail)

    # ── 5. Offer ──────────────────────────────────────────────────────────────
    total_before_offer = cart.total_paise
    if offer_upsell:
        cart = run_upsell(db, session, intent, cart, accept=accept_upsell)

    # ── 6. Sign the cart that is actually paid ────────────────────────────────
    # Signed once, after the offer. An accepted offer changes the total, and a
    # signature over the superseded cart would not cover what is being paid.
    resigned = cart.total_paise != total_before_offer
    if resigned:
        cart_token, _mandate = sign_cart(intent_jti=intent.jti, cart=cart)
        verify = verify_cart_mandate(cart_token, cart, db, session_id=session.id)
        if not verify.valid:
            close_session(
                db, session,
                status="failed",
                reason=f"post-offer mandate invalid: {verify.reason}",
                final_total_paise=cart.total_paise,
            )
            raise SagaError(f"post-offer mandate verification failed: {verify.reason}")

    claims = verify.cart_mandate_claims or {}
    append(db, session.id, EventType.CART_SIGNED, {
        "jti": claims.get("jti"),
        "intent_jti": claims.get("intent_jti") or intent.jti,
        "cart_hash": cart.canonical_hash(),
        "total_paise": cart.total_paise,
        "item_count": len(cart.items),
        "resigned_after_upsell": resigned,
        "mandate_valid": verify.valid,
    })

    return settle_authorised_cart(
        db, session, cart,
        payments=payments,
        simulate_refund=simulate_refund,
        capture_timeout_seconds=capture_timeout_seconds,
    )



# ── Thin wrappers ─────────────────────────────────────────────────────────────
#
# Kept so existing callers read naturally. Neither contains any behaviour: they
# only choose where the payment identifiers come from.

def run_saga_demo(
    *,
    db: Session,
    session: SessionRecord,
    intent: IntentMandate,
    intent_token: str,
    cart: Cart,
    cart_token: str,
    history: list[TxnHistoryItem],
    offer_upsell: bool = True,
    accept_upsell: bool = True,
) -> dict:
    """Option A: real Razorpay payment link. Delegates to run_saga()."""
    return run_saga(
        db=db, session=session, intent=intent, cart=cart, history=history,
        payments=PaymentMode.LIVE,
        cart_token=cart_token,
        offer_upsell=offer_upsell,
        accept_upsell=accept_upsell,
    )


def run_saga_harness(
    *,
    db: Session,
    session: SessionRecord,
    intent: IntentMandate,
    cart: Cart,
    history: list[TxnHistoryItem],
    cart_token: str | None = None,
    simulate_refund: bool = False,
    offer_upsell: bool = True,
    accept_upsell: bool = True,
    payments: PaymentMode = PaymentMode.REPLAY,
) -> dict:
    """
    Option B: recorded or generated identifiers, never a live Razorpay call.

    REPLAY uses the recorded capture when evals/fixtures/razorpay_capture.json
    has one and falls back to SYNTHETIC when it does not. SYNTHETIC skips the
    fixture entirely — that is what --payments=synthetic selects, and what makes
    a full demo possible with no network and no recording on disk.

    `cart_token` lets a scenario supply a forged, expired, replayed or
    mismatched mandate so the verifier can be exercised end to end. Left as
    None, run_saga signs one and verifies it like any other.
    """
    return run_saga(
        db=db, session=session, intent=intent, cart=cart, history=history,
        payments=payments,
        cart_token=cart_token,
        offer_upsell=offer_upsell,
        accept_upsell=accept_upsell,
        simulate_refund=simulate_refund,
    )


def initiate_refund(
    *,
    db: Session,
    session: SessionRecord,
    amount_paise: int | None = None,
    reason: str = "fulfilment_failed",
) -> dict:
    """
    REST entry point for refunding a captured payment.

    Delegates to attempt_refund so there is exactly one place a refund is
    attempted and exactly one way its rejection is recorded. The earlier version
    raised SagaError on failure, which discarded the provider's response body
    and left nothing on the ledger — the failure existed only in a log line.
    """
    outcome = attempt_refund(
        db, session,
        # The captured amount, not the authorised ceiling. budget_paise is what
        # the buyer allowed to be spent; refunding that would hand back more
        # than was ever taken. What was actually captured is on the ledger.
        amount_paise=amount_paise or _captured_amount(db, session),
        reason=reason,
    )
    return {
        "accepted": outcome.accepted,
        # A caller that only sees accepted=false cannot tell a refusal from a
        # deferral, and those need opposite handling.
        "pending_settlement": outcome.pending_settlement,
        "retry_after": outcome.retry_after,
        "error_code": outcome.error_code,
        "refund_id": outcome.refund_id,
        "session_id": session.id,
        "payment_id": session.razorpay_payment_id,
    }
