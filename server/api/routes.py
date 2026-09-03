"""
REST API routes.

Endpoints:
  POST   /sessions                    — create session, get intent token
  GET    /sessions                    — list sessions (dashboard left rail)
  POST   /sessions/{id}/checkout      — submit cart mandate, run saga
  GET    /sessions/{id}               — session status
  GET    /sessions/{id}/ledger        — paginated ledger for this session
  POST   /sessions/{id}/escalations/{esc_id}/approve
  POST   /sessions/{id}/escalations/{esc_id}/reject
  GET    /ledger                      — global ledger (paginated)
  GET    /ledger/verify               — hash-chain integrity check
  POST   /ledger/tamper               — (env-gated) mutate one entry for demo
  GET    /metrics                     — aggregate stats
  POST   /webhook                     — Razorpay webhook
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.api import analytics, narrative
from server.config import settings
from server.db.models import EscalationRequest, LedgerEntry, SessionRecord
from server.db.session import get_db
from server.ledger.chain import append, verify_chain
from server.ledger.events import EventType
from server.mandate.issuer import sign_intent
from server.mandate.schema import Cart, CartItem
from server.mandate.verifier import verify_cart_mandate, record_intent_jti
from server.mcp.cart import CartBuildError, build_authoritative_cart
from server.mcp.catalog import get_authoritative_price
from server.payments.saga import (
    SagaEscalated,
    SagaError,
    run_saga_demo,
    run_saga_harness,
)
from server.payments.webhook import handle_webhook
from server.policy.engine import evaluate
from server.policy.history import build_buyer_history

log = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    buyer_id: str
    goal: str
    budget_paise: int
    categories: list[str]
    max_items: int = 10
    estimate_paise: int | None = None


class CheckoutRequest(BaseModel):
    """
    Cart mandate JWT + list of SKU IDs and quantities.

    Prices are looked up server-side — the client MUST NOT submit prices.
    """
    cart_mandate_jwt: str
    intent_mandate_jwt: str
    sku_ids: list[str]
    quantities: list[int]
    mode: str = "demo"    # "demo" | "harness"


class EscalationDecisionRequest(BaseModel):
    resolved_by: str = "human_operator"


# ──────────────────────────────────────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/sessions", tags=["sessions"], status_code=201)
def create_session(
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
):
    """
    Create a new buyer session and return a signed IntentMandate JWT.
    The intent JWT must be submitted with every checkout request.
    """
    session_id = str(uuid.uuid4())

    # Sign the intent mandate for this session
    intent_token, intent_mandate = sign_intent(
        buyer_id=body.buyer_id,
        merchant_id=settings.MERCHANT_ID,
        budget_paise=body.budget_paise,
        categories=body.categories,
        max_items=body.max_items,
        estimate_paise=body.estimate_paise or int(body.budget_paise * 0.85),
    )

    # Persist session
    session = SessionRecord(
        id=session_id,
        buyer_id=body.buyer_id,
        merchant_id=settings.MERCHANT_ID,
        goal=body.goal,
        budget_paise=body.budget_paise,
        status="active",
    )
    db.add(session)
    db.commit()

    # Record intent JTI
    from datetime import datetime
    record_intent_jti(
        jti=intent_mandate.jti,
        expires_at=datetime.utcfromtimestamp(intent_mandate.exp),
        db=db,
        session_id=session_id,
    )

    append(db, session_id=session_id, event_type=EventType.INTENT_SIGNED, payload={
        "jti": intent_mandate.jti,
        "buyer_id": body.buyer_id,
        "budget_paise": body.budget_paise,
        "categories": body.categories,
        "max_items": body.max_items,
    })

    return {
        "session_id": session_id,
        "intent_mandate_jwt": intent_token,
        "intent_jti": intent_mandate.jti,
        "expires_at": intent_mandate.exp,
        "merchant_id": settings.MERCHANT_ID,
    }


@router.get("/sessions", tags=["sessions"])
def list_sessions(limit: int = 100, db: Session = Depends(get_db)):
    """
    Every session, newest first. Backs the dashboard's left rail, which polls
    this on a 2s interval.

    `elapsed_ms` is computed here rather than in the browser: terminal sessions
    report their settled duration, live ones a running clock. The client renders
    the number it is given and does no clock arithmetic of its own.
    """
    sessions = (
        db.query(SessionRecord)
        .order_by(SessionRecord.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )
    entries = db.query(LedgerEntry).order_by(LedgerEntry.seq).all()

    spans = analytics.session_spans(entries)
    now = datetime.now(timezone.utc)

    event_counts: dict[str, int] = {}
    # A settled session's duration is the one its own SESSION_CLOSED entry
    # recorded. Re-deriving it from the ledger span loses the sub-second
    # precision that most of these sessions finish in, which is how every
    # settled row came to read 00:00.
    closed_duration: dict[str, int] = {}
    # Offer state per session, for the rail's chip.
    offer_state: dict[str, str] = {}
    for e in entries:
        event_counts[e.session_id] = event_counts.get(e.session_id, 0) + 1
        if e.event_type == EventType.UPSELL_PROPOSED.value:
            offer_state[e.session_id] = (
                "withheld" if (e.payload or {}).get("blocked") else "offered"
            )
        elif e.event_type == EventType.UPSELL_ACCEPTED.value:
            offer_state[e.session_id] = "accepted"
        elif e.event_type == EventType.UPSELL_REJECTED.value:
            if offer_state.get(e.session_id) != "accepted":
                offer_state[e.session_id] = (
                    "withheld"
                    if (e.payload or {}).get("reason") == "exceeded_remaining_headroom"
                    else "declined"
                )
        if e.event_type == EventType.SESSION_CLOSED.value:
            duration = (e.payload or {}).get("duration_ms")
            if isinstance(duration, int):
                closed_duration[e.session_id] = duration

    # Sessions blocked on a human decision are surfaced as "escalated" in the
    # rail even before the session row itself flips, so the operator sees the
    # queue forming.
    pending_escalations = {
        row.session_id
        for row in db.query(EscalationRequest)
        .filter(EscalationRequest.status == "pending")
        .all()
    }

    return {
        "count": len(sessions),
        "sessions": [
            {
                "session_id": s.id,
                "buyer_id": s.buyer_id,
                "merchant_id": s.merchant_id,
                "goal": s.goal,
                "budget_paise": s.budget_paise,
                "status": s.status,
                "has_pending_escalation": s.id in pending_escalations,
                "event_count": event_counts.get(s.id, 0),
                "elapsed_ms": closed_duration.get(
                    s.id, analytics.elapsed_ms_for(s, spans.get(s.id), now)
                ),
                "offer": offer_state.get(s.id),
                "elapsed_source": (
                    "session_closed" if s.id in closed_duration else "ledger_span"
                ),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "razorpay_payment_id": s.razorpay_payment_id,
                "razorpay_refund_id": s.razorpay_refund_id,
            }
            for s in sessions
        ],
    }


@router.get("/sessions/{session_id}", tags=["sessions"])
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if not session:
        raise HTTPException(404, "session not found")
    return {
        "session_id": session.id,
        "buyer_id": session.buyer_id,
        "merchant_id": session.merchant_id,
        "goal": session.goal,
        "budget_paise": session.budget_paise,
        "status": session.status,
        "razorpay_order_id": session.razorpay_order_id,
        "razorpay_payment_id": session.razorpay_payment_id,
        "razorpay_refund_id": session.razorpay_refund_id,
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


@router.get("/sessions/{session_id}/ledger", tags=["sessions"])
def get_session_ledger(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    around_seq: int | None = None,
    db: Session = Depends(get_db),
):
    """
    Paginated ledger slice for one session. Same shape as GET /ledger.

    `around_seq` overrides `offset` and returns whichever page contains that
    entry. The operator view links each narrative line to its seq, and that
    jump has to land on the row even when the session runs past one page.
    """
    query = db.query(LedgerEntry).filter(LedgerEntry.session_id == session_id)
    total = query.count()
    page_size = min(limit, 200)

    if around_seq is not None:
        ordered = [e.seq for e in query.order_by(LedgerEntry.seq).all()]
        if around_seq in ordered:
            offset = (ordered.index(around_seq) // page_size) * page_size

    entries = (
        query.order_by(LedgerEntry.seq)
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return {
        "session_id": session_id,
        "total": total,
        "offset": offset,
        "count": len(entries),
        "entries": [
            {
                "seq": e.seq,
                "ts": e.ts,
                "session_id": e.session_id,
                "event_type": e.event_type,
                "payload": e.payload,
                "hash": e.hash,
                "prev_hash": e.prev_hash,
                "replayed_from_fixture": e.replayed_from_fixture,
            }
            for e in entries
        ],
    }


@router.get("/sessions/{session_id}/narrative", tags=["sessions"])
def get_session_narrative(session_id: str, db: Session = Depends(get_db)):
    """
    Plain-English account of one session, derived from its ledger payloads.

    Templated from recorded fields — no model is involved. Each line names the
    seq it came from so the operator view can link every sentence to the entry
    that proves it.
    """
    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if not session:
        raise HTTPException(404, "session not found")

    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .all()
    )
    return {
        "session_id": session_id,
        "status": session.status,
        "buyer_id": session.buyer_id,
        "goal": session.goal,
        "lines": narrative.build_narrative(session, entries),
        "event_labels": narrative.EVENT_LABELS,
    }


@router.post("/sessions/{session_id}/checkout", tags=["sessions"])
def checkout(
    session_id: str,
    body: CheckoutRequest,
    db: Session = Depends(get_db),
):
    """
    Run the payment saga for a session.

    Builds a server-authoritative Cart from sku_ids + quantities (prices from
    catalog, NOT from the client). Verifies the cart mandate JWT. Runs policy.
    """
    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if not session:
        raise HTTPException(404, "session not found")
    if session.status not in ("active", "escalated"):
        raise HTTPException(409, f"session is in terminal state: {session.status}")

    if len(body.sku_ids) != len(body.quantities):
        raise HTTPException(400, "sku_ids and quantities must be the same length")

    # ── Build server-authoritative cart ──────────────────────────────────────
    # Emits CATALOG_QUERIED, QUOTE_ISSUED and CART_BUILT as it goes.
    try:
        cart = build_authoritative_cart(
            db=db,
            session_id=session_id,
            sku_ids=body.sku_ids,
            quantities=body.quantities,
            merchant_id=settings.MERCHANT_ID,
        )
    except CartBuildError as exc:
        raise HTTPException(400, str(exc)) from exc

    # ── Decode intent from JWT ────────────────────────────────────────────────
    from jose import jwt as jose_jwt, JWTError
    from jose.constants import ALGORITHMS
    from server.mandate.issuer import buyer_keys
    from server.mandate.schema import IntentMandate

    try:
        intent_claims = jose_jwt.decode(
            body.intent_mandate_jwt,
            buyer_keys.public_pem,
            algorithms=[ALGORITHMS.ES256],
            options={"verify_aud": False},
        )
        intent = IntentMandate(**intent_claims)
    except (JWTError, Exception) as exc:
        raise HTTPException(400, f"invalid intent JWT: {exc}")

    # ── Build transaction history for this buyer ──────────────────────────────
    history = build_buyer_history(db, session.buyer_id)

    # ── Run saga ──────────────────────────────────────────────────────────────
    try:
        if body.mode == "harness":
            result = run_saga_harness(
                db=db,
                session=session,
                intent=intent,
                cart=cart,
                history=history,
            )
        else:
            result = run_saga_demo(
                db=db,
                session=session,
                intent=intent,
                intent_token=body.intent_mandate_jwt,
                cart=cart,
                cart_token=body.cart_mandate_jwt,
                history=history,
            )
    except SagaEscalated as exc:
        session.status = "escalated"
        db.commit()
        return {
            "status": "escalated",
            "escalation_id": exc.escalation_id,
            "reason_code": exc.reason_code,
            "detail": exc.detail,
        }
    except SagaError as exc:
        session.status = "failed"
        db.commit()
        raise HTTPException(422, str(exc))

    return {"status": "ok", **result}


# ──────────────────────────────────────────────────────────────────────────────
# Escalations
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/catalog/categories", tags=["catalog"])
def catalog_categories():
    """
    The categories the catalogue actually contains.

    Read from the catalogue rather than listed in the client, so the launcher
    cannot offer a category no SKU belongs to — an intent authorising a
    category that does not exist produces an empty cart and a session that
    dies at the cart builder.
    """
    from server.mcp.catalog import get_all_skus

    categories = sorted({
        sku["category"] for sku in get_all_skus() if sku.get("category")
    })
    return {"categories": categories}


@router.post("/sessions/{session_id}/run", tags=["sessions"], status_code=202)
def run_session(session_id: str, db: Session = Depends(get_db)):
    """
    Drive an existing session through the pipeline.

    POST /sessions signs the intent and creates the record; it runs nothing.
    A session left there has one INTENT_SIGNED entry and sits until the
    reconciler marks it stale — which is what a launcher wired only to that
    endpoint would produce, and is the same dead end the approve button had.

    This adds no pipeline and no agent. It is an HTTP entry point to the
    machinery `demo/run.py` already drives: BuyerAgent proposes SKUs, the
    catalogue prices them, the policy engine decides, the saga settles.

    Returns 202 immediately and runs on a thread, because the agent calls a
    model and the caller is a dashboard that wants to watch the narrative
    appear rather than block on it.
    """
    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if not session:
        raise HTTPException(404, "session not found")

    already = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session_id,
            LedgerEntry.event_type == EventType.POLICY_EVALUATED.value,
        )
        .first()
    )
    if already:
        raise HTTPException(409, "session has already been run")

    threading.Thread(
        target=_run_session_background,
        args=(session_id,),
        name=f"run-{session_id[:8]}",
        daemon=True,
    ).start()
    return {"status": "running", "session_id": session_id}


def _run_session_background(session_id: str) -> None:
    """
    The pipeline, on its own thread with its own DB session.

    Every failure path closes the session. A launcher that could leave a
    session open forever would be reintroducing the stale-session problem
    through a new door.
    """
    from server.agents.buyer import BuyerAgent
    from server.db.session import SessionLocal
    from server.mandate.schema import IntentMandate
    from server.payments.saga import (
        PaymentMode, SagaEscalated, SagaError, close_session, run_saga,
    )
    from server.policy.history import build_buyer_history

    db = SessionLocal()
    try:
        session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
        if not session:
            return

        signed = (
            db.query(LedgerEntry)
            .filter(
                LedgerEntry.session_id == session_id,
                LedgerEntry.event_type == EventType.INTENT_SIGNED.value,
            )
            .first()
        )
        claims = (signed.payload or {}) if signed else {}
        categories = claims.get("categories") or ["grocery"]
        max_items = claims.get("max_items") or 10

        # Re-sign rather than reconstruct: the mandate object the saga verifies
        # has to be the one whose jti is registered, and POST /sessions already
        # registered it. Signing again under the same session is accepted by the
        # verifier (a session re-presenting its own mandate is not a replay).
        _token, intent = sign_intent(
            buyer_id=session.buyer_id,
            merchant_id=settings.MERCHANT_ID,
            budget_paise=session.budget_paise,
            categories=categories,
            max_items=max_items,
            estimate_paise=int(session.budget_paise * 0.85),
        )
        record_intent_jti(
            jti=intent.jti,
            expires_at=datetime.utcfromtimestamp(intent.exp),
            db=db,
            session_id=session_id,
        )

        agent = BuyerAgent(
            buyer_id=session.buyer_id,
            merchant_id=settings.MERCHANT_ID,
            goal=session.goal or "",
            budget_paise=session.budget_paise,
            categories=categories,
            max_items=max_items,
            estimate_paise=int(session.budget_paise * 0.85),
            stub=settings.STUB_MODE,
        )
        proposal = agent.propose_cart(session_id)

        skus = proposal.get("proposed_skus") or []
        qtys = proposal.get("proposed_quantities") or [1] * len(skus)
        try:
            cart = build_authoritative_cart(
                db=db, session_id=session_id,
                sku_ids=skus, quantities=qtys,
                merchant_id=settings.MERCHANT_ID,
            )
        except CartBuildError as exc:
            close_session(db, session, status="no_cart",
                          reason=f"no_cart: {exc}", final_total_paise=0)
            return

        run_saga(
            db=db, session=session, intent=intent, cart=cart,
            history=build_buyer_history(db, session.buyer_id),
            payments=PaymentMode(settings.PAYMENTS_MODE),
        )
    except SagaEscalated:
        pass          # the escalation is on the ledger; a human decides next
    except SagaError:
        pass          # close_session already ran inside the saga
    except Exception:  # noqa: BLE001 - daemon thread
        log.exception("[run] session %s failed", session_id)
        try:
            session = db.query(SessionRecord).filter(
                SessionRecord.id == session_id
            ).first()
            if session and session.status == "active":
                from server.payments.saga import close_session as _close
                _close(db, session, status="error",
                       reason="agent run failed; see server log")
        except Exception:
            log.exception("[run] could not close %s", session_id)
    finally:
        db.close()


@router.get("/escalations", tags=["escalations"])
def list_escalations(status: str = "pending", db: Session = Depends(get_db)):
    """
    Escalations awaiting (or past) a human decision.

    Each row carries a pre-computed AUTHORISED-vs-PROPOSED diff: the mandate the
    buyer signed against the cart the agent actually built, with the differing
    fields already marked. The comparison is made here so the operator and the
    policy engine are reading the same fields.

    Pass status=all to include resolved escalations.
    """
    query = db.query(EscalationRequest)
    if status != "all":
        if status == "pending":
            # An approval that opened a live payment link is not finished
            # business: someone still has to pay it. Dropping it from the rail
            # the moment it was approved would take the payment URL off screen
            # at the exact moment it became the thing to act on.
            awaiting = {
                e.session_id
                for e in db.query(LedgerEntry)
                .filter(LedgerEntry.event_type == EventType.ORDER_CREATED.value)
                .all()
                if (e.payload or {}).get("awaiting_capture")
            } - {
                e.session_id
                for e in db.query(LedgerEntry)
                .filter(LedgerEntry.event_type.in_([
                    EventType.PAYMENT_CAPTURED.value,
                    EventType.SESSION_CLOSED.value,
                ]))
                .all()
            }
            query = query.filter(
                (EscalationRequest.status == "pending")
                | (
                    (EscalationRequest.status == "approved")
                    & EscalationRequest.session_id.in_(awaiting or [""])
                )
            )
        else:
            query = query.filter(EscalationRequest.status == status)
    rows = query.order_by(EscalationRequest.created_at.desc()).all()

    goals = {
        s.id: s.goal
        for s in db.query(SessionRecord)
        .filter(SessionRecord.id.in_([r.session_id for r in rows] or [""]))
        .all()
    }

    # History-based rules recorded what they examined on their ESCALATED entry,
    # at the moment they fired. It is read back from the chain rather than
    # recomputed, so the reviewer sees the same numbers the engine judged.
    evidence_by_escalation: dict[str, dict] = {}
    for entry in (
        db.query(LedgerEntry)
        .filter(LedgerEntry.event_type == EventType.ESCALATED.value)
        .all()
    ):
        payload = entry.payload or {}
        esc_id = payload.get("escalation_id")
        if esc_id and payload.get("evidence"):
            evidence_by_escalation[esc_id] = payload["evidence"]

    # An approved escalation on the live path has a real payment link waiting to
    # be paid. It is read back off ORDER_CREATED rather than kept on the
    # escalation row: the link is a fact about the order, and the ledger is
    # already where facts about the order live. Reading it here also means the
    # card cannot show a URL that was never actually recorded.
    payment_by_session: dict[str, dict] = {}
    for entry in (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.event_type.in_([
                EventType.ORDER_CREATED.value,
                EventType.PAYMENT_CAPTURED.value,
                EventType.SESSION_CLOSED.value,
            ]),
            LedgerEntry.session_id.in_([r.session_id for r in rows] or [""]),
        )
        .order_by(LedgerEntry.seq.asc())
        .all()
    ):
        payload = entry.payload or {}
        if entry.event_type == EventType.ORDER_CREATED.value:
            if not payload.get("short_url"):
                continue
            payment_by_session[entry.session_id] = {
                "short_url": payload.get("short_url"),
                "qr_url": payload.get("qr_url"),
                "razorpay_order_id": payload.get("razorpay_order_id"),
                "amount_paise": payload.get("amount_paise"),
                "state": "awaiting_capture",
                "seq": entry.seq,
            }
        elif entry.session_id in payment_by_session:
            # Later events resolve the wait; the link stops being actionable.
            captured = entry.event_type == EventType.PAYMENT_CAPTURED.value
            payment_by_session[entry.session_id].update({
                "state": "captured" if captured else "failed",
                "razorpay_payment_id": payload.get("razorpay_payment_id"),
                "detail": payload.get("reason") or payload.get("detail"),
                "resolved_seq": entry.seq,
            })

    cards = [
        {
            "id": r.id,
            "session_id": r.session_id,
            "payment": payment_by_session.get(r.session_id),
            "goal": goals.get(r.session_id),
            "reason_code": r.reason_code,
            "history_based": analytics.is_history_based(r.reason_code),
            "evidence": evidence_by_escalation.get(r.id),
            "detail": r.detail,
            "status": r.status,
            "resolved_by": r.resolved_by,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "cause": analytics.reason_code_cause(r.reason_code),
            "comparison": analytics.rule_comparison(
                r.reason_code, r.intent_snapshot or {}, r.cart_snapshot or {}
            ),
            "intent_snapshot": r.intent_snapshot,
            "cart_snapshot": r.cart_snapshot,
            "diff": analytics.escalation_diff(
                r.intent_snapshot, r.cart_snapshot, r.reason_code
            ),
        }
        for r in rows
    ]

    # Group by rule + merchant.
    #
    # Two NEW_MERCHANT escalations for the same merchant are one situation the
    # operator decides about once, not two unrelated alerts — presented as
    # separate cards it reads like a duplicate-rendering bug. The decision is
    # still per session, because approving one session must never silently
    # approve another.
    groups: dict[tuple[str, str], dict] = {}
    for card in cards:
        merchant = (
            (card["cart_snapshot"] or {}).get("merchant_id")
            or (card["intent_snapshot"] or {}).get("aud")
            or "unknown_merchant"
        )
        key = (card["reason_code"], merchant)
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "key": f"{card['reason_code']}::{merchant}",
                "reason_code": card["reason_code"],
                "merchant_id": merchant,
                "cause": card["cause"],
                "comparison": card.get("comparison"),
                "history_based": card["history_based"],
                "evidence": card["evidence"],
                "diff": card["diff"],
                "created_at": card["created_at"],
                "escalations": [card],
            }
        else:
            group["escalations"].append(card)
            # Keep the earliest raise time as the group's age.
            if card["created_at"] and (
                not group["created_at"] or card["created_at"] < group["created_at"]
            ):
                group["created_at"] = card["created_at"]

    grouped = sorted(
        groups.values(), key=lambda g: (-len(g["escalations"]), g["reason_code"])
    )
    for g in grouped:
        g["session_count"] = len(g["escalations"])

    return {"escalations": cards, "groups": grouped}


@router.post("/sessions/{session_id}/escalations/{esc_id}/approve", tags=["escalations"])
def approve_escalation(
    session_id: str,
    esc_id: str,
    body: EscalationDecisionRequest,
    db: Session = Depends(get_db),
):
    esc = db.query(EscalationRequest).filter(EscalationRequest.id == esc_id).first()
    if not esc or esc.session_id != session_id:
        raise HTTPException(404, "escalation not found")
    if esc.status != "pending":
        raise HTTPException(409, f"escalation already {esc.status}")

    esc.status = "approved"
    esc.resolved_by = body.resolved_by
    esc.resolved_at = datetime.utcnow()

    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if session:
        session.status = "active"

    append(db, session_id=session_id, event_type=EventType.HUMAN_APPROVED, payload={
        "escalation_id": esc_id,
        "resolved_by": body.resolved_by,
        "reason_code": esc.reason_code,
    })
    db.commit()

    response = {"status": "approved", "escalation_id": esc_id}

    # An approval is a decision to let the money move, so this is where the
    # money moves. Every mode settles, not just the live one: approving used to
    # write HUMAN_APPROVED and stop there, leaving the session idle until the
    # reconciler swept it stale a minute later. The operator saw the card
    # vanish and no transaction happen, which made the button decoration.
    # STUB_MODE is not a reason to skip this. It means "make no live API
    # calls", and synthetic settlement makes none — only the live mode needs
    # the network, and that is gated inside _settle_approved.
    if session:
        try:
            response.update(_settle_approved(db, session, esc))
        except SagaError as exc:
            # The approval stands and is already on the ledger; only the
            # settlement failed. Say so rather than reporting a clean approval.
            log.error("[approve] settlement failed for %s: %s", session_id, exc)
            from server.payments.saga import close_session
            close_session(
                db, session, status="failed",
                reason=f"approved, but settlement failed: {exc}",
            )
            response["payment_error"] = str(exc)

    return response


def _settle_approved(
    db: Session, session: SessionRecord, esc: EscalationRequest,
) -> dict:
    """
    Complete the transaction a human just authorised.

    The cart comes from the escalation's own snapshot — the cart a human looked
    at and approved, not one rebuilt afterwards from anything that may have
    moved since.

    Live mode is the only one that has to be split in two: creating the order
    and link is fast and belongs in this request, but waiting for someone to
    pay takes as long as it takes and cannot hold the connection open. The other
    modes settle inline and the session closes before this returns.
    """
    from server.payments.saga import (
        PaymentMode,
        open_live_payment,
        settle_authorised_cart,
    )

    cart = Cart.model_validate(esc.cart_snapshot)
    mode = PaymentMode(settings.PAYMENTS_MODE)
    if mode is PaymentMode.LIVE and settings.STUB_MODE:
        # Asking for live payments with live calls disabled is a contradiction.
        # Falling back silently would settle synthetically while the operator
        # believed real money had moved.
        raise SagaError(
            "PAYMENTS_MODE=live but STUB_MODE is on: refusing to settle, "
            "because a synthetic fallback here would look like a real payment"
        )

    if mode is not PaymentMode.LIVE:
        result = settle_authorised_cart(db, session, cart, payments=mode)
        db.commit()
        return {
            "settled": True,
            "payments": mode.value,
            "razorpay_order_id": result.get("order_id"),
            "razorpay_payment_id": result.get("payment_id"),
            "amount_paise": result.get("total_paise"),
            "replayed_from_fixture": result.get("replayed_from_fixture"),
            "awaiting_capture": False,
        }

    opened = open_live_payment(db, session, cart)
    db.commit()

    # The wait runs on its own thread with its own DB session. Polling a link
    # for up to five minutes inside the request would hold the connection open
    # for the whole of it, and the operator needs the URL now, not afterwards.
    # When a webhook receiver replaces the poller this thread goes away and
    # nothing else here changes.
    threading.Thread(
        target=_await_capture_background,
        args=(session.id, opened),
        name=f"capture-{session.id[:8]}",
        daemon=True,
    ).start()

    return {
        "payment_link_url": opened.short_url,
        "payment_link_id": opened.payment_link_id,
        "qr_url": opened.qr_url,
        "razorpay_order_id": opened.order_id,
        "amount_paise": opened.amount_paise,
        "awaiting_capture": True,
    }


def _await_capture_background(session_id: str, opened) -> None:
    """Poll until paid, record the outcome, close the session."""
    from server.db.session import SessionLocal
    from server.payments.saga import await_live_capture, close_session

    db = SessionLocal()
    try:
        session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
        if not session:
            return
        result = await_live_capture(db, session, opened)
        close_session(
            db, session,
            status="captured" if result.captured else "failed",
            reason=(
                "payment captured"
                if result.captured
                else f"payment not captured: {result.detail or result.status}"
            ),
            final_total_paise=opened.amount_paise if result.captured else None,
        )
    except Exception:                     # noqa: BLE001 - a daemon thread
        # Nothing above can catch this, and a silent death would leave the
        # session sitting active forever. The reconciler's stale sweep is the
        # backstop, but the traceback belongs in the log either way.
        log.exception("[capture] background wait failed for %s", session_id)
    finally:
        db.close()


@router.post("/sessions/{session_id}/escalations/{esc_id}/reject", tags=["escalations"])
def reject_escalation(
    session_id: str,
    esc_id: str,
    body: EscalationDecisionRequest,
    db: Session = Depends(get_db),
):
    esc = db.query(EscalationRequest).filter(EscalationRequest.id == esc_id).first()
    if not esc or esc.session_id != session_id:
        raise HTTPException(404, "escalation not found")
    if esc.status != "pending":
        raise HTTPException(409, f"escalation already {esc.status}")

    esc.status = "rejected"
    esc.resolved_by = body.resolved_by
    esc.resolved_at = datetime.utcnow()

    session = db.query(SessionRecord).filter(SessionRecord.id == session_id).first()
    if session:
        session.status = "failed"

    append(db, session_id=session_id, event_type=EventType.HUMAN_REJECTED, payload={
        "escalation_id": esc_id,
        "resolved_by": body.resolved_by,
    })
    db.commit()
    return {"status": "rejected", "escalation_id": esc_id}


# ──────────────────────────────────────────────────────────────────────────────
# Ledger
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/ledger", tags=["ledger"])
def get_ledger(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    total = db.query(LedgerEntry).count()
    entries = (
        db.query(LedgerEntry)
        .order_by(LedgerEntry.seq)
        .offset(offset)
        .limit(min(limit, 200))
        .all()
    )
    return {
        "total": total,
        "offset": offset,
        "entries": [
            {
                "seq": e.seq,
                "ts": e.ts,
                "session_id": e.session_id,
                "event_type": e.event_type,
                "payload": e.payload,
                "hash": e.hash,
                "prev_hash": e.prev_hash,
                "replayed_from_fixture": e.replayed_from_fixture,
            }
            for e in entries
        ],
    }


@router.get("/ledger/verify", tags=["ledger"])
def ledger_verify(db: Session = Depends(get_db)):
    """
    Re-derive every hash and check chain linkage. Used for the tamper demo and
    polled by the dashboard's chain-integrity badge.

    `entries` is included so the badge can state how many entries were actually
    verified rather than counting rows it happens to have loaded.
    """
    result = verify_chain(db)
    return {**result, "entries": db.query(LedgerEntry).count()}


@router.post("/ledger/tamper", tags=["ledger"])
def ledger_tamper(
    seq: int = Body(...),
    new_payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """
    Mutate a ledger entry in-place. DEMO ONLY.
    Returns 404 if ALLOW_TAMPER=false.
    After calling this, GET /ledger/verify will return {valid: false}.
    """
    if not settings.ALLOW_TAMPER:
        raise HTTPException(404, "tamper endpoint disabled (set ALLOW_TAMPER=true)")

    entry = db.query(LedgerEntry).filter(LedgerEntry.seq == seq).first()
    if not entry:
        raise HTTPException(404, f"ledger entry seq={seq} not found")

    old_payload = dict(entry.payload)
    entry.payload = {**entry.payload, **new_payload}
    db.commit()
    return {
        "status": "tampered",
        "seq": seq,
        "old_payload": old_payload,
        "new_payload": entry.payload,
        "note": "GET /ledger/verify will now return {valid: false}",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/metrics", tags=["metrics"])
def get_metrics(db: Session = Depends(get_db)):
    """
    Every figure the dashboard's metrics strip displays.

    All of it is derived here from session rows and the ledger — the client
    renders these values verbatim and computes nothing. Averages with no
    samples behind them come back as null with a sample count of 0, so the
    dashboard can show a dash instead of inventing a number.
    """
    sessions = db.query(SessionRecord).all()
    entries = db.query(LedgerEntry).order_by(LedgerEntry.seq).all()

    status_counts: dict[str, int] = {}
    for s in sessions:
        status_counts[s.status] = status_counts.get(s.status, 0) + 1

    pending_escalations = db.query(EscalationRequest).filter(
        EscalationRequest.status == "pending"
    ).count()
    resolved_escalations = db.query(EscalationRequest).filter(
        EscalationRequest.status != "pending"
    ).count()

    return {
        "sessions": {
            "total": len(sessions),
            "active": status_counts.get("active", 0),
            "captured": status_counts.get("captured", 0),
            "failed": status_counts.get("failed", 0),
            "error": status_counts.get("error", 0),
            "escalated": status_counts.get("escalated", 0),
            "refunded": status_counts.get("refunded", 0),
            # Owed a refund the provider deferred until settlement. Counted
            # separately from both "refunded" and "failed": the money has not
            # gone back, and the attempt has not been given up on.
            "refund_pending": status_counts.get("refund_pending", 0),
            "refund_failed": status_counts.get("refund_failed", 0),
            "stale": status_counts.get("stale", 0),
        },
        # What the system covers. Counted from the rule list and the attack
        # directory rather than written down, so a rule or attack added later
        # cannot leave a stale figure on the landing screen.
        "coverage": analytics.coverage_counts(),
        "policy": analytics.policy_split(entries, total_sessions=len(sessions)),
        "upsell": analytics.upsell_stats(entries),
        "reason_codes": analytics.reason_code_split(entries),
        "unauthorised_money_movement": analytics.unauthorised_money_movements(entries),
        "latency": analytics.latency_stats(sessions, entries),
        "cost": analytics.cost_stats(entries),
        "ledger": {
            "total_events": len(entries),
            "replayed_entries": sum(1 for e in entries if e.replayed_from_fixture),
        },
        "escalations": {
            "pending": pending_escalations,
            "resolved": resolved_escalations,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/webhook", tags=["webhook"])
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    return await handle_webhook(request, db)


# ──────────────────────────────────────────────────────────────────────────────
# Agent-commerce discovery
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/.well-known/agent-commerce.json", tags=["meta"])
def agent_commerce_manifest():
    """Standard agent-commerce discovery endpoint."""
    return {
        "schema_version": "1.0",
        "merchant_id": settings.MERCHANT_ID,
        "merchant_name": "Tollgate Demo Store",
        "mcp_endpoint": "/mcp",
        "checkout_endpoint": "/sessions",
        "supported_currencies": ["INR"],
        "payment_provider": "razorpay",
        "mandate_algorithm": "ES256",
        "policy_engine": "server-side-deterministic",
        "audit_trail": "/ledger",
    }
