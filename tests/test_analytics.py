"""
Tests for server/api/analytics.py — the numbers the dashboard displays.

Every figure on the control plane is computed by this module, so these tests
guard the claims the dashboard makes on screen. The headline one is
`unauthorised_money_movements`: it must count a charge that no ALLOW verdict
preceded, and it must not be fooled by an authorisation that arrives afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.api import analytics
from server.db.models import LedgerEntry, SessionRecord
from server.ledger.events import EventType


# ── Helpers ───────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def entry(seq: int, session_id: str, event_type, payload=None, *, offset_ms=0, replayed=False):
    """Build a detached LedgerEntry. These functions are pure — no DB needed."""
    ts = (_T0 + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds")
    return LedgerEntry(
        seq=seq,
        ts=ts,
        session_id=session_id,
        event_type=event_type.value if hasattr(event_type, "value") else event_type,
        payload=payload or {},
        prev_hash="0" * 64,
        hash="a" * 64,
        replayed_from_fixture=replayed,
    )


def verdict(seq, session_id, decision, code=None, **kw):
    return entry(
        seq,
        session_id,
        EventType.POLICY_EVALUATED,
        {"decision": decision, "code": code or decision},
        **kw,
    )


def session(session_id, status, created_at=_T0):
    return SessionRecord(
        id=session_id,
        buyer_id="buyer_test",
        merchant_id="merchant_test",
        goal="test goal",
        budget_paise=500_000,
        status=status,
        created_at=created_at.replace(tzinfo=None),
    )


# ── The headline number ───────────────────────────────────────────────────────

class TestUnauthorisedMoneyMovement:

    def test_allow_then_charge_is_authorised(self):
        entries = [
            verdict(1, "s1", "ALLOW"),
            entry(2, "s1", EventType.ORDER_CREATED, {"amount_paise": 29900}),
        ]
        result = analytics.unauthorised_money_movements(entries)
        assert result["count"] == 0
        assert result["movements_checked"] == 1

    def test_charge_with_no_verdict_at_all_is_counted(self):
        entries = [entry(1, "s1", EventType.ORDER_CREATED, {"amount_paise": 29900})]
        result = analytics.unauthorised_money_movements(entries)
        assert result["count"] == 1
        assert result["offending_entries"][0]["seq"] == 1
        assert result["offending_entries"][0]["amount_paise"] == 29900

    def test_charge_after_a_deny_is_counted(self):
        entries = [
            verdict(1, "s1", "DENY", "PER_TXN_CAP"),
            entry(2, "s1", EventType.PAYMENT_CAPTURED, {"amount_paise": 900_000}),
        ]
        assert analytics.unauthorised_money_movements(entries)["count"] == 1

    def test_authorisation_must_precede_the_movement(self):
        """An ALLOW arriving after the charge does not retroactively authorise it."""
        entries = [
            entry(1, "s1", EventType.ORDER_CREATED, {"amount_paise": 29900}),
            verdict(2, "s1", "ALLOW"),
        ]
        assert analytics.unauthorised_money_movements(entries)["count"] == 1

    def test_human_approval_authorises_an_escalated_session(self):
        entries = [
            verdict(1, "s1", "ESCALATE", "NEW_MERCHANT"),
            entry(2, "s1", EventType.HUMAN_APPROVED, {"resolved_by": "operator"}),
            entry(3, "s1", EventType.ORDER_CREATED, {"amount_paise": 29900}),
        ]
        assert analytics.unauthorised_money_movements(entries)["count"] == 0

    def test_authorisation_does_not_leak_between_sessions(self):
        """An ALLOW on one session must not cover a charge on another."""
        entries = [
            verdict(1, "s1", "ALLOW"),
            entry(2, "s2", EventType.ORDER_CREATED, {"amount_paise": 29900}),
        ]
        result = analytics.unauthorised_money_movements(entries)
        assert result["count"] == 1
        assert result["offending_entries"][0]["session_id"] == "s2"

    def test_simulated_payments_are_audited_too(self):
        """The harness path is held to the same standard as the live one."""
        entries = [entry(1, "s1", EventType.PAYMENT_SIMULATED, {"amount_paise": 1}, replayed=True)]
        assert analytics.unauthorised_money_movements(entries)["count"] == 1

    def test_refunds_are_not_money_movement(self):
        """A refund returns money; it is not an unauthorised charge."""
        entries = [entry(1, "s1", EventType.REFUND_INITIATED, {"amount_paise": 29900})]
        result = analytics.unauthorised_money_movements(entries)
        assert result["count"] == 0
        assert result["movements_checked"] == 0

    def test_empty_ledger(self):
        result = analytics.unauthorised_money_movements([])
        assert result["count"] == 0
        assert result["movements_checked"] == 0
        assert result["offending_entries"] == []

    def test_reports_the_interventions_behind_the_headline(self):
        """The zero is only meaningful next to what was stopped or held."""
        entries = [
            verdict(1, "s1", "DENY", "PER_TXN_CAP"),
            verdict(2, "s2", "ESCALATE", "VELOCITY"),
            entry(3, "s2", EventType.ESCALATED, {"reason_code": "VELOCITY"}),
            verdict(4, "s3", "ALLOW"),
            entry(5, "s3", EventType.ORDER_CREATED, {"amount_paise": 100}),
        ]
        result = analytics.unauthorised_money_movements(entries)
        assert result["count"] == 0
        assert result["movements_checked"] == 1
        assert result["policy_denials"] == 1
        assert result["escalations_raised"] == 1


# ── Verdict split ─────────────────────────────────────────────────────────────

class TestPolicySplit:

    def test_counts_each_decision(self):
        entries = [
            verdict(1, "s1", "ALLOW"),
            verdict(2, "s2", "DENY", "PER_TXN_CAP"),
            verdict(3, "s3", "ESCALATE", "VELOCITY"),
            verdict(4, "s4", "ALLOW"),
        ]
        split = analytics.policy_split(entries, total_sessions=4)
        assert (split["ALLOW"], split["DENY"], split["ESCALATE"]) == (2, 1, 1)
        assert split["total"] == 4

    def test_ignores_non_verdict_entries(self):
        entries = [verdict(1, "s1", "ALLOW"), entry(2, "s1", EventType.CART_SIGNED, {})]
        assert analytics.policy_split(entries)["total"] == 1

    def test_split_counts_sessions_not_entries(self):
        """
        A re-evaluated session is one session with one outcome. Counting entries
        let the strip report more verdicts than sessions with nothing saying
        which population each number described.
        """
        entries = [
            verdict(1, "s1", "ESCALATE", "NEW_MERCHANT"),
            entry(2, "s1", EventType.HUMAN_APPROVED, {"resolved_by": "op"}),
            verdict(3, "s1", "ALLOW"),          # same session, re-evaluated
        ]
        split = analytics.policy_split(entries, total_sessions=1)
        assert split["total"] == 1
        assert split["ALLOW"] == 1              # the last verdict wins
        assert split["ESCALATE"] == 0
        assert split["verdict_entries"] == 2    # raw count still available

    def test_split_is_internally_consistent(self):
        """ALLOW + DENY + ESCALATE must always equal total, which must be <= sessions."""
        entries = [
            verdict(1, "s1", "ALLOW"),
            verdict(2, "s2", "DENY", "PER_TXN_CAP"),
            verdict(3, "s3", "ESCALATE", "VELOCITY"),
        ]
        split = analytics.policy_split(entries, total_sessions=6)
        assert split["ALLOW"] + split["DENY"] + split["ESCALATE"] == split["total"]
        assert split["total"] <= split["sessions_total"]
        assert split["sessions_without_verdict"] == 3   # 6 sessions, 3 verdicts

    def test_reason_codes_exclude_allow(self):
        entries = [
            verdict(1, "s1", "ALLOW"),
            verdict(2, "s2", "DENY", "PER_TXN_CAP"),
            verdict(3, "s3", "DENY", "PER_TXN_CAP"),
            verdict(4, "s4", "ESCALATE", "VELOCITY"),
        ]
        codes = analytics.reason_code_split(entries)
        assert codes == {"PER_TXN_CAP": 2, "VELOCITY": 1}
        # Sorted most-frequent-first so the dashboard can render the top codes.
        assert list(codes)[0] == "PER_TXN_CAP"


# ── Latency ───────────────────────────────────────────────────────────────────

class TestLatencyStats:

    def test_measures_first_to_last_event(self):
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.PAYMENT_CAPTURED, offset_ms=250),
        ]
        stats = analytics.latency_stats([session("s1", "captured")], entries)
        assert stats["mean_ms"] == 250
        assert stats["samples"] == 1

    def test_excludes_live_sessions(self):
        """An unfinished session has no duration yet, so it is not a sample."""
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.CART_SIGNED, offset_ms=100),
        ]
        stats = analytics.latency_stats([session("s1", "active")], entries)
        assert stats["samples"] == 0
        assert stats["mean_ms"] is None
        assert stats["p95_ms"] is None
        assert stats["engine_mean_ms"] is None

    def test_engine_latency_excludes_model_time(self):
        """
        Engine latency is wall clock minus the model call.

        A live agent session spends seconds waiting on an inference API. Folding
        that into "latency" reports the provider's speed as though it were the
        rail's, which is what made a pure-policy path look like it had regressed
        to five seconds.
        """
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.LLM_CALL, {"latency_ms": 4000}, offset_ms=10),
            entry(3, "s1", EventType.SESSION_CLOSED, offset_ms=4200),
        ]
        stats = analytics.latency_stats([session("s1", "captured")], entries)

        assert stats["mean_ms"] == 4200            # wall clock, honestly reported
        assert stats["engine_mean_ms"] == 200      # 4200 - 4000 spent in the model
        assert stats["samples"] == 1

    def test_engine_latency_equals_wall_clock_without_a_model_call(self):
        """Harness and seeded runs make no model calls; the two must agree."""
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.SESSION_CLOSED, offset_ms=25),
        ]
        stats = analytics.latency_stats([session("s1", "captured")], entries)
        assert stats["mean_ms"] == stats["engine_mean_ms"] == 25

    def test_engine_latency_never_goes_negative(self):
        """A model latency larger than the recorded span must clamp, not invert."""
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.LLM_CALL, {"latency_ms": 9_000}, offset_ms=5),
            entry(3, "s1", EventType.SESSION_CLOSED, offset_ms=50),
        ]
        stats = analytics.latency_stats([session("s1", "captured")], entries)
        assert stats["engine_mean_ms"] == 0

    def test_no_data_reports_null_not_zero(self):
        stats = analytics.latency_stats([], [])
        assert stats["mean_ms"] is None
        assert stats["samples"] == 0


# ── Cost ──────────────────────────────────────────────────────────────────────

class TestCostStats:

    def test_no_llm_calls_reports_no_samples(self):
        """Stub runs make no model calls — the mean must be null, not zero."""
        stats = analytics.cost_stats([verdict(1, "s1", "ALLOW")])
        assert stats["mean_usd_micros_per_session"] is None
        assert stats["samples"] == 0
        assert stats["llm_calls"] == 0

    def test_means_over_sessions_not_calls(self):
        entries = [
            entry(1, "s1", EventType.LLM_CALL, {"cost_usd_micros": 100}),
            entry(2, "s1", EventType.LLM_CALL, {"cost_usd_micros": 300}),
            entry(3, "s2", EventType.LLM_CALL, {"cost_usd_micros": 400}),
        ]
        stats = analytics.cost_stats(entries)
        assert stats["total_usd_micros"] == 800
        assert stats["llm_calls"] == 3
        assert stats["samples"] == 2            # two sessions
        assert stats["mean_usd_micros_per_session"] == 400

    def test_unpriced_calls_counted_but_excluded_from_total(self):
        entries = [
            entry(1, "s1", EventType.LLM_CALL, {"cost_usd_micros": 100}),
            entry(2, "s1", EventType.LLM_CALL, {"cost_usd_micros": None}),
        ]
        stats = analytics.cost_stats(entries)
        assert stats["unpriced_calls"] == 1
        assert stats["total_usd_micros"] == 100


# ── Elapsed ───────────────────────────────────────────────────────────────────

class TestElapsed:

    def test_terminal_session_reports_settled_duration(self):
        entries = [
            entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0),
            entry(2, "s1", EventType.PAYMENT_CAPTURED, offset_ms=500),
        ]
        spans = analytics.session_spans(entries)
        # `now` is far in the future; a settled session must ignore it.
        now = _T0 + timedelta(hours=3)
        assert analytics.elapsed_ms_for(session("s1", "captured"), spans["s1"], now) == 500

    def test_live_session_reports_running_clock(self):
        entries = [entry(1, "s1", EventType.INTENT_SIGNED, offset_ms=0)]
        spans = analytics.session_spans(entries)
        now = _T0 + timedelta(seconds=10)
        assert analytics.elapsed_ms_for(session("s1", "active"), spans["s1"], now) == 10_000

    def test_session_with_no_ledger_entries_falls_back_to_created_at(self):
        now = _T0 + timedelta(seconds=4)
        assert analytics.elapsed_ms_for(session("s1", "active"), None, now) == 4_000


# ── Escalation diff ───────────────────────────────────────────────────────────

class TestEscalationDiff:

    INTENT = {
        "aud": "merchant_test",
        "budget_paise": 500_000,
        "estimate_paise": 100_000,
        "categories": ["grocery"],
        "max_items": 3,
    }

    def _row(self, diff, field):
        return next(r for r in diff if r["field"] == field)

    def test_conforming_cart_flags_nothing(self):
        cart = {
            "merchant_id": "merchant_test",
            "total_paise": 100_000,
            "items": [{"category": "grocery"}],
        }
        diff = analytics.escalation_diff(self.INTENT, cart)
        assert all(not row["differs"] for row in diff)

    def test_over_budget_flags_total(self):
        cart = {"merchant_id": "merchant_test", "total_paise": 900_000, "items": []}
        assert self._row(analytics.escalation_diff(self.INTENT, cart), "total")["differs"]

    def test_price_drift_uses_the_same_threshold_as_the_policy_engine(self):
        """PRICE_DRIFT fires above estimate x 1.05, so the diff must match it."""
        within = {"merchant_id": "merchant_test", "total_paise": 105_000, "items": []}
        beyond = {"merchant_id": "merchant_test", "total_paise": 105_001, "items": []}
        assert not self._row(analytics.escalation_diff(self.INTENT, within), "estimate")["differs"]
        assert self._row(analytics.escalation_diff(self.INTENT, beyond), "estimate")["differs"]

    def test_out_of_scope_category_flags_and_names_it(self):
        cart = {
            "merchant_id": "merchant_test",
            "total_paise": 1000,
            "items": [{"category": "grocery"}, {"category": "electronics"}],
        }
        row = self._row(analytics.escalation_diff(self.INTENT, cart), "categories")
        assert row["differs"]
        assert "electronics" in row["note"]

    def test_too_many_items_flags_count(self):
        cart = {
            "merchant_id": "merchant_test",
            "total_paise": 1000,
            "items": [{"category": "grocery"}] * 4,
        }
        row = self._row(analytics.escalation_diff(self.INTENT, cart), "line_items")
        assert row["differs"]
        assert row["proposed"] == 4

    def test_handles_empty_snapshots(self):
        diff = analytics.escalation_diff({}, {})
        assert len(diff) == 5
        assert all(not row["differs"] for row in diff)


# ── Timestamp parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "not-a-timestamp"])
def test_parse_ts_rejects_junk(bad):
    assert analytics.parse_ts(bad) is None


def test_parse_ts_normalises_to_utc():
    parsed = analytics.parse_ts("2026-08-29T12:00:00.000+00:00")
    assert parsed == _T0


# ── LLM token accounting ──────────────────────────────────────────────────────

class TestTokenUsageExtraction:
    """
    Usage field names differ by API. Anthropic reports input_tokens /
    output_tokens; the OpenAI-compatible APIs (Groq, xAI) report prompt_tokens /
    completion_tokens. Reading only one style recorded every call as zero
    tokens, which made every cost zero — a wrong number wearing the clothes of
    a real measurement.
    """

    def test_reads_anthropic_field_names(self):
        from server.ledger.llm_cost import extract_token_usage

        class U:
            input_tokens, output_tokens = 120, 45

        assert extract_token_usage(U()) == (120, 45)

    def test_reads_openai_compatible_field_names(self):
        from server.ledger.llm_cost import extract_token_usage

        class U:
            prompt_tokens, completion_tokens = 300, 88

        assert extract_token_usage(U()) == (300, 88)

    def test_reads_a_plain_dict(self):
        from server.ledger.llm_cost import extract_token_usage

        assert extract_token_usage({"prompt_tokens": 7, "completion_tokens": 3}) == (7, 3)

    def test_missing_usage_is_zero_not_an_error(self):
        from server.ledger.llm_cost import extract_token_usage

        assert extract_token_usage(None) == (0, 0)
        assert extract_token_usage(object()) == (0, 0)

    def test_unpriced_model_with_no_configured_rate_returns_none(self):
        """A model with no known rate must not be priced at zero."""
        from server.ledger.llm_cost import price_usd_micros

        assert price_usd_micros("some/unknown-model", 1000, 1000) is None

    def test_configured_rate_prices_an_otherwise_unknown_model(self, monkeypatch):
        from server.config import settings
        from server.ledger import llm_cost

        monkeypatch.setattr(settings, "LLM_PRICE_INPUT_USD_PER_MTOK", 1.0)
        monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_USD_PER_MTOK", 2.0)
        # 1M in at $1 + 1M out at $2 = $3 = 3_000_000 micro-USD
        assert llm_cost.price_usd_micros("some/unknown-model", 1_000_000, 1_000_000) == 3_000_000


class TestRuleComparison:
    """
    The card must show the pair the rule weighed, not every field of the mandate.

    A PRICE_DRIFT escalation on a ₹999 cart under a ₹5,000 budget showed a
    total-vs-budget row reading comfortably within limits, beside cause text
    saying 899% over. Both were accurate; only one was the rule's.
    """

    INTENT = {"budget_paise": 500_000, "estimate_paise": 10_000, "max_line_items": 4}
    CART = {"total_paise": 99_900, "items": [{"category": "electronics"}]}

    def test_price_drift_compares_estimate_to_cart_total(self):
        c = analytics.rule_comparison("PRICE_DRIFT", self.INTENT, self.CART)
        assert c["left_paise"] == 10_000          # the estimate, not the budget
        assert c["right_paise"] == 99_900
        assert "estimate" in c["left_label"]
        # The ceiling is the rule's own 1.05 threshold.
        assert c["threshold_paise"] == 10_500

    def test_per_txn_cap_compares_budget_to_cart_total(self):
        c = analytics.rule_comparison("PER_TXN_CAP", self.INTENT, self.CART)
        assert c["left_paise"] == 500_000         # the budget, not the estimate
        assert c["threshold_paise"] == 500_000

    def test_history_based_rules_have_no_cart_comparison(self):
        """VELOCITY weighs a rate; there is no pair of cart values to show."""
        for code in ("VELOCITY", "FIRST_CONTACT_BUYER", "DAILY_CAP"):
            assert analytics.rule_comparison(code, self.INTENT, self.CART) is None

    def test_no_comparison_without_a_cart_total(self):
        assert analytics.rule_comparison("PRICE_DRIFT", self.INTENT, {}) is None
