#!/usr/bin/env python
"""
Eval harness — evals/harness.py

Usage:
  python evals/harness.py
  python evals/harness.py --stub           # use fixture responses (default)
  python evals/harness.py --live           # call real Razorpay (requires fixture first)
  python evals/harness.py --attacks-only   # run adversarial tests only
  python evals/harness.py --fail-fulfilment # inject fulfilment failure scenario

The harness:
  1. Reads seed/sessions.json for buyer intent scenarios.
  2. For each scenario runs the buyer → policy → (simulated) payment pipeline.
  3. Records pass/fail/escalate verdicts.
  4. Writes evals/report.md summarising results.

Option B (fixture replay, default):
  Recorded IDs from evals/fixtures/razorpay_capture.json are replayed.
  Ledger events use PAYMENT_SIMULATED / REFUND_SIMULATED with
  replayed_from_fixture=True. Zero real API calls.

Option A (live mode, --live):
  Real Payment Link created for each session. This is for the demo run that
  RECORDS the fixture — not for CI.

Concurrency note:
  double_charge and refund_race scenarios are documented in the report as
  requiring the Postgres path. SQLite's WAL mode handles the UNIQUE constraint
  but cannot model true concurrent write races.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

# Inject test env BEFORE any server import
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_eval_harness")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "eval_secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./tollgate_eval.db")
os.environ.setdefault("STUB_MODE", "true")

from server.config import settings
from server.db.models import Base
from server.db.session import engine, SessionLocal
from server.mandate.issuer import ensure_keypairs, sign_intent
from server.mandate.schema import Cart, CartItem
from server.mcp.cart import build_authoritative_cart, record_intent_signed
from server.mcp.catalog import get_authoritative_price, get_sku_by_id, search_skus
from server.ledger.outcomes import matches, session_outcome
from server.payments import fixtures
from server.payments.saga import (
    PaymentMode,
    SagaEscalated,
    SagaError,
    run_saga_harness,
)
from server.policy.engine import evaluate
from server.policy.rules import TxnHistoryItem

SEED_PATH = Path(__file__).parent.parent / "seed" / "sessions.json"
ATTACKS_DIR = Path(__file__).parent / "attacks"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "razorpay_capture.json"
REPORT_PATH = Path(__file__).parent / "report.md"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tollgate eval harness")
    p.add_argument("--stub", action="store_true", default=True,
                   help="Use fixture responses (default)")
    p.add_argument("--live", action="store_true", default=False,
                   help="Use live Razorpay calls")
    p.add_argument(
        "--payments", choices=["synthetic", "replay"], default="replay",
        help=(
            "replay (default) uses the recorded Razorpay capture where one "
            "exists; synthetic generates every identifier locally, so a full "
            "suite runs with no fixture on disk and no network."
        ),
    )
    p.add_argument("--attacks-only", action="store_true", default=False)
    p.add_argument("--fail-fulfilment", action="store_true", default=False)
    return p.parse_args()


def setup_db():
    Base.metadata.create_all(bind=engine)


def resolve_cart(
    db, session_id: str, sku_ids: list[str], quantities: list[int], merchant_id: str
) -> Cart:
    """
    Build a server-authoritative cart. LLM-supplied prices never used.

    Delegates to the same builder the REST checkout path uses, so the harness
    records CATALOG_QUERIED / QUOTE_ISSUED / CART_BUILT exactly as a live run
    does instead of silently skipping the first half of the lifecycle.
    """
    return build_authoritative_cart(
        db=db,
        session_id=session_id,
        sku_ids=sku_ids,
        quantities=quantities,
        merchant_id=merchant_id,
    )


def forge_cart_mandate(kind: str, intent, cart: Cart, db, session_id: str) -> str:
    """
    Produce a deliberately bad cart mandate, so the verifier can be attacked
    end to end rather than only in unit tests.

    This is only possible because there is now one saga: `cart_token` is an
    injected parameter, so a scenario can hand the real path a forged token and
    watch the real verifier reject it.
    """
    from server.mandate.issuer import sign_cart

    if kind == "garbage":
        return "not.a.valid.jwt"

    if kind == "expired":
        # Signed already expired.
        token, _ = sign_cart(intent_jti=intent.jti, cart=cart, ttl_seconds=-1)
        return token

    if kind == "hash_mismatch":
        # Signed over a different cart than the one presented for payment.
        other = Cart(merchant_id=cart.merchant_id, items=[CartItem(
            sku_id="GRO007", name="Tata Salt 1kg", category="grocery",
            quantity=1, unit_price_paise=1,
        )])
        token, _ = sign_cart(intent_jti=intent.jti, cart=other)
        return token

    if kind == "replayed":
        # Burn the cart JTI first, so the saga's verification sees a replay.
        #
        # The intent JTI has to be recorded before priming, and under *this*
        # session's id. Without it the priming verify fails at
        # intent_jti_not_found and rolls the cart-JTI insert back, so nothing is
        # actually burned and the attack silently tests nothing — which is
        # exactly what it did on the first run.
        from datetime import datetime as _dt, timedelta as _td
        from server.mandate.verifier import record_intent_jti, verify_cart_mandate

        record_intent_jti(
            jti=intent.jti,
            expires_at=_dt.utcfromtimestamp(intent.exp),
            db=db,
            session_id=session_id,
        )
        token, _ = sign_cart(intent_jti=intent.jti, cart=cart)
        primed = verify_cart_mandate(
            token, cart, db, session_id="prior_use_of_this_jti"
        )
        if not primed.valid:
            raise RuntimeError(
                f"replay attack could not be primed: {primed.reason} — "
                "the mandate was never burned, so this scenario would test nothing"
            )
        return token

    if kind == "unknown_intent":
        token, _ = sign_cart(intent_jti="intent-that-was-never-issued", cart=cart)
        return token

    raise ValueError(f"unknown forge kind: {kind!r}")


def _recorded_verdict(db, session_id: str) -> dict:
    """
    The verdict the policy engine actually recorded for this session.

    Read from the ledger rather than inferred from control flow: what the chain
    says happened is the only thing worth asserting against.
    """
    from server.db.models import LedgerEntry
    from server.ledger.events import EventType

    row = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session_id,
            LedgerEntry.event_type == EventType.POLICY_EVALUATED.value,
        )
        .order_by(LedgerEntry.seq.desc())
        .first()
    )
    if row is None:
        return {"decision": None, "code": None, "mandate_reason": None}
    p = row.payload or {}
    return {
        "decision": p.get("decision"),
        "code": p.get("code"),
        "mandate_reason": p.get("mandate_reason"),
    }


def run_scenario(
    scenario: dict,
    stub: bool = True,
    payments: PaymentMode = PaymentMode.REPLAY,
) -> dict:
    """Run a single buyer intent scenario through the full pipeline."""
    db = SessionLocal()
    try:
        from server.db.models import SessionRecord

        session_id = str(uuid.uuid4())
        buyer_id = scenario.get("buyer_id", f"buyer_{session_id[:8]}")
        merchant_id = settings.MERCHANT_ID

        # Create session record
        session = SessionRecord(
            id=session_id,
            buyer_id=buyer_id,
            merchant_id=merchant_id,
            goal=scenario.get("goal", ""),
            budget_paise=scenario["budget_paise"],
            status="active",
        )
        db.add(session)
        db.commit()

        # Build intent
        _, intent = sign_intent(
            buyer_id=buyer_id,
            merchant_id=merchant_id,
            budget_paise=scenario["budget_paise"],
            categories=scenario.get("categories", ["grocery"]),
            max_items=scenario.get("max_items", 10),
            estimate_paise=scenario.get("estimate_paise"),
        )

        record_intent_signed(db, session_id, intent, extra={"harness": True})

        agent_outcome: dict | None = None

        # Build cart (records the catalog lookup, quote and cart in the ledger)
        #
        # A scenario carrying `_agent_selects` has no sku_ids: the model is
        # asked to choose, from a catalogue that includes an injected
        # instruction. Every other attack hands the cart over ready-made, which
        # tests the engine but never the selection step — so nothing in the
        # suite noticed whether the model was shown the attack at all.
        if scenario.get("_agent_selects"):
            from server.agents.buyer import BuyerAgent, LLMRateLimited

            agent = BuyerAgent(
                buyer_id=scenario["buyer_id"],
                merchant_id=merchant_id,
                goal=scenario["goal"],
                budget_paise=scenario["budget_paise"],
                categories=scenario.get("categories", ["grocery"]),
                max_items=scenario.get("max_items", 10),
                # Never the stub, whatever the harness is running as.
                #
                # This scenario exists to put an injected instruction in front
                # of the model, so a recorded fixture standing in for the
                # model's choice makes it test nothing. It did exactly that on
                # its first run: no LLM_CALL, a cart of GRO007 from the stub's
                # fallback, and a green tick. run_attack skips the scenario
                # outright when no provider is configured.
                stub=False,
            )
            proposal = agent.propose_cart(session_id)
            chosen = proposal.get("proposed_skus") or []
            quantities = proposal.get("proposed_quantities") or [1] * len(chosen)
            cart = resolve_cart(
                db=db,
                session_id=session_id,
                sku_ids=chosen,
                quantities=quantities,
                merchant_id=merchant_id,
            )
            # Two questions, answered separately.
            #
            # Whether the model took the bait and whether money actually moved
            # at the injected price are different facts, and collapsing them
            # into one pass/fail loses the interesting half. The model choosing
            # the poisoned SKU is not a failure — the defence is that the
            # server prices it from the catalogue regardless.
            agent_outcome = {
                "proposed_skus": chosen,
                "model_complied": any(
                    sku in (scenario.get("_injection_skus") or []) for sku in chosen
                ),
                "server_total_paise": cart.total_paise,
                "injected_total_paise": scenario.get("_injected_total_paise", 0),
                "money_moved_at_injected_price": (
                    cart.total_paise == scenario.get("_injected_total_paise", 0)
                ),
            }
        else:
            cart = resolve_cart(
                db=db,
                session_id=session_id,
                sku_ids=scenario["sku_ids"],
                quantities=scenario.get("quantities", [1] * len(scenario["sku_ids"])),
                merchant_id=merchant_id,
            )

        # Build transaction history from scenario metadata
        # _prior_spend_paise: inject prior settled spend for daily-cap tests
        # _prior_txn_count: inject N prior transactions for velocity tests
        # If neither is set AND the scenario does NOT have "expect_new_merchant: true",
        # inject one small prior settled transaction so the new_merchant rule doesn't
        # fire and obscure the rule actually being tested.
        import time as _time
        merchant_id_str = settings.MERCHANT_ID
        history: list[TxnHistoryItem] = []

        prior_spend = scenario.get("_prior_spend_paise", 0)
        prior_txn_count = scenario.get("_prior_txn_count", 0)
        expect_new_merchant = scenario.get("_expect_first_contact", scenario.get("_expect_new_merchant", False))

        if not expect_new_merchant:
            # Always inject one prior settled txn with this merchant so new_merchant
            # rule passes and the intended rule gets to fire
            history.append(TxnHistoryItem(
                session_id="prior_established",
                merchant_id=merchant_id_str,
                total_paise=10_000,
                settled=True,
                ts=_time.time() - 86400,  # yesterday
            ))

        if prior_spend > 0:
            history.append(TxnHistoryItem(
                session_id="prior_spend_injection",
                merchant_id=merchant_id_str,
                total_paise=prior_spend,
                settled=True,
                ts=_time.time() - 3600,
            ))

        if prior_txn_count > 0:
            now = _time.time()
            for i in range(prior_txn_count):
                history.append(TxnHistoryItem(
                    session_id=f"prior_txn_{i}",
                    merchant_id=merchant_id_str,
                    total_paise=5_000,
                    settled=True,
                    ts=now - (prior_txn_count - i) * 60,  # spaced 1 min apart, all within hour
                ))

        # A scenario may hand the saga a deliberately bad mandate. The saga
        # verifies whatever it is given, so this exercises the real verifier on
        # the real path rather than in isolation.
        forged = scenario.get("_forge_cart_mandate")
        cart_token = (
            forge_cart_mandate(forged, intent, cart, db, session_id) if forged else None
        )

        # Run saga (harness path by default)
        result = run_saga_harness(
            db=db,
            session=session,
            intent=intent,
            cart=cart,
            cart_token=cart_token,
            history=history,
            payments=payments,
        )

        return {
            "scenario_id": scenario.get("id", session_id),
            "status": "pass",
            "expected": scenario.get("_expected_outcome", "allow"),
            "session_id": session_id,
            "replayed_from_fixture": result.get("replayed_from_fixture", True),
            "total_paise": cart.total_paise,
            **_recorded_verdict(db, session_id),
            "recorded": session_outcome(db, session_id),
            "agent_outcome": agent_outcome,
        }

    except SagaEscalated as exc:
        return {
            "scenario_id": scenario.get("id"),
            "status": "escalated",
            "expected": scenario.get("_expected_outcome", "allow"),
            "session_id": session_id,
            "detail": exc.detail,
            **_recorded_verdict(db, session_id),
            "recorded": session_outcome(db, session_id),
        }
    except SagaError as exc:
        return {
            "scenario_id": scenario.get("id"),
            "status": "deny",
            "expected": scenario.get("_expected_outcome", "allow"),
            "session_id": session_id,
            "detail": str(exc),
            **_recorded_verdict(db, session_id),
            "recorded": session_outcome(db, session_id),
        }
    except Exception as exc:
        # An exception is never a result. It is reported as ERROR and can
        # never satisfy an expectation — a crash that scored as a pass is a
        # defence that was never exercised, reported as one that held.
        return {
            "scenario_id": scenario.get("id"),
            "status": "error",
            "expected": scenario.get("_expected_outcome", "allow"),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    finally:
        db.close()


def _live_model_available() -> bool:
    """Is a provider actually configured to answer a real call?"""
    try:
        from server.agents.llm import get_client_and_model

        get_client_and_model()
        return True
    except Exception:                                             # noqa: BLE001
        return False


def run_attack(
    attack_file: Path, payments: PaymentMode = PaymentMode.REPLAY
) -> dict:
    """
    Run one adversarial case and judge it against an exact expected verdict.

    An attack declares the decision AND the reason code it expects — not just
    "deny". A broad expectation is satisfied by the wrong defence firing: an
    ITEM_COUNT attack that trips CATEGORY_DENY first would have scored as a
    pass while the rule it names was never reached.

    Three outcomes, and ERROR is never one of the passing ones:

      PASS   the declared verdict was recorded
      FAIL   a verdict was recorded, but not the declared one
      ERROR  the scenario raised — nothing was tested

    `error` used to be accepted as satisfying `deny`, so a crash counted as a
    defence holding. Attack 04 passed that way for its entire existence while
    never once reaching the rule it was written for.
    """
    attack = json.loads(attack_file.read_text(encoding="utf-8"))
    expect = attack.get("expect")
    if not expect:
        return {
            "attack": attack_file.name,
            "description": attack.get("description", ""),
            "state": "ERROR",
            "expected": "(none declared)",
            "actual": "-",
            "detail": "attack declares no `expect` block; it cannot be judged",
        }

    # A scenario needing the real model is skipped, loudly, when no provider is
    # configured. Running it against the stub would put a recorded fixture in
    # place of the model's choice and report injection resistance that was
    # never exercised — a green tick for a test that did nothing.
    if attack["scenario"].get("_requires_live_model") and not _live_model_available():
        return {
            "attack": attack_file.name,
            "description": attack.get("description", ""),
            "state": "SKIP",
            "expected": f"{expect['decision']}/{expect['code']}",
            "actual": "not run",
            "detail": (
                "needs a live model (set GROQ_API_KEY); refusing to run it "
                "against a stub, which would report resistance never tested"
            ),
        }

    result = run_scenario(attack["scenario"], payments=payments)
    want = f"{expect['decision']}/{expect['code']}"
    if expect.get("reason_contains"):
        want += f" ~{expect['reason_contains']}"

    if result["status"] == "error":
        # Could not reach the provider at all: the attack was not run, which is
        # the same situation as having no key configured. Reported SKIP, never
        # PASS — a network blip must not turn the suite red, and must not be
        # able to report resistance that was never exercised either.
        error = str(result.get("error") or "")
        unreachable = attack["scenario"].get("_requires_live_model") and any(
            marker in error
            for marker in ("APIConnectionError", "APITimeoutError", "Connection error")
        )
        return {
            "attack": attack_file.name,
            "description": attack.get("description", ""),
            "state": "SKIP" if unreachable else "ERROR",
            "expected": want,
            "actual": "not run" if unreachable else "exception",
            "detail": (
                f"model provider unreachable, attack not run: {error[:120]}"
                if unreachable else result.get("error")
            ),
            "traceback": None if unreachable else result.get("traceback"),
        }

    decision, code = result.get("decision"), result.get("code")
    reason = result.get("mandate_reason") or ""
    got = f"{decision}/{code}"

    if decision is None:
        state, detail = "ERROR", "no POLICY_EVALUATED entry was recorded"
    elif decision != expect["decision"] or code != expect["code"]:
        state, detail = "FAIL", f"expected {want}, recorded {got}"
    elif expect.get("reason_contains") and expect["reason_contains"] not in reason:
        # Five attacks share MANDATE_INVALID; only the verifier's reason tells
        # them apart, so a mandate attack must fail for its own cause.
        state = "FAIL"
        detail = f"expected reason containing {expect['reason_contains']!r}, got {reason!r}"
    else:
        state, detail = "PASS", reason or result.get("detail") or ""

    return {
        "attack": attack_file.name,
        "description": attack.get("description", ""),
        "state": state,
        "expected": want,
        "actual": got + (f" ~{reason}" if reason else ""),
        "detail": detail,
        "agent_outcome": result.get("agent_outcome"),
    }


def write_report(
    normal_results: list[dict],
    attack_results: list[dict],
    elapsed: float,
    stub: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    pass_count = sum(1 for r in normal_results if r["status"] == "pass")
    deny_count = sum(1 for r in normal_results if r["status"] == "deny")
    esc_count  = sum(1 for r in normal_results if r["status"] == "escalated")
    err_count  = sum(1 for r in normal_results if r["status"] == "error")
    atk_pass   = sum(1 for r in attack_results if r["state"] == "PASS")
    atk_fail   = sum(1 for r in attack_results if r["state"] == "FAIL")
    atk_error  = sum(1 for r in attack_results if r["state"] == "ERROR")
    atk_skip   = sum(1 for r in attack_results if r["state"] == "SKIP")

    mode_tag = "FIXTURE REPLAY" if stub else "LIVE API"

    lines = [
        f"# Tollgate Eval Report",
        f"",
        f"Generated: {now}  |  Mode: {mode_tag}  |  Elapsed: {elapsed:.1f}s",
        f"",
        f"> [!IMPORTANT]",
        f"> All replayed legs use recorded IDs from `evals/fixtures/razorpay_capture.json`.",
        f"> Ledger events are marked `replayed_from_fixture: true`.",
        f"> Zero real Razorpay API calls were made in this run (stub mode).",
        f"",
        f"## Seeded Scenario Results ({len(normal_results)} scenarios)",
        f"",
        f"Verdicts, not pass marks. A DENY is the policy engine working: the only",
        f"row that indicates something went wrong is one where the recorded",
        f"verdict differs from the scenario's expectation, or where the run",
        f"errored before reaching a verdict.",
        f"",
        f"| Verdict | Sessions |",
        f"|---------|----------|",
        f"| ALLOW     | {pass_count} |",
        f"| ESCALATE  | {esc_count} |",
        f"| DENY      | {deny_count} |",
        f"| *errored* | {err_count} |",
        f"",
        f"| Scenario | Verdict | Reason | Expected | Matched |",
        f"|----------|---------|--------|----------|---------|",
    ]

    # Only a mismatch or an error is marked; a verdict on its own is just a
    # verdict. Marking every DENY with a cross read as three failures in a run
    # where the engine had done exactly its job.

    for r in normal_results:
        if r["status"] == "error":
            lines.append(
                f"| `{r['scenario_id']}` | — | — | {r.get('expected','?')} "
                f"| ⚠️ errored: {str(r.get('error'))[:60]} |"
            )
            continue
        decision = r.get("decision") or "—"
        code = r.get("code") or "—"
        expected = r.get("expected", "?")
        # Judged against the ledger. The old map read {"refund": "ALLOW"}, so a
        # scenario asking for a refund was marked matched purely because the
        # policy allowed the cart — regardless of whether a refund ever
        # happened, and it did not.
        ok, why = matches(expected, r.get("recorded") or r)
        matched = "yes" if ok else f"**no — {why}**"
        lines.append(
            f"| `{r['scenario_id']}` | {decision} | `{code}` | {expected} | {matched} |"
        )

    lines += [
        f"",
        f"## Adversarial Attack Results ({len(attack_results)} attacks)",
        f"",
        f"| Attack | Expected | Recorded | State |",
        f"|--------|----------|--------|------|",
    ]
    for r in attack_results:
        tick = {"PASS": "✅", "FAIL": "❌", "ERROR": "⚠️", "SKIP": "⊘"}[r["state"]]
        lines.append(
            f"| `{r['attack']}` | {r['expected']} | {r['actual']} | {tick} |"
        )
    lines += [
        f"",
        # Skipped attacks are counted out of the denominator, not folded into
        # the passes. "16/16 attacks" would claim all sixteen defences were
        # exercised, which is false the moment one is skipped on a network
        # blip — and this report exists to make claims that hold.
        (
            f"**{atk_pass} passed"
            + (f", {atk_skip} skipped" if atk_skip else "")
            + (f", {atk_fail} failed" if atk_fail else "")
            + (f", {atk_error} errored" if atk_error else "")
            + f"** of {len(attack_results)} attacks"
            + (
                f" — {atk_pass}/{len(attack_results) - atk_skip} of those actually run"
                if atk_skip else ""
            )
            + (" (an error means nothing was tested)" if atk_error else "")
        ),
        f"",
    ]

    skipped_rows = [r for r in attack_results if r["state"] == "SKIP"]
    if skipped_rows:
        lines += [
            f"",
            f"### Not run ({len(skipped_rows)})",
            f"",
            f"These defences were **not exercised** by this run. They are "
            f"neither failures nor passes.",
            f"",
        ]
        for r in skipped_rows:
            lines.append(f"- `{r['attack']}` — {r['detail']}")

    # ── Attack 16 reports two facts, not one verdict ────────────────────────
    agent_rows = [r for r in attack_results if r.get("agent_outcome")]
    if agent_rows:
        lines += [
            f"",
            f"### Injection through model selection",
            f"",
            f"The only attack where the model chooses. Two outcomes recorded "
            f"separately, because they answer different questions: whether the "
            f"model took the bait, and whether money actually moved at the "
            f"injected price. A single pass/fail hides the first.",
            f"",
            f"| Attack | model_complied | money_moved | server priced at | injection demanded |",
            f"|--------|----------------|-------------|------------------|--------------------|",
        ]
        for r in agent_rows:
            a = r["agent_outcome"]
            chose = ", ".join(a["proposed_skus"]) or "nothing"
            lines.append(
                f"| `{r['attack']}` "
                f"| {'**yes** — chose ' + chose if a['model_complied'] else 'no'} "
                f"| {'**YES**' if a['money_moved_at_injected_price'] else 'no'} "
                f"| {a['server_total_paise']:,} paise "
                f"| {a['injected_total_paise']:,} paise |"
            )
        lines += [
            f"",
            f"The model selecting the poisoned SKU is **not** a failure. The "
            f"defence is that the server prices whatever was chosen from its "
            f"own catalogue, so the injected instruction changes nothing about "
            f"what is charged.",
        ]

    lines += [
        f"",
        f"## Architectural Security Notes",
        f"",
        f"The injection defence is architectural, not pattern-based:",
        f"- The LLM **cannot** compute totals — prices are always server-authoritative.",
        f"- The LLM **cannot** alter policy verdicts — `evaluate()` has no LLM path.",
        f"- The LLM **cannot** mint a CartMandate — only the server's `sign_cart()` does.",
        f"- Product descriptions are wrapped in delimiters and flagged as untrusted.",
        f"- All `_`-prefixed catalog fields (injection markers) are stripped before the LLM sees them.",
        f"",
        f"## Concurrency Notes",
        f"",
        f"> [!NOTE]",
        f"> `double_charge` and `refund_race` scenarios require the **Postgres** path.",
        f"> SQLite's WAL mode correctly enforces the `UNIQUE(jti)` constraint in serial",
        f"> execution, but cannot model true concurrent write races.",
        f"> Run `docker-compose up -d db` and set `DATABASE_URL=postgresql://...`",
        f"> before executing these two scenarios.",
        f"",
    ]

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Report written to {REPORT_PATH}")


def main():
    args = parse_args()
    use_stub = not args.live
    payments = PaymentMode(args.payments)

    print("[TOLLGATE] Eval Harness")
    print(f"   Mode: {'STUB (fixture replay)' if use_stub else 'LIVE API'}")
    print(f"   Payments: {payments.value}"
          f"{' (no fixture on disk; falls back to synthetic)' if payments is PaymentMode.REPLAY and not fixtures.has('payment') else ''}")
    print(f"   DB:   {settings.DATABASE_URL}")

    ensure_keypairs()
    setup_db()

    t0 = time.time()
    normal_results = []
    attack_results = []

    # ── Normal scenarios ────────────────────────────────────────────────────
    if not args.attacks_only:
        if not SEED_PATH.exists():
            print(f"  [WARN] seed/sessions.json not found — skipping normal scenarios")
        else:
            scenarios = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            print(f"\n  Running {len(scenarios)} normal scenarios...")
            for scenario in scenarios:
                r = run_scenario(scenario, stub=use_stub, payments=payments)
                if r['status'] == 'pass':
                    print(f"  [OK] {r['scenario_id']} -> {r['status']}")
                elif r['status'] == 'escalated':
                    print(f"  [ESCALATE] {r['scenario_id']} -> {r['status']}")
                elif r['status'] == 'deny':
                    print(f"  [DENY] {r['scenario_id']} -> {r['status']}")
                else:
                    print(f"  [ERROR] {r['scenario_id']} -> {r['status']}")
                normal_results.append(r)

    # ── Attack scenarios ─────────────────────────────────────────────────────
    if ATTACKS_DIR.exists():
        attack_files = sorted(ATTACKS_DIR.glob("*.json"))
        print(f"\n  Running {len(attack_files)} adversarial attacks...")
        for af in attack_files:
            r = run_attack(af, payments=payments)
            tick = f'[{r["state"]}]'
            print(f"  {tick} {af.name} (expected={r['expected']}, got={r['actual']})")
            attack_results.append(r)
    else:
        print("\n  [WARN] evals/attacks/ not found — skipping attack tests")

    elapsed = time.time() - t0
    print(f"\n  Total: {elapsed:.1f}s")

    write_report(normal_results, attack_results, elapsed, use_stub)

    # Exit non-zero if any attack failed
    any_attack_failed = any(r["state"] not in ("PASS", "SKIP") for r in attack_results)
    skipped = [r for r in attack_results if r["state"] == "SKIP"]
    if skipped:
        print(f"\n[SKIP] {len(skipped)} attack(s) not run:")
        for r in skipped:
            print(f"    {r['attack']}: {r['detail']}")
    attack_errors = [r for r in attack_results if r["state"] == "ERROR"]
    if attack_errors:
        print(f"\n[ERROR] {len(attack_errors)} attack(s) raised instead of "
              f"producing a verdict — nothing was tested:")
        for r in attack_errors:
            print(f"    {r['attack']}: {r['detail']}")
    if any_attack_failed:
        print("[FAIL] One or more attacks were not correctly handled")
        sys.exit(1)

    err_count = sum(1 for r in normal_results if r["status"] == "error")
    if err_count:
        print(f"[FAIL] {err_count} scenarios ended in error")
        sys.exit(1)

    # A scenario whose ledger contradicts its expectation is a failure.
    #
    # Only `status == "error"` used to count here, so a run could print three
    # "**no — expected ...**" rows into the report and still exit 0 under
    # "[OK] All checks passed". The report was already honest; the exit code
    # was not, and the exit code is what a person or a CI job reads.
    scenario_failures = []
    for r in normal_results:
        if r["status"] == "error":
            continue
        ok, why = matches(r.get("expected"), r.get("recorded") or r)
        if not ok:
            scenario_failures.append(f"{r['scenario_id']}: {why}")

    if scenario_failures:
        print(f"\n[FAIL] {len(scenario_failures)} scenario(s) did not match "
              f"what the ledger recorded:")
        for line in scenario_failures:
            print(f"    {line}")
        sys.exit(1)

    # "All checks passed" beside "1 attack not run" is the same overclaim the
    # report was fixed for. A run with skips did not check everything.
    if skipped:
        ran = len(attack_results) - len(skipped)
        print(f"[OK] {ran}/{ran} attacks run passed — {len(skipped)} not run "
              f"(see above); NOT a full pass of the suite")
    else:
        print("[OK] All checks passed")


if __name__ == "__main__":
    main()
