"""
Session reconciler.

Runs as an asyncio background task (started in FastAPI lifespan) and performs
two sweeps every RECONCILER_INTERVAL_SECONDS:

  1. Orphaned payments — a session holding a razorpay_order_id with no capture
     past ORPHANED_PAYMENT_TIMEOUT_SECONDS. Recorded as FULFILMENT_FAILED and
     moved to "failed".

  2. Stalled sessions — any active session that has stopped producing ledger
     entries past STALE_SESSION_TIMEOUT_SECONDS, whether or not it ever reached
     the payment stage. Recorded as SESSION_STALE and moved to "stale".

The second sweep exists because the first one could not see most hung sessions:
it required razorpay_order_id IS NOT NULL, so a session that stalled at policy
evaluation — before any order existed — was never a candidate and sat in
"active" indefinitely. A hung session presenting itself as live is exactly the
kind of silent failure this system is supposed to make impossible.

Timestamps are compared as naive UTC throughout. SessionRecord.created_at and
updated_at are written by the database (server_default=now()) and stored naive;
mixing an aware datetime into that comparison makes SQLite fall back to
comparing strings with an offset suffix, which is not reliably ordered.

The double_charge and refund_race eval cases are run against Postgres (per the
user requirement); under SQLite this reconciler is informational only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from server.config import settings
from server.db.models import LedgerEntry, SessionRecord
from server.db.session import SessionLocal
from server.ledger.chain import append
from server.ledger.events import EventType

log = logging.getLogger(__name__)

# Statuses a session can be swept out of. Anything else has already settled.
_SWEEPABLE = ("active",)


def _utcnow_naive() -> datetime:
    """Naive UTC, matching how the DB writes created_at / updated_at."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def run_reconciler() -> None:
    """Main reconciler loop. Runs forever until cancelled."""
    log.info(
        f"[reconciler] started: interval={settings.RECONCILER_INTERVAL_SECONDS}s "
        f"orphan_timeout={settings.ORPHANED_PAYMENT_TIMEOUT_SECONDS}s "
        f"stale_timeout={settings.STALE_SESSION_TIMEOUT_SECONDS}s"
    )
    while True:
        try:
            sweep()
        except Exception as exc:
            log.error(f"[reconciler] sweep error: {exc}", exc_info=True)
        await asyncio.sleep(settings.RECONCILER_INTERVAL_SECONDS)


def sweep(db: Session | None = None) -> dict[str, int]:
    """
    Run both sweeps once. Returns how many sessions each one touched.

    Exposed (rather than private) so tests can drive a single pass without
    starting the loop.
    """
    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        return {
            "orphaned": _sweep_orphaned_payments(db),
            "stale": _sweep_stalled_sessions(db),
            "refunds_retried": _sweep_pending_refunds(db),
        }
    finally:
        if owns_db:
            db.close()


def _last_activity(db: Session, session_id: str) -> datetime | None:
    """
    When this session last produced a ledger entry, as naive UTC.

    The ledger is the real progress signal: updated_at also moves for changes
    that are not progress, and a session with no entries at all has never done
    anything.
    """
    row = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq.desc())
        .first()
    )
    if row is None:
        return None
    try:
        ts = datetime.fromisoformat(row.ts)
    except (TypeError, ValueError):
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def _sweep_orphaned_payments(db: Session) -> int:
    """Sessions that created an order but never saw it captured."""
    cutoff = _utcnow_naive() - timedelta(
        seconds=settings.ORPHANED_PAYMENT_TIMEOUT_SECONDS
    )

    orphaned = (
        db.query(SessionRecord)
        .filter(
            SessionRecord.status.in_(_SWEEPABLE),
            SessionRecord.razorpay_order_id.isnot(None),
            SessionRecord.razorpay_payment_id.is_(None),
            SessionRecord.updated_at < cutoff,
        )
        .all()
    )

    for session in orphaned:
        log.warning(
            f"[reconciler] orphaned session {session.id} "
            f"(order {session.razorpay_order_id})"
        )
        append(
            db,
            session_id=session.id,
            event_type=EventType.FULFILMENT_FAILED,
            payload={
                "reason": "reconciler_timeout",
                "razorpay_order_id": session.razorpay_order_id,
                "orphan_cutoff": cutoff.isoformat(),
            },
        )
        from server.payments.saga import close_session
        close_session(
            db, session,
            status="failed",
            reason="orphaned payment: order created, never captured",
        )

    return len(orphaned)


