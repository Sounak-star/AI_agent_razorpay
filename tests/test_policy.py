"""
Policy engine unit tests.

All tests are pure: no DB, no network, no LLM.
Target: >90% line coverage of server/policy/.

Test naming convention:
  test_<rule>_<condition> → <expected decision>

The injection defence is NOT tested here via "did the model ignore the text".
It is tested structurally: the rule sees server-computed cart.total_paise,
not anything from the LLM. If an injected description somehow told the LLM to
set price=0, the cart arriving here still has the catalog price — so PER_TXN_CAP
and CATEGORY_DENY fire as normal.
"""

from __future__ import annotations

import time
from typing import List

import pytest

from server.mandate.schema import Cart, CartItem, IntentMandate
from server.policy.codes import Decision, ReasonCode
from server.policy.engine import evaluate
from server.policy.rules import TxnHistoryItem, Verdict, rule_per_txn_cap, rule_daily_cap, rule_velocity, rule_category_deny, rule_price_drift, rule_item_count, rule_first_contact_buyer


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_intent(
    budget_paise: int = 100_000,
    categories: List[str] = None,
    max_items: int = 10,
    estimate_paise: int = 80_000,
    merchant_id: str = "merch_abc",
) -> IntentMandate:
    now = int(time.time())
    return IntentMandate(
        typ="intent",
        jti="test-jti-intent",
        sub="buyer_001",
        aud=merchant_id,
        iat=now,
        exp=now + 900,
        budget_paise=budget_paise,
        categories=categories or ["grocery", "electronics"],
        max_items=max_items,
        estimate_paise=estimate_paise,
    )


def make_cart(
    total_override: int | None = None,
    categories: List[str] = None,
    n_items: int = 2,
    merchant_id: str = "merch_abc",
) -> Cart:
    cats = categories or ["grocery"]
    if total_override is not None:
        # Use a single item so cart.total_paise == total_override exactly.
        # Integer division across multiple items loses the remainder.
        items = [
            CartItem(
                sku_id="SKU000",
                name="Product 0",
                category=cats[0],
                quantity=1,
                unit_price_paise=total_override,
            )
        ]
    else:
        items = [
            CartItem(
                sku_id=f"SKU{i:03}",
                name=f"Product {i}",
                category=cats[i % len(cats)],
                quantity=1,
                unit_price_paise=10_000,
            )
            for i in range(n_items)
        ]
    return Cart(merchant_id=merchant_id, items=items)


def make_history(
    n: int = 0,
    merchant_id: str = "merch_abc",
    settled: bool = True,
    ts_offset: float = 0,          # seconds relative to now
    total_paise: int = 10_000,
) -> List[TxnHistoryItem]:
    now = time.time()
    return [
        TxnHistoryItem(
            session_id=f"sess_{i}",
            merchant_id=merchant_id,
            total_paise=total_paise,
            settled=settled,
            ts=now + ts_offset,
        )
        for i in range(n)
    ]


# ── PER_TXN_CAP ───────────────────────────────────────────────────────────────

