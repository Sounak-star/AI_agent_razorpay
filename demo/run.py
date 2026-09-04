#!/usr/bin/env python
"""
Demo runner — demo/run.py

Usage:
  python demo/run.py
  python demo/run.py --stub              # no LLM / Razorpay calls
  python demo/run.py --resume-payment <payment_id>

The live demo (no --stub):
  1. Creates a session via the API.
  2. The BuyerAgent calls the configured LLM (see server/agents/llm.py).
  3. The server builds an authoritative cart and runs policy.
  4. Creates a Razorpay Payment Link → prints the URL.
  5. Waits for the user to pay (polls GET /sessions/{id}).
  6. Records order_id / payment_id → evals/fixtures/razorpay_capture.json
     for use by the eval harness (Option B replay).

--resume-payment <payment_id>:
  Re-attaches to an existing payment and triggers the post-payment
  ledger writes without re-creating the order.
  Useful if a demo run was interrupted after payment link creation.

--stub:
  Uses BuyerAgent._stub_propose() and run_saga_harness() — no live calls.
  Good for verifying the pipeline without credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Env defaults — .env overrides these
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_placeholder")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "placeholder_secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./tollgate.db")
os.environ.setdefault("ALLOW_TAMPER", "true")

from server.config import settings
from server.db.models import Base, SessionRecord
from server.db.session import engine, SessionLocal
from server.mandate.issuer import ensure_keypairs, sign_intent, sign_cart
from server.mandate.schema import Cart, CartItem
from server.mandate.verifier import record_intent_jti
from server.mcp.cart import (
    CartBuildError,
    build_authoritative_cart,
    record_intent_signed,
    record_no_cart_built,
)
from server.mcp.catalog import get_authoritative_price, get_sku_by_id
from server.policy.history import build_buyer_history
from server.payments.saga import (
    SagaEscalated,
    SagaError,
    close_session,
    PaymentMode,
    run_saga_demo,
    run_saga_harness,
)

FIXTURE_PATH = Path(__file__).parent.parent / "evals" / "fixtures" / "razorpay_capture.json"
FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)


def parse_args():
    p = argparse.ArgumentParser(description="Tollgate demo runner")
    p.add_argument("--stub", action="store_true", help="Use stubs, no live calls")
    p.add_argument(
        "--payments", choices=["synthetic", "replay", "live"], default="replay",
        help=(
            "Where payment identifiers come from on the stub path. "
            "synthetic generates them locally and touches nothing; "
            "replay uses the recorded capture in evals/fixtures/ when one "
            "exists and falls back to synthetic when it does not; "
            "live creates a real test-mode order and payment link."
        ),
    )
    p.add_argument("--resume-payment", metavar="PAYMENT_ID",
                   help="Resume post-payment recording for an existing payment")
    return p.parse_args()


def setup():
    Base.metadata.create_all(bind=engine)
    ensure_keypairs()


def demo_live(args):
    """Full live demo with real Razorpay and a live LLM."""
    from server.agents.buyer import BuyerAgent, LLMRateLimited

    print("=" * 60)
    print("  TOLLGATE — Agentic Commerce Rail  (LIVE DEMO)")
    print("=" * 60)

    buyer_id = "buyer_demo_001"
    goal = "weekly grocery shopping for a family of 4"
    budget_paise = 2_000_00  # ₹2,000

    agent = BuyerAgent(
        buyer_id=buyer_id,
        merchant_id=settings.MERCHANT_ID,
        goal=goal,
        budget_paise=budget_paise,
        categories=["grocery"],
        max_items=8,
        stub=False,
    )

    db = SessionLocal()
    try:
        # 1. Create session
        import uuid
        session_id = str(uuid.uuid4())
        session = SessionRecord(
            id=session_id,
            buyer_id=buyer_id,
            merchant_id=settings.MERCHANT_ID,
            goal=goal,
            budget_paise=budget_paise,
            status="active",
        )
        db.add(session)
        db.commit()
        print(f"\n[OK] Session created: {session_id}")

        # 2. Sign intent
        intent_token, intent = agent.sign_intent()
        # Record it at the point of signing. This was missing, which is why
        # live sessions opened at CATALOG_QUERIED with no record of what had
        # been authorised — the one entry the whole trail hangs off.
        record_intent_signed(db, session_id, intent)
        print(f"[OK] Intent signed: jti={intent.jti[:12]}...")

        # 3. LLM proposes cart
        #
        # Every failure below closes the session. Previously these paths just
        # returned, and an agent error — a bad API key, an unparseable
        # response, a hallucinated SKU — left the session sitting in "active"
        # forever. The reconciler then correctly swept it to "stale" a minute
        # later, so a run that failed at the model call was indistinguishable
        # on the dashboard from one that simply hung.
        print(f"\n[AI] Asking AI to propose a cart for: '{goal}'...")
        try:
            proposal = agent.propose_cart(session_id)
        except LLMRateLimited as exc:
            # A provider quota, not a fault in this system. Its own terminal
            # state so the trail does not read as a failed decision.
            print(f"\n[LIMIT] Provider refused the call: {exc}")
            print(f"        token budget resets in {exc.reset or 'unknown'}")
            close_session(
                db, session,
                status="rate_limited",
                reason=f"model provider rate limit: {str(exc)[:160]}",
            )
            print("   Session closed as rate_limited — LLM_RATE_LIMITED is on the ledger.")
            return
        except Exception as exc:
            print(f"\n[FAIL] Agent could not propose a cart: {type(exc).__name__}: {exc}")
            close_session(
                db, session,
                status="error",
                reason=f"agent_error: {type(exc).__name__}: {str(exc)[:160]}",
            )
            print("   Session closed as failed — see the ledger for the trail.")
            return
        print(f"   → Proposed SKUs: {proposal['proposed_skus']}")
        print(f"   → Rationale: {proposal.get('rationale', '')[:100]}")

        # 4. Build server-authoritative cart
        #
        # Through the shared builder, like every other path. Building the Cart
        # inline here meant QUOTE_ISSUED and CART_BUILT were never written for a
        # live session: the cart was real and the policy engine judged it, but
        # nothing recorded what was in it, so the operator view could only read
        # "chose 0" from an absent entry.
        valid_skus: list[str] = []
        valid_qtys: list[int] = []
        for sku_id, qty in zip(proposal["proposed_skus"], proposal["proposed_quantities"]):
            if get_authoritative_price(sku_id) is None:
                print(f"   [skip] SKU {sku_id} not in catalog")
                continue
            valid_skus.append(sku_id)
            valid_qtys.append(qty)

        if not valid_skus:
            print("[FAIL] No valid SKUs in proposal — none matched the catalog")
            record_no_cart_built(
                db, session_id,
                reason="proposed SKUs absent from the catalog",
                proposed_skus=proposal.get("proposed_skus") or [],
                rationale=proposal.get("rationale"),
            )
            close_session(
                db, session,
                status="no_cart",
                reason="no_cart: agent proposed only SKUs absent from the catalog",
                final_total_paise=0,
            )
            return

        try:
            cart = build_authoritative_cart(
                db=db,
                session_id=session_id,
                sku_ids=valid_skus,
                quantities=valid_qtys,
                merchant_id=settings.MERCHANT_ID,
            )
        except CartBuildError as exc:
            print(f"[FAIL] Cart could not be built: {exc}")
            close_session(
                db, session,
                status="no_cart",
                reason=f"no_cart: {exc}",
                final_total_paise=0,
            )
            return
        print(f"\n🛒 Server-authoritative cart:")
        for i in cart.items:
            print(f"   {i.sku_id}  {i.name}  ×{i.quantity}  ₹{i.unit_price_paise/100:.2f}")
        print(f"   TOTAL: ₹{cart.total_paise/100:.2f}")

        # 5. Sign cart mandate
        cart_token, _ = sign_cart(intent_jti=intent.jti, cart=cart)

        # 6. Run saga (live payment link)
        #
        # History is read from the database, the same query the REST checkout
        # path uses. This was previously an empty list, which meant the live
        # demo never consulted settlement history at all: FIRST_CONTACT_BUYER
        # fired on every run no matter how many times that buyer had already
        # settled, and DAILY_CAP and VELOCITY could never fire here.
        history = build_buyer_history(db, buyer_id)
        print(f"   Buyer history: {len(history)} prior settled transaction(s)")

        print(f"\n💳 Creating Razorpay Payment Link...")
        try:
            result = run_saga_demo(
                db=db,
                session=session,
                intent=intent,
                intent_token=intent_token,
                cart=cart,
                cart_token=cart_token,
                history=history,
            )
        except SagaEscalated as exc:
            # Deliberately left active: it is waiting on a human, not hung, and
            # the reconciler exempts sessions with a pending escalation.
            print(f"\n[HOLD] ESCALATED: {exc.reason_code} — {exc.detail}")
            print(f"   Escalation ID: {exc.escalation_id}")
            print("   Resolve it in the dashboard's Escalations panel.")
            return
        except SagaError as exc:
            print(f"\n[STOP] {exc}")
            is_policy = str(exc).startswith("policy DENY:") or str(exc).startswith("harness policy DENY:")
            close_session(
                db, session,
                status="failed" if is_policy else "error",
                reason="policy denied" if is_policy else "infrastructure error",
                final_total_paise=cart.total_paise,
            )
            return

        order_id = result.get("order_id", "")
        url = result.get("payment_link_url", "")

        print(f"\n[OK] Payment Link created!")
        print(f"\n  ┌{'─'*56}┐")
        print(f"  │  PAY HERE: {url:<44}  │")
        print(f"  └{'─'*56}┘")
        print(f"\n  Order ID: {order_id}")
        print(f"\n  After payment, run:")
        print(f"    python demo/run.py --resume-payment <payment_id>")
        print(f"\n  (the payment_id appears in the Razorpay dashboard / webhook)\n")

    finally:
        db.close()


def demo_stub(args):
    """Stub demo — no Razorpay / LLM calls."""
    from server.agents.buyer import BuyerAgent
    import uuid

    print("=" * 60)
    print("  TOLLGATE — Agentic Commerce Rail  (STUB DEMO)")
    print("=" * 60)

    # Driven from the seed file rather than hardcoded, so this path exercises
    # the same scenario definitions the seeder and harness use. For a full
    # spread of sessions covering all four terminal states, use
    # `python demo/seed.py --reset`.
    scenario = json.loads(
        (Path(__file__).parent.parent / "seed" / "sessions.json").read_text(encoding="utf-8")
    )[0]

    buyer_id = scenario["buyer_id"]
    goal = scenario["goal"]
    budget_paise = scenario["budget_paise"]

    db = SessionLocal()
    try:
        session_id = str(uuid.uuid4())
        session = SessionRecord(
            id=session_id,
            buyer_id=buyer_id,
            merchant_id=settings.MERCHANT_ID,
            goal=goal,
            budget_paise=budget_paise,
            status="active",
        )
        db.add(session)
        db.commit()

        _, intent = sign_intent(
            buyer_id=buyer_id,
            merchant_id=settings.MERCHANT_ID,
            budget_paise=budget_paise,
            categories=scenario.get("categories", ["grocery"]),
            max_items=scenario.get("max_items", 6),
            estimate_paise=scenario.get("estimate_paise", budget_paise // 2),
        )
        record_intent_signed(db, session_id, intent)

        # Built through the shared builder: prices come from the catalog, and
        # the catalog lookup, quote and cart are recorded in the ledger. The
        # previous inline cart hardcoded names and prices that had already
        # drifted from the catalog it was meant to mirror.
        cart = build_authoritative_cart(
            db=db,
            session_id=session_id,
            sku_ids=scenario["sku_ids"],
            quantities=scenario.get("quantities", [1] * len(scenario["sku_ids"])),
            merchant_id=settings.MERCHANT_ID,
        )

        print(f"\n  Session: {session_id}")
        print(f"\n  Cart total: Rs. {cart.total_paise/100:.2f}")
        
        import time
        from server.policy.rules import TxnHistoryItem
        result = run_saga_harness(
            db=db,
            session=session,
            intent=intent,
            cart=cart,
            payments=PaymentMode(args.payments),
            history=[TxnHistoryItem(
                session_id="fake_past_session",
                merchant_id=settings.MERCHANT_ID,
                total_paise=10000,
                settled=True,
                ts=time.time() - 86400,
            )],
        )

        print(f"\n[OK] Harness saga result:")
        print(f"   replayed_from_fixture = {result.get('replayed_from_fixture')}")
        print(f"   payment_id = {result.get('payment_id')}")
        print(f"\n[LEDGER] Ledger entries:")

        from server.db.models import LedgerEntry
        entries = db.query(LedgerEntry).filter(
            LedgerEntry.session_id == session_id
        ).order_by(LedgerEntry.seq).all()
        for e in entries:
            print(f"   [{e.seq}] {e.event_type} | hash={e.hash[:12]}...")

    finally:
        db.close()


def resume_payment(payment_id: str):
    """Attach an existing payment to a session and record the fixture."""
    db = SessionLocal()
    try:
        from server.db.models import LedgerEntry
        from server.payments.razorpay_client import fetch_payment
        from server.ledger.chain import append
        from server.ledger.events import EventType

        payment = fetch_payment(payment_id)
        order_id = payment.get("order_id", "")

        session = db.query(SessionRecord).filter(
            SessionRecord.razorpay_order_id == order_id
        ).first()

        if not session:
            print(f"❌ No session found for order_id={order_id}")
            return

        session.razorpay_payment_id = payment_id
        session.status = "captured"
        db.commit()

        append(db, session.id, EventType.PAYMENT_CAPTURED, {
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": order_id,
            "amount": payment.get("amount"),
            "method": payment.get("method"),
            "captured": True,
            "resumed": True,
        })

        print(f"[OK] Payment {payment_id} attached to session {session.id}")

        # Write fixture
        fixture = {
            "order_id": order_id,
            "payment_id": payment_id,
            "refund_id": None,
            "session_id": session.id,
            "amount": payment.get("amount"),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        FIXTURE_PATH.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        print(f"[OK] Fixture written to {FIXTURE_PATH}")

    finally:
        db.close()


def main():
    args = parse_args()
    setup()

    if args.resume_payment:
        resume_payment(args.resume_payment)
    elif args.stub:
        demo_stub(args)
    else:
        demo_live(args)


if __name__ == "__main__":
    main()