def _sweep_pending_refunds(db: Session) -> int:
    """
    Retry refunds the provider deferred for want of settled balance.

    These are not failures and must not be swept, closed, or counted as
    resolved. The payment is captured, the buyer is owed the money, and the only
    thing standing between the two is Razorpay's settlement cycle — so the
    reconciler's job here is to keep coming back until the refund lands, and to
    leave a record each time it tries.

    Two guards on the retry:

      * Nothing is attempted before the expected settlement date. Hammering the
        API in the meantime produces the same 400 over and over and buries the
        real state in noise.
      * A session whose retry succeeded, or which turned into a hard
        REFUND_FAILED, is no longer a candidate. Only genuinely open ones are.
    """
    from server.payments.saga import attempt_refund

    now = _utcnow_naive()
    retried = 0

    for session in (
        db.query(SessionRecord)
        .filter(SessionRecord.status == "refund_pending")
        .all()
    ):
        entries = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id)
            .order_by(LedgerEntry.seq.desc())
            .all()
        )
        types = [e.event_type for e in entries]
        if EventType.REFUND_CONFIRMED.value in types:
            continue                      # already made good
        if EventType.REFUND_FAILED.value in types:
            continue                      # a real refusal; not this sweep's business

        deferred = next(
            (e for e in entries
             if e.event_type == EventType.REFUND_PENDING_SETTLEMENT.value),
            None,
        )
        if deferred is None:
            continue

        payload = deferred.payload or {}
        expected = (payload.get("settlement") or {}).get("expected_at")
        if expected and _before_settlement(now, expected):
            continue                      # cannot possibly succeed yet

        last = _last_activity(db, session.id)
        if last is not None and (now - last).total_seconds() < (
            settings.REFUND_RETRY_INTERVAL_SECONDS
        ):
            continue                      # backing off between attempts

        amount = payload.get("amount_paise") or session.budget_paise
        append(
            db,
            session_id=session.id,
            event_type=EventType.REFUND_RETRY_SCHEDULED,
            payload={
                "razorpay_payment_id": session.razorpay_payment_id,
                "amount_paise": amount,
                "deferred_at_seq": deferred.seq,
                "expected_settlement": expected,
                "attempt": types.count(EventType.REFUND_RETRY_SCHEDULED.value) + 1,
            },
        )
        try:
            outcome = attempt_refund(
                db, session, amount_paise=amount,
                reason=payload.get("reason") or "fulfilment_failed",
                is_retry=True,
            )
        except Exception as exc:                          # noqa: BLE001
            log.error("[reconciler] refund retry failed for %s: %s", session.id, exc)
            continue

        retried += 1
        if outcome.accepted:
            session.status = "refunded"
            db.commit()
            log.info("[reconciler] deferred refund for %s succeeded", session.id)
        elif not outcome.pending_settlement:
            # It stopped being a settlement problem and became a real refusal.
            session.status = "refund_failed"
            db.commit()
        # Still pending: left as-is, deliberately. The session stays open.

    return retried


def _before_settlement(now: datetime, expected_iso: str) -> bool:
    """True while the expected settlement date is still in the future."""
    try:
        due = datetime.fromisoformat(expected_iso)
    except (TypeError, ValueError):
        return False
    if due.tzinfo is not None:
        due = due.astimezone(timezone.utc).replace(tzinfo=None)
    return now < due


def _sweep_stalled_sessions(db: Session) -> int:
    """
    Sessions that stopped making progress before reaching a terminal state.

    Deliberately does not require an order id — the common stall is earlier than
    that. A session blocked on a pending escalation is not stalled: it is
    waiting on a human by design, and the escalations panel already shows it.
    """
    now = _utcnow_naive()
    cutoff = now - timedelta(seconds=settings.STALE_SESSION_TIMEOUT_SECONDS)

    from server.db.models import EscalationRequest
    awaiting_human = {
        row.session_id
        for row in db.query(EscalationRequest)
        .filter(EscalationRequest.status == "pending")
        .all()
    }

    candidates = (
        db.query(SessionRecord)
        .filter(SessionRecord.status.in_(_SWEEPABLE))
        .all()
    )

    swept = 0
    for session in candidates:
        if session.id in awaiting_human:
            continue

        last = _last_activity(db, session.id) or session.created_at
        if last is None or last >= cutoff:
            continue

        stalled_for = int((now - last).total_seconds())
        log.warning(
            f"[reconciler] stalled session {session.id} "
            f"(no ledger activity for {stalled_for}s)"
        )
        append(
            db,
            session_id=session.id,
            event_type=EventType.SESSION_STALE,
            payload={
                "reason": "no_progress_past_threshold",
                "stalled_for_seconds": stalled_for,
                "threshold_seconds": settings.STALE_SESSION_TIMEOUT_SECONDS,
                "last_activity": last.isoformat(),
            },
        )
        session.status = "stale"
        db.commit()
        swept += 1

    return swept
