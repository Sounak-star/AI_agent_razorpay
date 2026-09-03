"""
Session lifecycle and reconciler tests.

These cover two bugs that were invisible from the API surface but obvious on the
dashboard:

  1. Completed sessions carried only POLICY_EVALUATED and PAYMENT_SIMULATED.
     The events were never appended — CATALOG_QUERIED, CART_BUILT and
     SESSION_CLOSED had no append() call anywhere in the codebase — so the
     ledger could not show that anything was checked before money moved.

  2. Sessions stalled before the payment stage sat in "active" forever. The
     reconciler's only sweep required razorpay_order_id IS NOT NULL, so a
     session that hung at policy evaluation was never a candidate.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.config import settings
from server.db.models import EscalationRequest, LedgerEntry, SessionRecord
from server.ledger.events import EventType
from server.mandate.issuer import sign_intent
from server.mcp.cart import CartBuildError, build_authoritative_cart, record_intent_signed
from server.payments import reconciler
from server.payments.saga import SagaError, close_session, run_saga_harness

# The full lifecycle a completed session must be able to show.
REQUIRED_LIFECYCLE = [
    EventType.INTENT_SIGNED,
    EventType.CATALOG_QUERIED,
    EventType.QUOTE_ISSUED,
    EventType.CART_BUILT,
    EventType.POLICY_EVALUATED,
    EventType.CART_SIGNED,
    EventType.ORDER_CREATED,
    EventType.SESSION_CLOSED,
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_session(db, *, budget_paise=5_000_000, status="active", buyer_id="buyer_lifecycle"):
    session = SessionRecord(
        id=str(uuid.uuid4()),
        buyer_id=buyer_id,
        merchant_id=settings.MERCHANT_ID,
        goal="lifecycle test",
        budget_paise=budget_paise,
        status=status,
    )
    db.add(session)
    db.commit()
    return session


def events_for(db, session_id) -> list[str]:
    return [
        e.event_type
        for e in db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .all()
    ]


def run_allowed_session(db, session, sku_ids=None, quantities=None, offer_upsell=False):
    """
    Drive one session all the way through an ALLOW verdict.

    The offer step is off unless a test asks for it: it deliberately changes the
    cart, which every assertion about a specific cart would otherwise have to
    account for.
    """
    _token, intent = sign_intent(
        buyer_id=session.buyer_id,
        merchant_id=settings.MERCHANT_ID,
        budget_paise=session.budget_paise,
        categories=["grocery"],
        max_items=10,
        estimate_paise=session.budget_paise,
    )
    record_intent_signed(db, session.id, intent)
    cart = build_authoritative_cart(
        db=db,
        session_id=session.id,
        sku_ids=sku_ids or ["GRO001"],
        quantities=quantities or [1],
        merchant_id=settings.MERCHANT_ID,
    )
    # One prior settled txn so new_merchant doesn't escalate ahead of the point
    # under test.
    from server.policy.rules import TxnHistoryItem
    history = [TxnHistoryItem(
        session_id="prior",
        merchant_id=settings.MERCHANT_ID,
        total_paise=10_000,
        settled=True,
        ts=datetime.now(timezone.utc).timestamp() - 86_400,
    )]
    return run_saga_harness(
        db=db, session=session, intent=intent, cart=cart, history=history,
        offer_upsell=offer_upsell,
    )


# ── Bug 1: complete lifecycle ─────────────────────────────────────────────────

class TestSessionLifecycle:

    def test_completed_session_has_at_least_9_entries(self, test_db):
        """The regression guard: a completed session must show its whole story."""
        session = make_session(test_db)
        run_allowed_session(test_db, session)

        entries = events_for(test_db, session.id)
        assert len(entries) >= 9, (
            f"completed session recorded only {len(entries)} entries: {entries}"
        )

    def test_completed_session_records_every_lifecycle_stage(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)

        recorded = set(events_for(test_db, session.id))
        missing = [e.value for e in REQUIRED_LIFECYCLE if e.value not in recorded]
        assert not missing, f"missing lifecycle events: {missing}"

    def test_lifecycle_is_recorded_in_causal_order(self, test_db):
        """The cart must be built before it is judged, and judged before it is paid."""
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        order = events_for(test_db, session.id)

        def at(event: EventType) -> int:
            return order.index(event.value)

        assert at(EventType.INTENT_SIGNED) < at(EventType.CATALOG_QUERIED)
        assert at(EventType.CATALOG_QUERIED) < at(EventType.QUOTE_ISSUED)
        assert at(EventType.QUOTE_ISSUED) < at(EventType.CART_BUILT)
        assert at(EventType.CART_BUILT) < at(EventType.POLICY_EVALUATED)
        assert at(EventType.POLICY_EVALUATED) < at(EventType.CART_SIGNED)
        assert at(EventType.CART_SIGNED) < at(EventType.ORDER_CREATED)
        assert at(EventType.ORDER_CREATED) == max(
            at(EventType.ORDER_CREATED), at(EventType.CART_SIGNED)
        )
        assert order[-1] == EventType.SESSION_CLOSED.value

    def test_denied_session_is_also_closed(self, test_db):
        """A DENY is an outcome too, and must not leave the session hanging."""
        session = make_session(test_db, budget_paise=100)   # forces PER_TXN_CAP
        with pytest.raises(SagaError):
            run_allowed_session(test_db, session)

        recorded = events_for(test_db, session.id)
        assert EventType.SESSION_CLOSED.value in recorded
        test_db.refresh(session)
        assert session.status == "failed"

    def test_close_session_is_idempotent(self, test_db):
        """A second close must not append a second SESSION_CLOSED."""
        session = make_session(test_db)
        close_session(test_db, session, status="captured", reason="first")
        close_session(test_db, session, status="captured", reason="second")

        closes = [e for e in events_for(test_db, session.id)
                  if e == EventType.SESSION_CLOSED.value]
        assert len(closes) == 1

    def test_cart_builder_records_the_catalog_lookup(self, test_db):
        session = make_session(test_db)
        build_authoritative_cart(
            db=test_db,
            session_id=session.id,
            sku_ids=["GRO001"],
            quantities=[2],
            merchant_id=settings.MERCHANT_ID,
        )
        recorded = events_for(test_db, session.id)
        assert recorded == [
            EventType.CATALOG_QUERIED.value,
            EventType.QUOTE_ISSUED.value,
            EventType.CART_BUILT.value,
        ]

    def test_cart_builder_prices_from_catalog_not_caller(self, test_db):
        """The QUOTE_ISSUED total must come from the catalog."""
        from server.mcp.catalog import get_authoritative_price

        session = make_session(test_db)
        cart = build_authoritative_cart(
            db=test_db,
            session_id=session.id,
            sku_ids=["GRO001"],
            quantities=[3],
            merchant_id=settings.MERCHANT_ID,
        )
        assert cart.total_paise == get_authoritative_price("GRO001") * 3

        quote = next(
            e for e in test_db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id).all()
            if e.event_type == EventType.QUOTE_ISSUED.value
        )
        assert quote.payload["total_paise"] == cart.total_paise
        assert quote.payload["priced_by"] == "server"

    def test_unknown_sku_is_rejected_before_anything_is_logged(self, test_db):
        session = make_session(test_db)
        with pytest.raises(CartBuildError):
            build_authoritative_cart(
                db=test_db,
                session_id=session.id,
                sku_ids=["NOT_A_REAL_SKU"],
                quantities=[1],
                merchant_id=settings.MERCHANT_ID,
            )
        assert events_for(test_db, session.id) == []


class TestLedgerIsSelfSufficient:
    """
    The acceptance bar: a reader can reconstruct exactly what was bought and for
    how much from the ledger alone — no catalog, no session table, no other
    source. An audit trail that needs a second source to interpret is not an
    audit trail.

    These tests deliberately read only ledger payloads. Nothing else is queried.
    """

    def _payloads(self, db, session_id) -> dict[str, dict]:
        return {
            e.event_type: (e.payload or {})
            for e in db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session_id)
            .order_by(LedgerEntry.seq)
            .all()
        }

    def test_purchase_is_reconstructable_from_the_ledger_alone(self, test_db):
        from server.mcp.catalog import get_authoritative_price

        session = make_session(test_db)
        run_allowed_session(test_db, session, sku_ids=["GRO001", "GRO007"], quantities=[1, 3])
        p = self._payloads(test_db, session.id)

        # What was bought, and for how much — read only from CART_BUILT.
        items = p[EventType.CART_BUILT.value]["items"]
        assert {i["sku_id"] for i in items} == {"GRO001", "GRO007"}

        by_sku = {i["sku_id"]: i for i in items}
        assert by_sku["GRO001"]["quantity"] == 1
        assert by_sku["GRO007"]["quantity"] == 3

        # Every line carries a unit price and a line total that agree.
        for item in items:
            assert item["unit_price_paise"] > 0
            assert item["line_total_paise"] == item["unit_price_paise"] * item["quantity"]

        # The line totals add up to the recorded cart total...
        recomputed = sum(i["line_total_paise"] for i in items)
        assert recomputed == p[EventType.CART_BUILT.value]["total_paise"]

        # ...and that total is carried consistently through quote, order and close.
        assert p[EventType.QUOTE_ISSUED.value]["total_paise"] == recomputed
        assert p[EventType.ORDER_CREATED.value]["amount_paise"] == recomputed
        assert p[EventType.SESSION_CLOSED.value]["final_total_paise"] == recomputed

        # And the prices really are the catalog's, not something a caller supplied.
        assert by_sku["GRO001"]["unit_price_paise"] == get_authoritative_price("GRO001")

    def test_intent_payload_carries_the_full_authorisation(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        intent = self._payloads(test_db, session.id)[EventType.INTENT_SIGNED.value]

        for field in ("jti", "budget_paise", "categories", "max_items",
                      "estimate_paise", "exp"):
            assert field in intent, f"INTENT_SIGNED missing {field}"
        assert isinstance(intent["exp"], int)
        assert isinstance(intent["categories"], list)

    def test_catalog_queried_records_query_filters_and_ids_only(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        cat = self._payloads(test_db, session.id)[EventType.CATALOG_QUERIED.value]

        assert "query" in cat and "filters" in cat
        assert cat["sku_ids_returned"] == ["GRO001"]
        # IDs only: prices are fixed by QUOTE_ISSUED and must not appear to be
        # set anywhere else.
        assert "unit_price_paise" not in json.dumps(cat)

    def test_quote_payload_carries_id_lines_and_expiry(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        quote = self._payloads(test_db, session.id)[EventType.QUOTE_ISSUED.value]

        for field in ("quote_id", "items", "total_paise", "expires_at"):
            assert field in quote, f"QUOTE_ISSUED missing {field}"
        for item in quote["items"]:
            assert {"sku_id", "quantity", "unit_price_paise"} <= set(item)

    def test_quote_id_refers_to_a_real_persisted_quote(self, test_db):
        """quote_id must name something, not be a decorative identifier."""
        from server.db.models import QuoteRecord

        session = make_session(test_db)
        run_allowed_session(test_db, session)
        quote = self._payloads(test_db, session.id)[EventType.QUOTE_ISSUED.value]

        row = test_db.query(QuoteRecord).filter(
            QuoteRecord.id == quote["quote_id"]
        ).first()
        assert row is not None
        assert row.total_paise == quote["total_paise"]

    def test_cart_signed_links_mandate_to_intent(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        p = self._payloads(test_db, session.id)
        signed = p[EventType.CART_SIGNED.value]

        for field in ("jti", "intent_jti", "cart_hash", "total_paise"):
            assert field in signed, f"CART_SIGNED missing {field}"
        # The mandate chain is walkable: cart -> intent, entirely from payloads.
        assert signed["intent_jti"] == p[EventType.INTENT_SIGNED.value]["jti"]
        assert signed["cart_hash"] == p[EventType.CART_BUILT.value]["cart_hash"]

    def test_order_created_carries_receipt_and_amount(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        order = self._payloads(test_db, session.id)[EventType.ORDER_CREATED.value]

        for field in ("razorpay_order_id", "receipt", "amount_paise"):
            assert field in order, f"ORDER_CREATED missing {field}"
        assert order["receipt"]

    def test_session_closed_carries_state_total_and_duration(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        closed = self._payloads(test_db, session.id)[EventType.SESSION_CLOSED.value]

        assert closed["terminal_state"] == "captured"
        assert closed["final_total_paise"] > 0
        assert isinstance(closed["duration_ms"], int)
        assert closed["duration_ms"] >= 0


# ── Bug 2: reconciler sweeps stalled sessions ─────────────────────────────────

def _backdate(db, session_id, seconds):
    """Push every ledger entry for a session into the past."""
    stale_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds")
    for e in db.query(LedgerEntry).filter(LedgerEntry.session_id == session_id).all():
        e.ts = stale_ts
    db.commit()


class TestReconcilerStaleSweep:

    def test_session_stalled_before_payment_is_marked_stale(self, test_db):
        """
        The exact case the old sweep could not see: no order id, so the
        razorpay_order_id IS NOT NULL filter excluded it and it sat active
        forever.
        """
        session = make_session(test_db)
        build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )
        _backdate(test_db, session.id, settings.STALE_SESSION_TIMEOUT_SECONDS + 30)

        assert session.razorpay_order_id is None      # the old filter's blind spot
        swept = reconciler.sweep(test_db)

        assert swept["stale"] == 1
        test_db.refresh(session)
        assert session.status == "stale"
        assert EventType.SESSION_STALE.value in events_for(test_db, session.id)

    def test_recently_active_session_is_left_alone(self, test_db):
        session = make_session(test_db)
        build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )
        assert reconciler.sweep(test_db)["stale"] == 0
        test_db.refresh(session)
        assert session.status == "active"

    def test_session_awaiting_a_human_is_not_stale(self, test_db):
        """Waiting on an operator is by design, not a hang."""
        session = make_session(test_db)
        build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )
        test_db.add(EscalationRequest(
            id=str(uuid.uuid4()),
            session_id=session.id,
            reason_code="NEW_MERCHANT",
            detail="first purchase",
            intent_snapshot={},
            cart_snapshot={},
            status="pending",
        ))
        test_db.commit()
        _backdate(test_db, session.id, settings.STALE_SESSION_TIMEOUT_SECONDS + 30)

        assert reconciler.sweep(test_db)["stale"] == 0
        test_db.refresh(session)
        assert session.status == "active"

    def test_settled_session_is_never_swept(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        _backdate(test_db, session.id, settings.STALE_SESSION_TIMEOUT_SECONDS + 300)

        assert reconciler.sweep(test_db)["stale"] == 0
        test_db.refresh(session)
        assert session.status == "captured"

    def test_sweep_is_idempotent(self, test_db):
        session = make_session(test_db)
        build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )
        _backdate(test_db, session.id, settings.STALE_SESSION_TIMEOUT_SECONDS + 30)

        assert reconciler.sweep(test_db)["stale"] == 1
        assert reconciler.sweep(test_db)["stale"] == 0   # already stale

    def test_session_with_no_entries_at_all_falls_back_to_created_at(self, test_db):
        """A session that never produced a single event is still swept."""
        session = make_session(test_db)
        session.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=settings.STALE_SESSION_TIMEOUT_SECONDS + 60
        )
        test_db.commit()

        assert reconciler.sweep(test_db)["stale"] == 1
        test_db.refresh(session)
        assert session.status == "stale"


class TestStaleStatusSemantics:
    """A stale session stops its clock but must not be counted as a completion."""

    def test_stale_stops_the_elapsed_clock(self):
        from server.api import analytics
        assert "stale" in analytics.TERMINAL_SESSION_STATUSES

    def test_stale_is_excluded_from_latency_samples(self):
        from server.api import analytics
        assert "stale" not in analytics.SETTLED_SESSION_STATUSES


# ── Operator narrative ────────────────────────────────────────────────────────

class TestNarrative:
    """
    The operator view is templated from ledger payloads, never generated. These
    tests pin that: every line must name a seq that exists, and the money
    figures must match the entries they claim to describe.
    """

    def _lines(self, db, session):
        from server.api.narrative import build_narrative

        entries = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id)
            .order_by(LedgerEntry.seq)
            .all()
        )
        return build_narrative(session, entries), entries

    def test_every_line_traces_to_a_real_entry(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        lines, entries = self._lines(test_db, session)

        real = {e.seq for e in entries}
        assert lines, "a completed session must produce a narrative"
        for line in lines:
            assert line["seq"] in real, f"line cites seq {line['seq']}, which does not exist"

    def test_allowed_session_reads_as_a_completed_purchase(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        text = " | ".join(l["text"] for l in self._lines(test_db, session)[0])

        assert "asked to" in text
        assert "Authorised up to" in text
        assert "Agent proposed" in text
        assert "Policy check: ALLOWED" in text
        assert "payment captured" in text
        assert "Session closed" in text

    def test_denied_session_states_that_no_money_moved(self, test_db):
        session = make_session(test_db, budget_paise=100)   # forces PER_TXN_CAP
        with pytest.raises(SagaError):
            run_allowed_session(test_db, session)
        text = " | ".join(l["text"] for l in self._lines(test_db, session)[0])

        assert "Policy check: DENIED" in text
        assert "over per-transaction cap" in text
        assert "No money moved" in text

    def test_narrative_totals_match_the_cart_entry(self, test_db):
        """The story must not restate a different number from the ledger."""
        session = make_session(test_db)
        run_allowed_session(test_db, session, sku_ids=["GRO001", "GRO007"], quantities=[1, 2])
        lines, entries = self._lines(test_db, session)

        cart = next(e for e in entries if e.event_type == EventType.CART_BUILT.value)
        total = cart.payload["total_paise"]
        rendered = f"₹{total / 100:,.2f}".replace(".00", "")
        assert any(rendered in l["text"] for l in lines), (
            f"no narrative line carries the cart total {rendered}"
        )

    def test_no_line_is_emitted_without_a_seq(self, test_db):
        """A sentence the chain cannot evidence must not appear at all."""
        session = make_session(test_db)          # no ledger entries whatsoever
        lines, _ = self._lines(test_db, session)
        assert lines == []

    def test_operator_labels_cover_every_event_the_lifecycle_emits(self):
        from server.api.narrative import EVENT_LABELS, label_for

        for event in REQUIRED_LIFECYCLE:
            assert event.value in EVENT_LABELS, f"no operator label for {event.value}"
        # Unknown events still render readably rather than blowing up.
        assert label_for("SOME_NEW_EVENT") == "Some new event"


# ── Policy history: the two bugs that silently disabled rules ─────────────────

class TestBuyerHistory:
    """
    Both failures here were silent: no error, no log, the rule simply never
    matched. That is the worst shape a policy bug can take, so each gets a test.
    """

    def _settle(self, db, buyer_id, sku_ids=None, quantities=None, budget=5_000_000):
        session = make_session(db, buyer_id=buyer_id, budget_paise=budget)
        run_allowed_session(db, session, sku_ids=sku_ids, quantities=quantities)
        return session

    def test_history_timestamps_are_utc_not_local(self, test_db):
        """
        created_at is naive UTC; .timestamp() would read it as local time.

        East of UTC that skews every past transaction hours into the past, which
        pushed them all outside VELOCITY's one-hour window and stopped the rule
        firing at all.
        """
        import time as _time
        from server.policy.history import build_buyer_history

        self._settle(test_db, "buyer_tz")
        history = build_buyer_history(test_db, "buyer_tz")
        assert history, "a settled session must appear in history"

        age_seconds = _time.time() - history[0].ts
        assert -60 < age_seconds < 60, (
            f"history timestamp is {age_seconds / 3600:.1f}h off wall clock — "
            "naive datetime read in the local zone"
        )

    def test_history_carries_settled_amount_not_authorised_budget(self, test_db):
        """
        DAILY_CAP sums total_paise and calls it 'today's settled spend'.

        Reading the budget instead counts a buyer with a 50,000 limit who spent
        500 as having spent 50,000, and would deny them far too early.
        """
        from server.mcp.catalog import get_authoritative_price
        from server.policy.history import build_buyer_history

        self._settle(test_db, "buyer_amt", sku_ids=["GRO001"], quantities=[1],
                     budget=5_000_000)
        history = build_buyer_history(test_db, "buyer_amt")

        assert history[0].total_paise == get_authoritative_price("GRO001")
        assert history[0].total_paise != 5_000_000     # not the budget

    def test_history_is_scoped_to_one_buyer(self, test_db):
        from server.policy.history import build_buyer_history

        self._settle(test_db, "buyer_one")
        self._settle(test_db, "buyer_two")
        assert len(build_buyer_history(test_db, "buyer_one")) == 1

    def test_unsettled_sessions_are_excluded(self, test_db):
        from server.policy.history import build_buyer_history

        make_session(test_db, buyer_id="buyer_pending")     # active, never settled
        assert build_buyer_history(test_db, "buyer_pending") == []


class TestIntentJtiReplay:
    """
    A session re-presenting its own intent is not a replay.

    The mandate is registered when the session is created and again when its
    saga runs. Treating any duplicate as a replay made every demo-mode checkout
    fail before it reached the policy engine — which is what made
    MANDATE_INVALID unreachable.
    """

    def _record(self, db, jti, session_id):
        from server.mandate.verifier import record_intent_jti

        return record_intent_jti(
            jti=jti,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=1),
            db=db,
            session_id=session_id,
        )

    def test_same_session_may_re_present_its_own_intent(self, test_db):
        jti = str(uuid.uuid4())
        assert self._record(test_db, jti, "session_a") is True
        assert self._record(test_db, jti, "session_a") is True   # not a replay

    def test_a_different_session_reusing_the_jti_is_a_replay(self, test_db):
        jti = str(uuid.uuid4())
        assert self._record(test_db, jti, "session_a") is True
        assert self._record(test_db, jti, "session_b") is False  # genuine replay


# ── Operator narrative: cart, considered, offers, counterfactuals ─────────────

class TestNarrativeDetail:
    """
    Everything the panel shows is lifted from a payload. These pin that the
    structured detail matches the ledger rather than being recomputed, and that
    the denial counterfactual states the recorded threshold.
    """

    def _lines(self, db, session):
        from server.api.narrative import build_narrative

        entries = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id)
            .order_by(LedgerEntry.seq)
            .all()
        )
        return build_narrative(session, entries)

    def _detail(self, lines, kind):
        return next(
            (l["detail"] for l in lines if l.get("detail", {}).get("kind") == kind),
            None,
        )

    def test_cart_detail_matches_the_cart_built_entry(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session, sku_ids=["GRO001", "GRO007"],
                            quantities=[1, 2])
        detail = self._detail(self._lines(test_db, session), "cart")

        assert detail is not None, "the proposed line must carry the cart"
        by_sku = {i["sku_id"]: i for i in detail["items"]}
        assert by_sku["GRO007"]["quantity"] == 2
        # Line totals come from the ledger, and must add up to the stated total.
        assert sum(i["line_total_paise"] for i in detail["items"]) == detail["total_paise"]

    def test_per_txn_cap_denial_names_the_limit_that_would_have_passed(self, test_db):
        session = make_session(test_db, budget_paise=100)
        with pytest.raises(SagaError):
            run_allowed_session(test_db, session)
        text = " | ".join(l["text"] for l in self._lines(test_db, session))
        assert "Would have passed at ₹1 or less." in text

    def test_counterfactual_is_absent_when_the_rule_has_no_boundary(self, test_db):
        """An ALLOW has nothing to counterfactualise; no line should appear."""
        session = make_session(test_db)
        run_allowed_session(test_db, session)
        text = " | ".join(l["text"] for l in self._lines(test_db, session))
        assert "Would have passed" not in text

    def test_offer_lines_appear_when_an_upsell_is_accepted(self, test_db):
        session = make_session(test_db)
        run_allowed_session(test_db, session, offer_upsell=True)
        text = " | ".join(l["text"] for l in self._lines(test_db, session))

        if "Offer:" in text:                      # an offer was possible
            assert "Offer accepted" in text
            assert "re-signed at the new total" in text
        else:                                     # nothing suitable existed
            assert "No offer was made." in text

    def test_exactly_one_cart_signed_even_after_an_accepted_offer(self, test_db):
        """
        The saga signs once, after the offer.

        Signing inside the upsell as well produced two signatures, the first
        over a cart that had already been superseded.
        """
        session = make_session(test_db)
        run_allowed_session(test_db, session, offer_upsell=True)
        signed = [e for e in events_for(test_db, session.id)
                  if e == EventType.CART_SIGNED.value]
        assert len(signed) == 1

    def test_accepted_offer_stays_inside_the_authorised_budget(self, test_db):
        """The headroom guard is the reason an offer needs no new authorisation."""
        session = make_session(test_db)
        run_allowed_session(test_db, session, offer_upsell=True)
        payloads = {
            e.event_type: (e.payload or {})
            for e in test_db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id).all()
        }
        accepted = payloads.get(EventType.UPSELL_ACCEPTED.value)
        if accepted:
            intent = payloads[EventType.INTENT_SIGNED.value]
            assert accepted["cart_total_after_paise"] <= intent["budget_paise"]
            assert accepted["headroom_remaining_paise"] >= 0


class TestEmptyCartGuard:
    """
    A zero-item cart must never reach the policy engine.

    Every rule would pass it vacuously — no total to weigh, no categories to
    check — so it would arrive at payment carrying an ALLOW verdict.
    """

    def test_cart_builder_refuses_an_empty_cart(self, test_db):
        session = make_session(test_db)
        with pytest.raises(CartBuildError):
            build_authoritative_cart(
                db=test_db, session_id=session.id,
                sku_ids=[], quantities=[], merchant_id=settings.MERCHANT_ID,
            )
        assert events_for(test_db, session.id) == []

    def test_saga_closes_an_empty_cart_as_no_cart_without_evaluating(self, test_db):
        from server.mandate.schema import Cart
        from server.payments.saga import run_saga_harness

        session = make_session(test_db)
        _token, intent = sign_intent(
            buyer_id=session.buyer_id, merchant_id=settings.MERCHANT_ID,
            budget_paise=session.budget_paise, categories=["grocery"],
            max_items=10, estimate_paise=session.budget_paise,
        )
        record_intent_signed(test_db, session.id, intent)
        empty = Cart(merchant_id=settings.MERCHANT_ID, items=[])

        with pytest.raises(SagaError):
            run_saga_harness(db=test_db, session=session, intent=intent,
                             cart=empty, history=[])

        recorded = events_for(test_db, session.id)
        assert EventType.POLICY_EVALUATED.value not in recorded, (
            "an empty cart must not be evaluated"
        )
        assert EventType.SESSION_CLOSED.value in recorded
        test_db.refresh(session)
        assert session.status == "no_cart"


class TestAttachRateProvenance:
    """The attach rate must not present a seed setting as a measured result."""

    def test_seeded_acceptance_is_marked_simulated(self, test_db):
        from server.api import analytics

        session = make_session(test_db)
        run_allowed_session(test_db, session, offer_upsell=True)
        entries = (
            test_db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id).all()
        )
        stats = analytics.upsell_stats(entries)

        if stats["offered"]:
            assert stats["acceptance_is_simulated"] is True
            assert "seed configuration" in stats["acceptance_basis"]

    def test_attach_rate_is_null_with_no_offers(self):
        from server.api import analytics

        stats = analytics.upsell_stats([])
        assert stats["attach_rate"] is None      # undefined, not zero
        assert stats["offered"] == 0


class TestPolicyEngineRefusesCartlessEvaluation:
    """
    A cartless session must never produce a policy decision.

    Every rule is a comparison against the cart, so an empty one satisfies all
    of them vacuously and comes out carrying ALLOW — a verdict meaning "checked
    and permitted" attached to something never checked. Raising is the only safe
    answer; returning DENY would still be a decision about a cart that does not
    exist.
    """

    def _intent(self):
        _token, intent = sign_intent(
            buyer_id="b", merchant_id=settings.MERCHANT_ID, budget_paise=500_000,
            categories=["grocery"], max_items=5, estimate_paise=500_000,
        )
        return intent

    def test_empty_cart_raises_rather_than_returning_a_verdict(self):
        from server.mandate.schema import Cart
        from server.policy.engine import EmptyCartError, evaluate

        with pytest.raises(EmptyCartError):
            evaluate(
                intent=self._intent(),
                cart=Cart(merchant_id=settings.MERCHANT_ID, items=[]),
                history=[],
                mandate_valid=True,
            )

    def test_none_cart_raises(self):
        from server.policy.engine import EmptyCartError, evaluate

        with pytest.raises(EmptyCartError):
            evaluate(intent=self._intent(), cart=None, history=[], mandate_valid=True)

    def test_an_invalid_mandate_does_not_shadow_the_cart_guard(self):
        """The guard runs first: there is nothing to judge either way."""
        from server.mandate.schema import Cart
        from server.policy.engine import EmptyCartError, evaluate

        with pytest.raises(EmptyCartError):
            evaluate(
                intent=self._intent(),
                cart=Cart(merchant_id=settings.MERCHANT_ID, items=[]),
                history=[],
                mandate_valid=False,
            )


class TestNoCartBuilt:
    """The agent returning nothing usable is its own outcome, not a payment failure."""

    def test_records_what_the_model_proposed_and_the_call_that_produced_it(self, test_db):
        from server.ledger.chain import append
        from server.mcp.cart import record_no_cart_built

        session = make_session(test_db)
        append(test_db, session.id, EventType.LLM_CALL, {
            "model": "qwen/qwen3.8-27b", "input_tokens": 1200, "output_tokens": 9,
            "latency_ms": 1500, "purpose": "buyer_propose_cart",
        })
        record_no_cart_built(
            test_db, session.id,
            reason="proposed SKUs absent from the catalog",
            proposed_skus=["NOPE001", "NOPE002"],
            rationale="picked items that felt right",
        )

        entry = next(
            e for e in test_db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id).all()
            if e.event_type == EventType.NO_CART_BUILT.value
        )
        assert entry.payload["proposed_skus"] == ["NOPE001", "NOPE002"]
        # Provenance of the call that produced nothing usable.
        assert entry.payload["model"] == "qwen/qwen3.8-27b"
        assert entry.payload["input_tokens"] == 1200
        assert entry.payload["llm_call_seq"] is not None


class TestCostReportsRealUsage:
    """Token usage exists whether or not a price is configured for the model."""

    def test_usage_is_reported_without_a_configured_rate(self, test_db):
        from server.api import analytics
        from server.ledger.chain import append

        session = make_session(test_db)
        for tin, tout in ((1000, 20), (500, 10)):
            append(test_db, session.id, EventType.LLM_CALL, {
                "model": "qwen/qwen3.8-27b", "input_tokens": tin,
                "output_tokens": tout, "cost_usd_micros": None, "priced": False,
            })
        entries = test_db.query(LedgerEntry).filter(
            LedgerEntry.session_id == session.id).all()
        stats = analytics.cost_stats(entries)

        assert stats["samples"] == 0                    # nothing priced
        assert stats["mean_usd_micros_per_session"] is None
        # ...but the usage is real and reported.
        assert stats["usage"]["llm_calls"] == 2
        assert stats["usage"]["input_tokens"] == 1500
        assert stats["usage"]["output_tokens"] == 30
        assert stats["usage"]["primary_model"] == "qwen/qwen3.8-27b"
        assert stats["usage"]["mean_tokens_per_session"] == 1530

    def test_no_calls_reports_no_model(self):
        from server.api import analytics

        usage = analytics.cost_stats([])["usage"]
        assert usage["llm_calls"] == 0
        assert usage["primary_model"] is None
        assert usage["mean_tokens_per_session"] is None


class TestSagaIsOnePath:
    """
    Live and seeded runs must differ only by injected parameters.

    Two implementations drifted apart until the harness path stopped calling
    verify_cart_mandate at all — a full attack run recorded zero rows in
    mandate_jtis, so every eval result came from a path that skipped the
    enforcement point the system rests on.
    """

    def test_wrappers_delegate_and_hold_no_behaviour(self):
        import inspect
        from server.payments import saga

        for fn in (saga.run_saga_demo, saga.run_saga_harness):
            body = inspect.getsource(fn)
            assert "run_saga(" in body, f"{fn.__name__} must delegate"
            # No branch may re-implement a step of the saga.
            for forbidden in ("evaluate(", "verify_cart_mandate", "append(", "close_session("):
                assert forbidden not in body, (
                    f"{fn.__name__} re-implements {forbidden} instead of delegating"
                )

    def test_every_path_verifies_the_cart_mandate(self, test_db):
        """Even with no client mandate supplied, the verifier must run."""
        from server.db.models import MandateJti

        session = make_session(test_db)
        run_allowed_session(test_db, session)

        carts = test_db.query(MandateJti).filter(MandateJti.jti_type == "cart").count()
        assert carts >= 1, "the saga signed a mandate but never verified it"

    def test_a_forged_mandate_is_denied_end_to_end(self, test_db):
        """
        The verifier is reachable from the saga, not only from unit tests.

        This is the coverage that was missing: the mandate rules were proven in
        isolation while the path that produced eval results never called them.
        """
        from server.payments.saga import run_saga, PaymentMode

        session = make_session(test_db)
        _token, intent = sign_intent(
            buyer_id=session.buyer_id, merchant_id=settings.MERCHANT_ID,
            budget_paise=session.budget_paise, categories=["grocery"],
            max_items=10, estimate_paise=session.budget_paise,
        )
        record_intent_signed(test_db, session.id, intent)
        cart = build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )

        with pytest.raises(SagaError):
            run_saga(
                db=test_db, session=session, intent=intent, cart=cart, history=[],
                payments=PaymentMode.REPLAY,
                cart_token="not.a.valid.jwt",
                offer_upsell=False,
            )

        verdict = next(
            e for e in test_db.query(LedgerEntry)
            .filter(LedgerEntry.session_id == session.id).all()
            if e.event_type == EventType.POLICY_EVALUATED.value
        )
        assert verdict.payload["decision"] == "DENY"
        assert verdict.payload["code"] == "MANDATE_INVALID"
        assert verdict.payload["mandate_valid"] is False


class TestModelTimeout:
    """A slow model must never hold a payment open."""

    def test_timeout_falls_back_to_no_offer_and_is_recorded(self, test_db, monkeypatch):
        from server.agents import upsell as upsellmod
        from server.payments.saga import run_upsell

        def slow(*a, **kw):
            raise upsellmod.UpsellTimeout("Request timed out.")

        monkeypatch.setattr(upsellmod, "suggest_upsell", slow)

        session = make_session(test_db)
        _token, intent = sign_intent(
            buyer_id=session.buyer_id, merchant_id=settings.MERCHANT_ID,
            budget_paise=500_000, categories=["grocery"], max_items=5,
            estimate_paise=500_000,
        )
        cart = build_authoritative_cart(
            db=test_db, session_id=session.id, sku_ids=["GRO001"],
            quantities=[1], merchant_id=settings.MERCHANT_ID,
        )
        returned = run_upsell(test_db, session, intent, cart)

        # Deterministic fallback: the cart is untouched.
        assert returned.total_paise == cart.total_paise
        recorded = events_for(test_db, session.id)
        assert EventType.LLM_TIMEOUT.value in recorded
        assert EventType.UPSELL_ACCEPTED.value not in recorded

    def test_a_timeout_is_not_reported_as_nothing_to_suggest(self):
        from server.agents.upsell import _is_timeout, UpsellTimeout
        import httpx

        assert _is_timeout(httpx.ReadTimeout("timed out"))
        assert _is_timeout(TimeoutError())
        assert not _is_timeout(ValueError("bad json"))
