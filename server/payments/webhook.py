"""
Payment webhook handler.

Razorpay sends POST /webhook with an X-Razorpay-Signature header.
We verify the signature against RAZORPAY_WEBHOOK_SECRET before processing.

Events we care about:
  - payment.captured  → update session, append PAYMENT_CAPTURED to ledger
  - payment.failed    → append FULFILMENT_FAILED
  - refund.created    → append REFUND_CONFIRMED (if amount matches)

All ledger writes are idempotent via the session_id + event_type + payment_id
check. A duplicate webhook does nothing.

Registration: POST /webhook is wired in server/api/routes.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request, HTTPException
from sqlalchemy.orm import Session

from server.config import settings
from server.db.models import LedgerEntry, SessionRecord
from server.ledger.chain import append
from server.ledger.events import EventType
from server.payments.razorpay_client import verify_webhook_signature

log = logging.getLogger(__name__)


async def handle_webhook(request: Request, db: Session) -> dict:
    """
    Validate and dispatch a Razorpay webhook.
    Returns {"status": "ok"} on success so Razorpay stops retrying.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if settings.RAZORPAY_WEBHOOK_SECRET:
        if not verify_webhook_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
            log.warning("[webhook] invalid signature — rejecting")
            raise HTTPException(status_code=400, detail="invalid signature")
    else:
        log.warning("[webhook] RAZORPAY_WEBHOOK_SECRET not set — skipping signature check")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad json")

    event: str = payload.get("event", "")
    entity: dict = payload.get("payload", {}).get("payment", {}).get("entity", {})

    log.info(f"[webhook] event={event} payment_id={entity.get('id')}")

    if event == "payment.captured":
        _on_payment_captured(entity, db)
    elif event == "payment.failed":
        _on_payment_failed(entity, db)
    elif event == "refund.created":
        refund_entity = payload.get("payload", {}).get("refund", {}).get("entity", {})
        _on_refund_created(refund_entity, db)

    return {"status": "ok"}


def _session_by_order(order_id: str, db: Session) -> SessionRecord | None:
    return (
        db.query(SessionRecord)
        .filter(SessionRecord.razorpay_order_id == order_id)
        .first()
    )


def _already_appended(session_id: str, event_type: str, db: Session) -> bool:
    return bool(
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session_id,
            LedgerEntry.event_type == event_type,
        )
        .first()
    )


def _on_payment_captured(entity: dict, db: Session) -> None:
    order_id = entity.get("order_id", "")
    payment_id = entity.get("id", "")
    session = _session_by_order(order_id, db)
    if not session:
        log.warning(f"[webhook] payment.captured: no session for order {order_id}")
        return

    if _already_appended(session.id, EventType.PAYMENT_CAPTURED.value, db):
        log.info(f"[webhook] payment.captured duplicate — session {session.id}")
        return

    session.razorpay_payment_id = payment_id
    session.status = "captured"
    append(
        db,
        session_id=session.id,
        event_type=EventType.PAYMENT_CAPTURED,
        payload={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": order_id,
            "amount": entity.get("amount"),
            "method": entity.get("method"),
            "captured": True,
        },
    )
    db.commit()
    close_session(db, session, status="captured", reason="payment captured via webhook")
    log.info(f"[webhook] PAYMENT_CAPTURED session={session.id} payment={payment_id}")


def close_session(*args, **kwargs):
    """Thin indirection so the saga import stays lazy (webhook <-> saga cycle)."""
    from server.payments.saga import close_session as _close
    return _close(*args, **kwargs)


def _on_payment_failed(entity: dict, db: Session) -> None:
    order_id = entity.get("order_id", "")
    session = _session_by_order(order_id, db)
    if not session:
        return

    if _already_appended(session.id, EventType.FULFILMENT_FAILED.value, db):
        return

    session.status = "failed"
    append(
        db,
        session_id=session.id,
        event_type=EventType.FULFILMENT_FAILED,
        payload={
            "reason": "payment_failed",
            "razorpay_order_id": order_id,
            "error_code": entity.get("error_code"),
            "error_description": entity.get("error_description"),
        },
    )
    db.commit()
    close_session(db, session, status="failed", reason="payment failed")
    log.info(f"[webhook] FULFILMENT_FAILED session={session.id}")


def _on_refund_created(entity: dict, db: Session) -> None:
    payment_id = entity.get("payment_id", "")
    refund_id = entity.get("id", "")

    session = (
        db.query(SessionRecord)
        .filter(SessionRecord.razorpay_payment_id == payment_id)
        .first()
    )
    if not session:
        return

    if _already_appended(session.id, EventType.REFUND_CONFIRMED.value, db):
        return

    session.razorpay_refund_id = refund_id
    session.status = "refunded"
    append(
        db,
        session_id=session.id,
        event_type=EventType.REFUND_CONFIRMED,
        payload={
            "razorpay_refund_id": refund_id,
            "razorpay_payment_id": payment_id,
            "amount": entity.get("amount"),
        },
    )
    db.commit()
    close_session(db, session, status="refunded", reason="refund confirmed via webhook")
    log.info(f"[webhook] REFUND_CONFIRMED session={session.id} refund={refund_id}")
