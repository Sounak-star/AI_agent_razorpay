#!/usr/bin/env python
"""
Demo seeder — demo/seed.py

Populates a database with a representative spread of sessions so the dashboard
shows a real workload rather than one scenario run repeatedly.

    python demo/seed.py                 # seed ./tollgate.db
    python demo/seed.py --reset         # wipe first, then seed
    python demo/seed.py --db sqlite:///./demo.db

Every scenario comes from seed/sessions.json and runs through the real pipeline:
the same cart builder, the same policy engine, the same saga. Nothing here
writes a ledger entry directly, so what appears on screen is genuinely the
product of the system under test — including the verdicts. A scenario's
`_expected_outcome` is a label for the operator, never an instruction to the
engine; if the policy engine disagrees with it, the seeder says so and the
engine wins.

The seed set aims to put several terminal states on screen at once: ALLOWED,
DENIED and ESCALATED. REFUNDED is currently unreachable — the recorded refund
attempt was rejected by the provider — and the seeder reports that as MISSING
rather than counting a refund that never happened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_placeholder")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "placeholder_secret")
os.environ.setdefault("ALLOW_TAMPER", "true")

SEED_PATH = Path(__file__).parent.parent / "seed" / "sessions.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed a Tollgate demo database")
    p.add_argument("--db", metavar="URL", help="DATABASE_URL to seed")
    p.add_argument("--reset", action="store_true",
                   help="Delete existing sessions, ledger and escalations first")
    return p.parse_args()


def reset_database(db) -> None:
    """Clear demo state. The chain is rebuilt from seq 1 by the next run."""
    from server.db.models import EscalationRequest, LedgerEntry, MandateJti, SessionRecord

    for model in (LedgerEntry, EscalationRequest, MandateJti, SessionRecord):
        deleted = db.query(model).delete()
        print(f"  cleared {deleted:>4} {model.__tablename__}")
    db.commit()


def seed_scenario(db, scenario: dict) -> dict:
    """Run one scenario end to end and report what the engine actually did."""
    from server.config import settings
    from server.db.models import SessionRecord
    from server.mandate.issuer import sign_intent
    from server.mcp.cart import build_authoritative_cart, record_intent_signed
    from server.ledger.outcomes import matches, session_outcome
    from server.payments.saga import SagaEscalated, SagaError, run_saga_harness
    from server.policy.rules import TxnHistoryItem

    session_id = str(uuid.uuid4())
    goal = scenario.get("goal", "")
    session = SessionRecord(
        id=session_id,
        buyer_id=scenario["buyer_id"],
        merchant_id=settings.MERCHANT_ID,
        goal=goal,
        budget_paise=scenario["budget_paise"],
        status="active",
    )
    db.add(session)
    db.commit()

    _token, intent = sign_intent(
        buyer_id=scenario["buyer_id"],
        merchant_id=settings.MERCHANT_ID,
        budget_paise=scenario["budget_paise"],
        categories=scenario.get("categories", ["grocery"]),
        max_items=scenario.get("max_items", 10),
        estimate_paise=scenario.get("estimate_paise"),
    )

    record_intent_signed(db, session_id, intent)

    cart = build_authoritative_cart(
        db=db,
        session_id=session_id,
        sku_ids=scenario["sku_ids"],
        quantities=scenario.get("quantities", [1] * len(scenario["sku_ids"])),
        merchant_id=settings.MERCHANT_ID,
    )

    # Unless the scenario is specifically testing the new-merchant gate, give
    # the buyer one prior settled transaction so that gate doesn't fire ahead
    # of whatever the scenario is meant to show.
    history: list[TxnHistoryItem] = []
    if not scenario.get("_expect_first_contact", scenario.get("_expect_new_merchant")):
        history.append(TxnHistoryItem(
            session_id="prior_established",
            merchant_id=settings.MERCHANT_ID,
            total_paise=10_000,
            settled=True,
            ts=time.time() - 86_400,
        ))

    # Real settled history for this buyer, same builder the REST path uses.
    from server.policy.history import build_buyer_history
    history.extend(build_buyer_history(db, scenario["buyer_id"]))

    try:
        run_saga_harness(
            db=db,
            session=session,
            intent=intent,
            cart=cart,
            history=history,
            simulate_refund=bool(scenario.get("_simulate_refund")),
            offer_upsell=scenario.get("_offer_upsell", True),
            accept_upsell=scenario.get("_accept_upsell", True),
        )
    except SagaEscalated as exc:
        # Still read back from the ledger: the exception says where control
        # went, the chain says what was recorded, and only the second is
        # evidence.
        recorded = session_outcome(db, session_id)
        return {
            "outcome": recorded["outcome"], "detail": exc.reason_code,
            "session_id": session_id, "recorded": recorded,
        }
    except SagaError as exc:
        recorded = session_outcome(db, session_id)
        return {
            "outcome": recorded["outcome"], "detail": str(exc)[:60],
            "session_id": session_id, "recorded": recorded,
        }

    db.refresh(session)
    # The outcome is whatever the ledger shows, never what the scenario asked
    # for. A scenario carrying _simulate_refund used to be counted as a refund
    # on the strength of that flag alone, which reported "refund 1 ok" while no
    # refund event existed anywhere.
    recorded = session_outcome(db, session_id)
    return {
        "outcome": recorded["outcome"],
        "detail": recorded["detail"],
        "session_id": session_id,
        "recorded": recorded,
    }


def main() -> int:
    args = parse_args()
    if args.db:
        os.environ["DATABASE_URL"] = args.db
    else:
        os.environ.setdefault("DATABASE_URL", "sqlite:///./tollgate.db")

    from server.config import settings
    from server.db.models import Base
    from server.db.session import SessionLocal, engine
    from server.ledger.outcomes import matches
    from server.mandate.issuer import ensure_keypairs

    Base.metadata.create_all(bind=engine)
    ensure_keypairs()

    scenarios = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    print("=" * 68)
    print("  TOLLGATE — demo seed")
    print("=" * 68)
    print(f"  DB: {settings.DATABASE_URL}")
    print(f"  Scenarios: {len(scenarios)}\n")

    db = SessionLocal()
    try:
        if args.reset:
            print("  Resetting:")
            reset_database(db)
            print()

        seen: dict[str, int] = {}
        mismatches: list[str] = []
        results: list[dict] = []

        for scenario in scenarios:
            expected = scenario.get("_expected_outcome", "allow")
            result = seed_scenario(db, scenario)
            results.append(result)
            actual = result["outcome"]
            seen[actual] = seen.get(actual, 0) + 1

            # Judged by comparing the expectation against the ledger, not by
            # string-matching two labels. `matches` is the same function the
            # eval report uses, so the seeder and the report cannot disagree
            # about whether a scenario worked.
            ok, why = matches(expected, result.get("recorded") or {})
            flag = " " if ok else "!"
            if not ok:
                mismatches.append(f"{scenario['id']}: {why}")
            print(
                f" {flag} {scenario['id']:<45} {actual:<9} {result['detail'][:40]}"
            )

        print("\n  Terminal states seeded:")
        for outcome in ("allow", "deny", "escalate", "refund"):
            count = seen.get(outcome, 0)
            mark = "ok " if count else "MISSING"
            print(f"    {outcome:<9} {count:>3}  {mark}")

        # Counted off the ledger, so a refund that was attempted and rejected
        # shows here as what it is rather than disappearing into "allow".
        refund_states: dict[str, int] = {}
        for r in results:
            state = (r.get("recorded") or {}).get("refund_state", "none")
            if state != "none":
                refund_states[state] = refund_states.get(state, 0) + 1
        if refund_states:
            print("\n  Refund attempts recorded:")
            for state, count in sorted(refund_states.items()):
                note = "" if state in ("confirmed", "simulated") else "  <- buyer NOT repaid"
                print(f"    {state:<12} {count:>3}{note}")

        if mismatches:
            # The engine is the authority. A mismatch means the scenario's label
            # is wrong, or a rule changed — either way it needs a human, so it
            # is reported rather than quietly smoothed over.
            print("\n  Scenarios whose outcome differed from their label:")
            for m in mismatches:
                print(f"    ! {m}")

        missing = [o for o in ("allow", "deny", "escalate", "refund") if not seen.get(o)]
        if missing:
            print(f"\n  WARNING: no session ended in: {', '.join(missing)}")
            print("  The dashboard will not show every terminal state.")
            return 1

        print("\n  Seeded. Start the server and open / to view.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