class TestPerTxnCap:
    def test_under_budget_allows(self):
        intent = make_intent(budget_paise=100_000)
        cart = make_cart(total_override=99_000)
        result = rule_per_txn_cap(intent, cart, [])
        assert result is None   # rule does not trigger

    def test_at_budget_allows(self):
        intent = make_intent(budget_paise=100_000)
        cart = make_cart(total_override=100_000)
        result = rule_per_txn_cap(intent, cart, [])
        assert result is None   # equal to budget — allowed

    def test_over_budget_denies(self):
        intent = make_intent(budget_paise=100_000)
        cart = make_cart(total_override=100_001)
        result = rule_per_txn_cap(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.DENY
        assert result.code == ReasonCode.PER_TXN_CAP

    def test_zero_budget_denies_any_nonzero(self):
        intent = make_intent(budget_paise=0)
        cart = make_cart(total_override=1)
        result = rule_per_txn_cap(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.DENY


# ── DAILY_CAP ─────────────────────────────────────────────────────────────────

class TestDailyCap:
    def test_no_history_under_cap(self):
        intent = make_intent(budget_paise=100_000)
        cart = make_cart(total_override=50_000)
        result = rule_daily_cap(intent, cart, [])
        assert result is None

    def test_prior_spend_pushes_over_cap(self):
        from server.config import settings
        cap = settings.DAILY_SPEND_CAP_PAISE
        history = make_history(n=1, total_paise=cap - 1000, settled=True)
        cart = make_cart(total_override=2_000)
        intent = make_intent(budget_paise=cap)
        result = rule_daily_cap(intent, cart, history)
        assert result is not None
        assert result.decision == Decision.DENY
        assert result.code == ReasonCode.DAILY_CAP

    def test_unsettled_history_excluded_from_daily_sum(self):
        from server.config import settings
        cap = settings.DAILY_SPEND_CAP_PAISE
        # Unsettled history — should NOT count toward daily cap
        history = make_history(n=100, total_paise=cap, settled=False)
        cart = make_cart(total_override=1_000)
        intent = make_intent()
        result = rule_daily_cap(intent, cart, history)
        assert result is None


# ── VELOCITY ──────────────────────────────────────────────────────────────────

class TestVelocity:
    def test_under_limit_passes(self):
        from server.config import settings
        history = make_history(n=settings.VELOCITY_MAX_TXN - 1, ts_offset=-60)
        result = rule_velocity(make_intent(), make_cart(), history)
        assert result is None

    def test_at_limit_escalates(self):
        from server.config import settings
        history = make_history(n=settings.VELOCITY_MAX_TXN, ts_offset=-60)
        result = rule_velocity(make_intent(), make_cart(), history)
        assert result is not None
        assert result.decision == Decision.ESCALATE
        assert result.code == ReasonCode.VELOCITY

    def test_old_history_outside_window_ignored(self):
        from server.config import settings
        # Put history far in the past, outside the velocity window
        history = make_history(
            n=settings.VELOCITY_MAX_TXN * 10,
            ts_offset=-(settings.VELOCITY_WINDOW_SECONDS + 1),
        )
        result = rule_velocity(make_intent(), make_cart(), history)
        assert result is None


# ── CATEGORY_DENY ─────────────────────────────────────────────────────────────

class TestCategoryDeny:
    def test_allowed_category_passes(self):
        intent = make_intent(categories=["grocery"])
        cart = make_cart(categories=["grocery"])
        result = rule_category_deny(intent, cart, [])
        assert result is None

    def test_forbidden_category_denies(self):
        intent = make_intent(categories=["grocery"])
        cart = make_cart(categories=["electronics"])  # not in intent
        result = rule_category_deny(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.DENY
        assert result.code == ReasonCode.CATEGORY_DENY

    def test_mixed_cart_one_forbidden_denies(self):
        intent = make_intent(categories=["grocery"])
        # Two items: one grocery (ok), one electronics (forbidden)
        cart = Cart(
            merchant_id="merch_abc",
            items=[
                CartItem(sku_id="G001", name="Rice", category="grocery", quantity=1, unit_price_paise=300_00),
                CartItem(sku_id="E001", name="Earbuds", category="electronics", quantity=1, unit_price_paise=1500_00),
            ],
        )
        result = rule_category_deny(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.DENY
        assert "E001" in result.detail


# ── PRICE_DRIFT ───────────────────────────────────────────────────────────────

class TestPriceDrift:
    def test_within_five_percent_passes(self):
        intent = make_intent(estimate_paise=100_000)
        cart = make_cart(total_override=105_000)  # exactly 5% — should pass (not strictly greater)
        result = rule_price_drift(intent, cart, [])
        assert result is None

    def test_over_five_percent_escalates(self):
        intent = make_intent(estimate_paise=100_000)
        cart = make_cart(total_override=105_001)  # one paise over 5%
        result = rule_price_drift(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.ESCALATE
        assert result.code == ReasonCode.PRICE_DRIFT

    def test_zero_estimate_skips_check(self):
        intent = make_intent(estimate_paise=0)
        cart = make_cart(total_override=999_999)
        result = rule_price_drift(intent, cart, [])
        assert result is None  # can't compute drift with zero estimate


# ── ITEM_COUNT ────────────────────────────────────────────────────────────────

class TestItemCount:
    def test_at_limit_passes(self):
        intent = make_intent(max_items=3)
        cart = make_cart(n_items=3)
        result = rule_item_count(intent, cart, [])
        assert result is None

    def test_over_limit_denies(self):
        intent = make_intent(max_items=3)
        cart = make_cart(n_items=4)
        result = rule_item_count(intent, cart, [])
        assert result is not None
        assert result.decision == Decision.DENY
        assert result.code == ReasonCode.ITEM_COUNT

    def test_one_item_passes_when_limit_is_one(self):
        intent = make_intent(max_items=1)
        cart = make_cart(n_items=1)
        result = rule_item_count(intent, cart, [])
        assert result is None


# ── FIRST_CONTACT_BUYER ──────────────────────────────────────────────────────────────

class TestNewMerchant:
    def test_no_history_escalates(self):
        result = rule_first_contact_buyer(make_intent(), make_cart(), history=[])
        assert result is not None
        assert result.decision == Decision.ESCALATE
        assert result.code == ReasonCode.FIRST_CONTACT_BUYER

    def test_prior_history_with_same_merchant_passes(self):
        history = make_history(n=1, merchant_id="merch_abc")
        intent = make_intent(merchant_id="merch_abc")
        cart = make_cart(merchant_id="merch_abc")
        result = rule_first_contact_buyer(intent, cart, history)
        assert result is None

    def test_history_with_different_merchant_escalates(self):
        history = make_history(n=5, merchant_id="merch_other")
        cart = make_cart(merchant_id="merch_abc")
        result = rule_first_contact_buyer(make_intent(), cart, history)
        assert result is not None
        assert result.decision == Decision.ESCALATE


# ── Full engine: evaluate() ────────────────────────────────────────────────────

class TestEngine:
    def test_all_passing_returns_allow(self):
        intent = make_intent(budget_paise=200_000, estimate_paise=100_000)
        cart = make_cart(total_override=100_000)
        history = make_history(n=1, merchant_id="merch_abc")
        verdict = evaluate(intent, cart, history, mandate_valid=True)
        assert verdict.decision == Decision.ALLOW
        assert verdict.code == ReasonCode.ALLOW

    def test_invalid_mandate_denies_before_any_rule(self):
        intent = make_intent()
        cart = make_cart()
        verdict = evaluate(intent, cart, [], mandate_valid=False, mandate_fail_reason="jti_replayed")
        assert verdict.decision == Decision.DENY
        assert verdict.code == ReasonCode.MANDATE_INVALID
        assert "jti_replayed" in verdict.detail

    def test_first_failing_rule_wins(self):
        """PER_TXN_CAP fires before CATEGORY_DENY — confirm PER_TXN_CAP is the verdict."""
        intent = make_intent(budget_paise=50_000, categories=["grocery"])
        # Cart is over budget AND has a forbidden category
        cart = Cart(
            merchant_id="merch_abc",
            items=[
                CartItem(sku_id="E001", name="Laptop", category="electronics", quantity=1, unit_price_paise=100_000),
            ],
        )
        history = make_history(n=1, merchant_id="merch_abc")
        verdict = evaluate(intent, cart, history, mandate_valid=True)
        assert verdict.decision == Decision.DENY
        assert verdict.code == ReasonCode.PER_TXN_CAP  # fires first

    def test_mandate_invalid_even_when_other_rules_would_allow(self):
        intent = make_intent(budget_paise=1_000_000, estimate_paise=1_000_000)
        cart = make_cart(total_override=100_000)
        history = make_history(n=1, merchant_id="merch_abc")
        verdict = evaluate(intent, cart, history, mandate_valid=False, mandate_fail_reason="expired")
        assert verdict.decision == Decision.DENY
        assert verdict.code == ReasonCode.MANDATE_INVALID

    def test_price_drift_escalates_before_first_contact(self):
        """PRICE_DRIFT (index 4) fires before FIRST_CONTACT_BUYER (index 6)."""
        intent = make_intent(estimate_paise=100_000, budget_paise=200_000, categories=["grocery"])
        cart = make_cart(total_override=110_001)  # > 5% drift
        verdict = evaluate(intent, cart, history=[], mandate_valid=True)
        # History is empty → new merchant would escalate too, but drift fires first
        assert verdict.decision == Decision.ESCALATE
        assert verdict.code == ReasonCode.PRICE_DRIFT


# ── ITEM_COUNT: lines, not units ──────────────────────────────────────────────

class TestItemCountCountsLinesNotUnits:
    """
    The ceiling applies to cart LINES. "max_items" said neither, and read as
    units to anyone who had not gone looking at the rule.

    A 2-line cart of 5 units each is 2 against the ceiling, not 10. That is the
    behaviour; these tests pin it so it cannot drift silently, and name the
    ambiguous case explicitly.
    """

    def _cart(self, *quantities: int) -> Cart:
        return Cart(merchant_id="merch_test", items=[
            CartItem(
                sku_id=f"GRO{n:03d}", name=f"item {n}", category="grocery",
                quantity=q, unit_price_paise=1000,
            )
            for n, q in enumerate(quantities, start=1)
        ])

    def _intent(self, max_line_items: int) -> IntentMandate:
        return make_intent(max_items=max_line_items)

    def test_the_ambiguous_case_two_lines_ten_units_under_a_limit_of_four(self):
        """
        The case that prompted this: authorised "max 4", cart holds 2 lines
        totalling 10 units. It passes, because the ceiling counts lines.
        """
        verdict = rule_item_count(self._intent(4), self._cart(5, 5), [])
        assert verdict is None, "2 line items is within a 4 line-item ceiling"

    def test_units_alone_never_trip_the_rule(self):
        """One line, a hundred units, ceiling of one: still allowed."""
        assert rule_item_count(self._intent(1), self._cart(100), []) is None

    def test_lines_over_the_ceiling_deny(self):
        verdict = rule_item_count(self._intent(2), self._cart(1, 1, 1), [])
        assert verdict is not None
        assert verdict.code == ReasonCode.ITEM_COUNT

    def test_detail_reports_lines_and_units_separately(self):
        """
        A reader of the verdict must not have to guess which was counted.
        """
        verdict = rule_item_count(self._intent(2), self._cart(5, 5, 5), [])
        assert "3 line items" in verdict.detail
        assert "15 units" in verdict.detail
        assert "allows 2 line items" in verdict.detail

    def test_boundary_is_inclusive(self):
        """Exactly at the ceiling is allowed; one over is not."""
        assert rule_item_count(self._intent(3), self._cart(1, 1, 1), []) is None
        assert rule_item_count(self._intent(3), self._cart(1, 1, 1, 1), []) is not None
