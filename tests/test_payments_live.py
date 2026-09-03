"""
The live payment path: modes, confirmation, and the refund that gets rejected.

Three things are under test here, and only one of them is about payments
succeeding.

The first is provenance. REPLAYED means "these identifiers came out of a real
recorded capture" and SYNTHETIC means "these were generated so the demo would
run". Those are different claims, and a badge that blurs them turns the whole
audit trail into decoration. The tests below assert the boundary in both
directions: a synthetic run must not cite a fixture even when one is sitting on
disk, and a replay run must cite it.

The second is that a refund rejection stays visible. Razorpay returns 400
BAD_REQUEST_ERROR on the refund in this account, cause undetermined, and the
temptation is to fall back to REFUND_SIMULATED so the demo looks whole. That
would put "money returned to the buyer" on a ledger where no money was returned.
The failure is recorded verbatim, the session does not become "refunded", and
the operator narrative says the provider refused.

The third is that nothing here needs the network. Every test runs offline.
"""

from __future__ import annotations

import uuid

import pytest

from server.api import analytics, narrative as narrative_mod
from server.config import settings
from server.db.models import LedgerEntry, SessionRecord
from server.ledger.events import EventType
from server.payments import fixtures
from server.payments.confirm import (
    CaptureResult,
    FixtureConfirmer,
    PollingConfirmer,
    SyntheticConfirmer,
    _payment_id_from_link,
)
from server.payments.razorpay_client import RazorpayError
from server.payments.saga import (
    LivePayment,
    PaymentMode,
    attempt_refund,
    await_live_capture,
)
from tests.test_lifecycle import events_for, make_session

# The shape Razorpay actually returns, taken from the recorded capture rather
# than imagined. A test that asserts against a made-up response shape proves
# only that the code agrees with the test author.
REAL_LINK_PAID = {
    "id": "plink_TWH1ggYqP5mDTC",
    "status": "paid",
    "short_url": "https://rzp.io/rzp/LOhM9u9d",
    "amount": 100,
    "payments": [
        {
            "payment_id": "pay_TWH9Tg3wQsVH5g",
            "status": "captured",
            "amount": 100,
            "method": "card",
            "created_at": 1788157235,
        }
    ],
}

REAL_REFUND_REJECTION = {
    "error": {
        "code": "BAD_REQUEST_ERROR",
        "description": "invalid request sent",
        "metadata": {},
        "reason": "NA",
        "source": "NA",
        "step": "NA",
    }
}


# ── Confirmation ──────────────────────────────────────────────────────────────

class TestConfirmers:
    """The seam that lets a webhook replace the poller without touching the saga."""

    def test_payment_id_is_read_from_the_real_link_shape(self):
        assert _payment_id_from_link(REAL_LINK_PAID) == "pay_TWH9Tg3wQsVH5g"

    def test_unpaid_link_yields_no_payment_id(self):
        assert _payment_id_from_link({"id": "plink_x", "status": "created"}) is None

    def test_poller_returns_the_capture_once_the_link_turns_paid(self, monkeypatch):
        """Two 'created' reads, then 'paid'. The poller keeps going until it lands."""
        responses = [
            {"id": "plink_x", "status": "created"},
            {"id": "plink_x", "status": "created"},
            REAL_LINK_PAID,
        ]
        calls = []

        def fake_fetch(link_id):
            calls.append(link_id)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        monkeypatch.setattr("server.payments.confirm.fetch_payment_link", fake_fetch)
        result = PollingConfirmer(interval_seconds=0).wait_for_capture(
            "plink_x", timeout_seconds=5
        )

        assert result.captured is True
        assert result.payment_id == "pay_TWH9Tg3wQsVH5g"
        assert len(calls) == 3

    def test_poller_gives_up_at_the_deadline_without_claiming_capture(self, monkeypatch):
        monkeypatch.setattr(
            "server.payments.confirm.fetch_payment_link",
            lambda link_id: {"id": link_id, "status": "created"},
        )
        result = PollingConfirmer(interval_seconds=0).wait_for_capture(
            "plink_x", timeout_seconds=0.05
        )

        assert result.captured is False
        assert result.payment_id is None
        assert "no capture" in (result.detail or "")

    def test_a_read_failure_is_not_a_failed_payment(self, monkeypatch):
        """
        A transient API error mid-poll must not be reported as "not paid".

        Treating an unreadable response as a decision would let a blip on the
        network cancel a payment the buyer had already made.
        """
        state = {"n": 0}

        def flaky(link_id):
            state["n"] += 1
            if state["n"] < 3:
                raise RazorpayError("read failed", status_code=502, body={})
            return REAL_LINK_PAID

        monkeypatch.setattr("server.payments.confirm.fetch_payment_link", flaky)
        result = PollingConfirmer(interval_seconds=0).wait_for_capture(
            "plink_x", timeout_seconds=5
        )
        assert result.captured is True
        assert result.payment_id == "pay_TWH9Tg3wQsVH5g"

    def test_a_cancelled_link_ends_the_wait_immediately(self, monkeypatch):
        monkeypatch.setattr(
            "server.payments.confirm.fetch_payment_link",
            lambda link_id: {"id": link_id, "status": "cancelled"},
        )
        result = PollingConfirmer(interval_seconds=0).wait_for_capture(
            "plink_x", timeout_seconds=60
        )
        assert result.captured is False
        assert result.status == "cancelled"
        assert result.waited_seconds < 5     # did not sit out the timeout

    def test_synthetic_confirmer_needs_no_network(self):
        result = SyntheticConfirmer("abcdef1234").wait_for_capture(
            "plink_x", timeout_seconds=1
        )
        assert result.captured is True
        assert result.payment_id.startswith("harness_pay_")

    def test_every_confirmer_satisfies_the_same_protocol(self):
        """What makes the poller swappable for a webhook receiver later."""
        for conf in (PollingConfirmer(), FixtureConfirmer(), SyntheticConfirmer("x")):
            assert callable(conf.wait_for_capture)


# ── Provenance ────────────────────────────────────────────────────────────────

class TestBadgeProvenance:

    def test_synthetic_run_never_claims_fixture_backing(self, test_db):
        """
        The defect this guards against: fixture_path was keyed off whether the
        file existed rather than whether this event was read from it, so a
        synthetic run cited a recording it had never opened.
        """
        session = make_session(test_db)
        _run(test_db, session, PaymentMode.SYNTHETIC)

        for entry in _payment_entries(test_db, session.id):
            payload = entry.payload or {}
            assert payload.get("replayed_from_fixture") is not True, entry.event_type
            assert payload.get("fixture_path") is None, entry.event_type
            assert entry.replayed_from_fixture is False

    def test_synthetic_identifiers_are_recognisable_as_synthetic(self, test_db):
        session = make_session(test_db)
        result = _run(test_db, session, PaymentMode.SYNTHETIC)
        assert result["payment_id"].startswith("harness_")

    @pytest.mark.skipif(
        not fixtures.has("payment"), reason="no recorded capture on disk"
    )
    def test_replay_run_cites_the_recording_it_read(self, test_db):
        session = make_session(test_db)
        _run(test_db, session, PaymentMode.REPLAY)

        captures = [
            e for e in _payment_entries(test_db, session.id)
            if e.event_type in (
                EventType.PAYMENT_SIMULATED.value, EventType.PAYMENT_CAPTURED.value
            )
        ]
        assert captures
        payload = captures[0].payload or {}
        assert payload["replayed_from_fixture"] is True
        assert payload["fixture_path"] == "evals/fixtures/razorpay_capture.json"
        assert payload["razorpay_payment_id"].startswith("pay_")

    @pytest.mark.skipif(
        not fixtures.has("payment"), reason="no recorded capture on disk"
    )
    def test_replay_says_which_fields_it_actually_replayed(self, test_db):
        """
        The ids are real; the amount is this cart's, not the recorded one.

        REPLAYED over a Rs.479 cart would otherwise read as "Rs.479 really
        moved", when the recording is for Rs.1.
        """
        session = make_session(test_db)
        _run(test_db, session, PaymentMode.REPLAY)

        payload = next(
            e.payload for e in _payment_entries(test_db, session.id)
            if e.event_type in (
                EventType.PAYMENT_SIMULATED.value, EventType.PAYMENT_CAPTURED.value
            )
        )
        assert payload["replayed_amount_paise"] == 100
        assert payload["amount_paise"] != payload["replayed_amount_paise"]
        assert "razorpay_payment_id" in payload["replayed_fields"]
        assert "amount_paise" not in payload["replayed_fields"]

    def test_fixture_path_is_repo_relative(self):
        """An absolute home path would be hashed into the chain and shown on screen."""
        path = fixtures.fixture_path_if_backed("payment")
        if path is not None:
            assert not path.startswith("/")
            assert ":" not in path            # no C:\ drive letter
            assert path == "evals/fixtures/razorpay_capture.json"

    def test_unrecorded_kind_gets_no_path(self):
        assert fixtures.fixture_path_if_backed("not_a_kind") is None


# ── The refund that gets rejected ─────────────────────────────────────────────

class TestRefundRejection:

    def test_rejection_is_recorded_verbatim(self, test_db, monkeypatch):
        session = _captured_session(test_db)

        def reject(**kwargs):
            raise RazorpayError(
                "create_refund(pay_x) failed: invalid request sent",
                status_code=400,
                body=REAL_REFUND_REJECTION,
                code="BAD_REQUEST_ERROR",
            )

        monkeypatch.setattr("server.payments.saga.create_refund", reject)
        outcome = attempt_refund(test_db, session, amount_paise=100)

        assert outcome.accepted is False
        failed = _entry(test_db, session.id, EventType.REFUND_FAILED)
        assert failed is not None
        payload = failed.payload
        assert payload["status_code"] == 400
        assert payload["error_code"] == "BAD_REQUEST_ERROR"
        assert payload["razorpay_payment_id"] == "pay_TWH9Tg3wQsVH5g"
        # The whole envelope, not a paraphrase of it.
        assert payload["response_body"] == REAL_REFUND_REJECTION

    def test_rejection_does_not_produce_a_synthetic_refund(self, test_db, monkeypatch):
        """The failure must not be papered over with a refund that did not happen."""
        session = _captured_session(test_db)
        monkeypatch.setattr(
            "server.payments.saga.create_refund",
            _rejecting_refund,
        )
        attempt_refund(test_db, session, amount_paise=100)

        types = events_for(test_db, session.id)
        assert EventType.REFUND_FAILED.value in types
        assert EventType.REFUND_SIMULATED.value not in types
        assert EventType.REFUND_CONFIRMED.value not in types

    def test_session_is_not_marked_refunded_when_the_refund_was_refused(
        self, test_db, monkeypatch
    ):
        session = _captured_session(test_db)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        test_db.refresh(session)
        assert session.status != "refunded"
        assert session.razorpay_refund_id is None

    def test_an_accepted_refund_still_confirms(self, test_db, monkeypatch):
        """The rejection path must not have broken the success path."""
        session = _captured_session(test_db)
        monkeypatch.setattr(
            "server.payments.saga.create_refund",
            lambda **kw: {"id": "rfnd_real", "status": "processed", "amount": 100},
        )
        assert attempt_refund(test_db, session, amount_paise=100).accepted is True

        test_db.refresh(session)
        assert session.razorpay_refund_id == "rfnd_real"
        assert _entry(test_db, session.id, EventType.REFUND_CONFIRMED) is not None
        assert _entry(test_db, session.id, EventType.REFUND_FAILED) is None

    def test_refund_without_a_payment_is_refused_before_calling_the_provider(
        self, test_db
    ):
        session = make_session(test_db)
        with pytest.raises(Exception) as exc:
            attempt_refund(test_db, session, amount_paise=100)
        assert "no payment_id" in str(exc.value)


class TestRefundNarrative:

    def test_operator_is_told_the_provider_refused(self, test_db, monkeypatch):
        session = _captured_session(test_db)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        text = " ".join(line["text"] for line in _narrate(test_db, session))

        assert "rejected by provider" in text
        assert "BAD_REQUEST_ERROR" in text
        # The consequence, stated plainly.
        assert "has not been repaid" in text

    def test_the_rejection_line_links_to_its_ledger_entry(self, test_db, monkeypatch):
        session = _captured_session(test_db)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        failed = _entry(test_db, session.id, EventType.REFUND_FAILED)
        lines = _narrate(test_db, session)
        rejection = [ln for ln in lines if "rejected by provider" in ln["text"]]

        assert rejection, "no rejection line"
        assert rejection[0]["seq"] == failed.seq
        assert rejection[0]["tone"] == "bad"

    def test_no_money_returned_line_appears(self, test_db, monkeypatch):
        """The sentence this whole path exists to prevent."""
        session = _captured_session(test_db)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        text = " ".join(ln["text"] for ln in _narrate(test_db, session))
        assert "returned to the buyer" not in text


# ── Opening and awaiting a live payment ───────────────────────────────────────

class TestLiveCapture:
    """
    The live path with the provider stubbed out.

    What is being tested is the saga's handling of the two outcomes, not
    Razorpay — the real calls were exercised once by scripts/record_fixture.py,
    and their responses are in the fixture.
    """

    def test_capture_records_the_payment_and_the_wait(self, test_db):
        session = make_session(test_db)
        opened = _opened(session)

        result = await_live_capture(
            test_db, session, opened,
            confirmer=_StubConfirmer(CaptureResult(
                captured=True, payment_id="pay_TWH9Tg3wQsVH5g",
                status="paid", waited_seconds=4.0, raw=REAL_LINK_PAID,
            )),
        )

        assert result.captured is True
        test_db.refresh(session)
        assert session.razorpay_payment_id == "pay_TWH9Tg3wQsVH5g"

        entry = _entry(test_db, session.id, EventType.PAYMENT_CAPTURED)
        assert entry is not None
        assert entry.payload["live"] is True
        assert entry.payload["synthetic"] is False
        assert entry.payload["replayed_from_fixture"] is False
        assert entry.payload["waited_seconds"] == 4.0

    def test_a_timeout_records_failure_and_moves_no_money(self, test_db):
        session = make_session(test_db)
        opened = _opened(session)

        result = await_live_capture(
            test_db, session, opened,
            confirmer=_StubConfirmer(CaptureResult(
                captured=False, payment_id=None, status="created",
                waited_seconds=300.0, detail="no capture within 300s",
            )),
        )

        assert result.captured is False
        test_db.refresh(session)
        assert session.razorpay_payment_id is None

        # Nothing is appended to the payment lifecycle: no payment happened.
        assert _entry(test_db, session.id, EventType.PAYMENT_CAPTURED) is None
        assert _entry(test_db, session.id, EventType.PAYMENT_SIMULATED) is None

    def test_awaiting_capture_does_not_close_the_session_itself(self, test_db):
        """Closing belongs to the caller, which knows what comes next."""
        session = make_session(test_db)
        await_live_capture(
            test_db, session, _opened(session),
            confirmer=_StubConfirmer(CaptureResult(
                captured=True, payment_id="pay_x", status="paid", waited_seconds=1.0,
            )),
        )
        assert _entry(test_db, session.id, EventType.SESSION_CLOSED) is None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(db, session, payments: PaymentMode) -> dict:
    """One ALLOW session end to end, in the given payment mode."""
    from datetime import datetime, timezone as _tz

    from server.mandate.issuer import sign_intent
    from server.mcp.cart import build_authoritative_cart, record_intent_signed
    from server.payments.saga import run_saga_harness
    from server.policy.rules import TxnHistoryItem

    _token, intent = sign_intent(
        buyer_id=session.buyer_id,
        merchant_id=settings.MERCHANT_ID,
        budget_paise=session.budget_paise,
        categories=["grocery"],
        max_items=5,
        estimate_paise=session.budget_paise,
    )
    record_intent_signed(db, session.id, intent)
    cart = build_authoritative_cart(
        db=db, session_id=session.id, sku_ids=["GRO001"], quantities=[1],
        merchant_id=settings.MERCHANT_ID,
    )
    history = [TxnHistoryItem(
        session_id="prior", merchant_id=settings.MERCHANT_ID,
        total_paise=10_000, settled=True,
        ts=datetime.now(_tz.utc).timestamp() - 86_400,
    )]
    return run_saga_harness(
        db=db, session=session, intent=intent, cart=cart, history=history,
        offer_upsell=False, payments=payments,
    )


def _narrate(db, session) -> list[dict]:
    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session.id)
        .order_by(LedgerEntry.seq)
        .all()
    )
    return narrative_mod.build_narrative(session, entries)


def _rejecting_refund(**kwargs):
    raise RazorpayError(
        "create_refund(pay_TWH9Tg3wQsVH5g) failed: invalid request sent",
        status_code=400,
        body=REAL_REFUND_REJECTION,
        code="BAD_REQUEST_ERROR",
    )


class _StubConfirmer:
    def __init__(self, result: CaptureResult) -> None:
        self.result = result

    def wait_for_capture(self, payment_link_id, *, timeout_seconds):
        return self.result


def _opened(session: SessionRecord) -> LivePayment:
    return LivePayment(
        order_id="order_TWGyePFj90eobR",
        payment_link_id="plink_TWH1ggYqP5mDTC",
        short_url="https://rzp.io/rzp/LOhM9u9d",
        qr_url=None,
        amount_paise=100,
    )


def _captured_session(db) -> SessionRecord:
    session = make_session(db)
    session.razorpay_payment_id = "pay_TWH9Tg3wQsVH5g"
    session.status = "captured"
    db.commit()
    return session


def _payment_entries(db, session_id) -> list[LedgerEntry]:
    return [
        e for e in db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .all()
        if e.event_type in (
            EventType.ORDER_CREATED.value,
            EventType.PAYMENT_SIMULATED.value,
            EventType.PAYMENT_CAPTURED.value,
        )
    ]


def _entry(db, session_id, event_type: EventType) -> LedgerEntry | None:
    return (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session_id,
            LedgerEntry.event_type == event_type.value,
        )
        .order_by(LedgerEntry.seq)
        .first()
    )


# ── Deferred for settlement, not refused ──────────────────────────────────────

class TestPendingSettlement:
    """
    Razorpay returns the same generic 400 whether the account is short of
    settled balance or the request was genuinely bad:

        {"error": {"code": "BAD_REQUEST_ERROR",
                   "description": "invalid request sent", "metadata": {} ...}}

    The dashboard is what disambiguates it — "Your account does not have
    sufficient balance to instantly refund this payment" — and the payment's
    settlement state is what lets the code reach the same conclusion. These
    tests pin the split, because getting it wrong in either direction is bad:
    calling a real refusal retryable strands the buyer in a retry loop, and
    calling a settlement delay terminal writes off money that is still owed.
    """

    def test_unsettled_payment_is_classified_as_pending_not_failed(
        self, test_db, monkeypatch
    ):
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)

        outcome = attempt_refund(test_db, session, amount_paise=100)

        assert outcome.accepted is False
        assert outcome.pending_settlement is True
        types = events_for(test_db, session.id)
        assert EventType.REFUND_PENDING_SETTLEMENT.value in types
        assert EventType.REFUND_FAILED.value not in types

    def test_pending_entry_carries_body_status_settlement_and_date(
        self, test_db, monkeypatch
    ):
        """All four things the payload is required to record."""
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        p = _entry(test_db, session.id, EventType.REFUND_PENDING_SETTLEMENT).payload
        assert p["response_body"] == REAL_REFUND_REJECTION      # verbatim
        assert p["status_code"] == 400                          # HTTP status
        assert p["settlement"]["status"] == "unsettled"         # settlement status
        assert p["settlement"]["expected_at"] == "2026-09-02"   # expected date
        assert "sufficient balance" in p["provider_dashboard_reason"]

    def test_expected_date_is_labelled_as_derived(self, test_db, monkeypatch):
        """
        Razorpay exposes no per-payment settlement schedule endpoint, so the
        date is computed. The payload has to say so rather than presenting it
        as something the provider returned.
        """
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        s = _entry(test_db, session.id, EventType.REFUND_PENDING_SETTLEMENT).payload[
            "settlement"
        ]
        assert "derived" in s["expected_basis"]

    def test_settlement_status_is_queried_not_guessed(self):
        """The status comes from the provider's own answer, not from the error text."""
        from server.payments.settlement import settlement_state

        import server.payments.razorpay_client as rc
        original = rc.list_settlements
        rc.list_settlements = lambda count=10: {"count": 0, "items": []}
        try:
            state = settlement_state(None)
        finally:
            rc.list_settlements = original

        assert state.status == "unsettled"
        assert "GET /v1/settlements" in state.status_source

    def test_unknown_settlement_state_is_not_treated_as_pending(
        self, test_db, monkeypatch
    ):
        """
        The guard against optimism. If settlement status could not be
        established, the rejection is recorded as a failure — an unverified
        "it will work later" converts a permanent failure into a silent retry
        loop while the buyer waits.
        """
        session = _captured_session(test_db)
        monkeypatch.setattr(
            "server.payments.saga.settlement_state",
            lambda captured_at=None: _state("unknown", None),
        )
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)

        outcome = attempt_refund(test_db, session, amount_paise=100)

        assert outcome.pending_settlement is False
        assert EventType.REFUND_FAILED.value in events_for(test_db, session.id)

    def test_a_non_generic_rejection_stays_terminal(self, test_db, monkeypatch):
        """A different error is a different problem, even on an unsettled payment."""
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)

        def refuse(**kwargs):
            raise RazorpayError(
                "create_refund failed: The refund amount is greater than payment",
                status_code=400,
                body={"error": {
                    "code": "BAD_REQUEST_ERROR",
                    "description": "The refund amount is greater than payment",
                }},
                code="BAD_REQUEST_ERROR",
            )

        monkeypatch.setattr("server.payments.saga.create_refund", refuse)
        outcome = attempt_refund(test_db, session, amount_paise=100)

        assert outcome.pending_settlement is False
        assert EventType.REFUND_FAILED.value in events_for(test_db, session.id)

    def test_narrative_says_not_yet_settled_with_the_date(self, test_db, monkeypatch):
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        text = " ".join(ln["text"] for ln in _narrate(test_db, session))
        assert "payment not yet settled, no balance available" in text
        assert "Expected settlement 2026-09-02" in text
        assert "still owed to the buyer" in text
        # It is not a refusal, so it must not read as one.
        assert "returned to the buyer" not in text


class TestRetryIsNotTerminal:

    def test_session_is_left_open_not_resolved(self, test_db, monkeypatch):
        session = _captured_session(test_db)
        _stub_unsettled(monkeypatch)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
        attempt_refund(test_db, session, amount_paise=100)

        test_db.refresh(session)
        assert session.status not in ("refunded", "closed")
        assert session.razorpay_refund_id is None

    def test_reconciler_does_not_retry_before_settlement(self, test_db, monkeypatch):
        """Retrying early produces the same 400 and buries the real state in noise."""
        from server.payments import reconciler

        _pending_refund_session(test_db, monkeypatch, expected="2099-01-01")
        calls = []

        def counting(**kw):
            calls.append(kw)
            return _rejecting_refund(**kw)

        monkeypatch.setattr("server.payments.saga.create_refund", counting)
        assert reconciler._sweep_pending_refunds(test_db) == 0
        assert calls == []

    def test_reconciler_retries_once_settlement_is_due(self, test_db, monkeypatch):
        from server.payments import reconciler

        session = _pending_refund_session(test_db, monkeypatch, expected="2020-01-01")
        monkeypatch.setattr(settings, "REFUND_RETRY_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(
            "server.payments.saga.create_refund",
            lambda **kw: {"id": "rfnd_after_settlement", "status": "processed"},
        )

        assert reconciler._sweep_pending_refunds(test_db) == 1

        test_db.refresh(session)
        assert session.status == "refunded"
        assert session.razorpay_refund_id == "rfnd_after_settlement"
        types = events_for(test_db, session.id)
        assert EventType.REFUND_RETRY_SCHEDULED.value in types
        assert EventType.REFUND_CONFIRMED.value in types

    def test_a_still_deferred_retry_leaves_the_session_open(self, test_db, monkeypatch):
        from server.payments import reconciler

        session = _pending_refund_session(test_db, monkeypatch, expected="2020-01-01")
        monkeypatch.setattr(settings, "REFUND_RETRY_INTERVAL_SECONDS", 0)
        monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)

        reconciler._sweep_pending_refunds(test_db)

        test_db.refresh(session)
        assert session.status == "refund_pending"     # still open, still owed

    def test_a_settled_session_is_no_longer_a_candidate(self, test_db, monkeypatch):
        from server.payments import reconciler

        _pending_refund_session(test_db, monkeypatch, expected="2020-01-01")
        monkeypatch.setattr(settings, "REFUND_RETRY_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(
            "server.payments.saga.create_refund",
            lambda **kw: {"id": "rfnd_x", "status": "processed"},
        )
        reconciler._sweep_pending_refunds(test_db)
        # Second pass must not refund twice.
        assert reconciler._sweep_pending_refunds(test_db) == 0

    def test_stale_sweep_never_touches_a_session_awaiting_settlement(
        self, test_db, monkeypatch
    ):
        """
        Waiting on a settlement cycle is waiting by design, exactly like waiting
        on a human. Sweeping it as stalled would close a session that is still
        owed money.
        """
        from server.payments import reconciler

        session = _pending_refund_session(test_db, monkeypatch, expected="2099-01-01")
        monkeypatch.setattr(settings, "STALE_SESSION_TIMEOUT_SECONDS", 0)

        reconciler._sweep_stalled_sessions(test_db)

        test_db.refresh(session)
        assert session.status == "refund_pending"
        assert EventType.SESSION_STALE.value not in events_for(test_db, session.id)


# ── Settlement helpers ────────────────────────────────────────────────────────

def _state(status: str, expected: str | None):
    from server.payments.settlement import SettlementState

    return SettlementState(
        status=status,
        status_source="stubbed for test",
        expected_at=expected,
        expected_basis="stubbed for test (derived)",
        settled_count=0 if status == "unsettled" else None,
    )


def _stub_unsettled(monkeypatch, expected: str = "2026-09-02"):
    """The verified real condition: captured, nothing settled on the account."""
    monkeypatch.setattr(
        "server.payments.saga.settlement_state",
        lambda captured_at=None: _state("unsettled", expected),
    )


def _pending_refund_session(db, monkeypatch, *, expected: str) -> SessionRecord:
    """A session already sitting in REFUND_PENDING_SETTLEMENT."""
    session = _captured_session(db)
    _stub_unsettled(monkeypatch, expected=expected)
    monkeypatch.setattr("server.payments.saga.create_refund", _rejecting_refund)
    attempt_refund(db, session, amount_paise=100)
    session.status = "refund_pending"
    db.commit()
    return session


# ── Approving an escalation must actually settle ──────────────────────────────

class TestApproveSettles:
    """
    The defect these exist for: approving an escalation recorded HUMAN_APPROVED,
    set the session back to "active", and then did nothing whatsoever. Sixty
    seconds later the reconciler correctly swept the idle session to STALE. The
    ledger read HUMAN_APPROVED -> SESSION_STALE with no payment in between, and
    an operator who clicked APPROVE saw the card disappear and no transaction
    happen.

    A human approval is an authorisation to move money. If it does not move
    money, the button is decoration — so these tests assert the settlement, not
    just the decision.
    """

    def test_approve_settles_and_closes_the_session(self, client, test_db, monkeypatch):
        monkeypatch.setattr(settings, "PAYMENTS_MODE", "synthetic")
        session, esc = _escalated_session(test_db)

        r = client.post(
            f"/sessions/{session.id}/escalations/{esc.id}/approve",
            json={"resolved_by": "test_operator"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"
        assert body["settled"] is True
        assert body["razorpay_payment_id"]

        types = events_for(test_db, session.id)
        assert EventType.HUMAN_APPROVED.value in types
        assert EventType.ORDER_CREATED.value in types
        assert EventType.SESSION_CLOSED.value in types

    def test_approved_session_does_not_go_stale(self, client, test_db, monkeypatch):
        """The observable symptom: HUMAN_APPROVED followed by SESSION_STALE."""
        from server.payments import reconciler

        monkeypatch.setattr(settings, "PAYMENTS_MODE", "synthetic")
        monkeypatch.setattr(settings, "STALE_SESSION_TIMEOUT_SECONDS", 0)
        session, esc = _escalated_session(test_db)

        client.post(
            f"/sessions/{session.id}/escalations/{esc.id}/approve",
            json={"resolved_by": "test_operator"},
        )
        reconciler._sweep_stalled_sessions(test_db)

        test_db.refresh(session)
        assert session.status == "captured"
        assert EventType.SESSION_STALE.value not in events_for(test_db, session.id)

    def test_approval_settles_the_cart_the_human_saw(self, client, test_db, monkeypatch):
        """
        The amount paid must be the escalation's snapshot, not a cart rebuilt
        afterwards — the snapshot is what a person actually looked at.
        """
        monkeypatch.setattr(settings, "PAYMENTS_MODE", "synthetic")
        session, esc = _escalated_session(test_db)
        approved_total = esc.cart_snapshot["total_paise"]

        body = client.post(
            f"/sessions/{session.id}/escalations/{esc.id}/approve",
            json={"resolved_by": "test_operator"},
        ).json()

        assert body["amount_paise"] == approved_total

    def test_rejection_still_moves_no_money(self, client, test_db, monkeypatch):
        monkeypatch.setattr(settings, "PAYMENTS_MODE", "synthetic")
        session, esc = _escalated_session(test_db)

        client.post(
            f"/sessions/{session.id}/escalations/{esc.id}/reject",
            json={"resolved_by": "test_operator"},
        )

        types = events_for(test_db, session.id)
        assert EventType.HUMAN_REJECTED.value in types
        assert EventType.ORDER_CREATED.value not in types
        assert EventType.PAYMENT_SIMULATED.value not in types
        test_db.refresh(session)
        assert session.status == "failed"

    def test_synthetic_approval_makes_no_network_call(
        self, client, test_db, monkeypatch
    ):
        """The stage-demo guarantee, asserted at the approve endpoint."""
        monkeypatch.setattr(settings, "PAYMENTS_MODE", "synthetic")

        def explode(*a, **kw):
            raise AssertionError("a live Razorpay call was attempted")

        # Patched at the client, which is the one chokepoint every caller
        # goes through — saga imports some of these lazily.
        for name in ("create_order", "create_payment_link", "create_refund",
                     "fetch_payment_link", "list_settlements"):
            monkeypatch.setattr(f"server.payments.razorpay_client.{name}", explode)
        monkeypatch.setattr("server.payments.saga.create_payment_link", explode)
        monkeypatch.setattr("server.payments.saga.create_refund", explode)

        session, esc = _escalated_session(test_db)
        r = client.post(
            f"/sessions/{session.id}/escalations/{esc.id}/approve",
            json={"resolved_by": "test_operator"},
        )
        assert r.status_code == 200
        assert r.json()["settled"] is True


def _escalated_session(db):
    """A session parked on a pending escalation, as the dashboard would show it."""
    from server.db.models import EscalationRequest
    from server.mandate.issuer import sign_intent
    from server.mcp.cart import build_authoritative_cart, record_intent_signed

    session = make_session(db, buyer_id=f"buyer_{uuid.uuid4().hex[:8]}")
    _token, intent = sign_intent(
        buyer_id=session.buyer_id,
        merchant_id=settings.MERCHANT_ID,
        budget_paise=session.budget_paise,
        categories=["grocery"],
        max_items=5,
        estimate_paise=session.budget_paise,
    )
    record_intent_signed(db, session.id, intent)
    cart = build_authoritative_cart(
        db=db, session_id=session.id, sku_ids=["GRO001"], quantities=[1],
        merchant_id=settings.MERCHANT_ID,
    )
    esc = EscalationRequest(
        id=str(uuid.uuid4()),
        session_id=session.id,
        reason_code="FIRST_CONTACT_BUYER",
        detail="buyer has never settled a transaction with this merchant",
        intent_snapshot=intent.model_dump(),
        cart_snapshot={
            "merchant_id": cart.merchant_id,
            "items": [i.model_dump() for i in cart.items],
            "total_paise": cart.total_paise,
        },
        status="pending",
    )
    db.add(esc)
    session.status = "escalated"
    db.commit()
    return session, esc


# ── Provenance badges belong only on provider events ──────────────────────────

class TestProvenanceScope:
    """
    SESSION_CLOSED was inheriting REPLAYED from the settlement before it, so the
    closing entry claimed to have been read out of a recorded capture. It is
    written locally in every mode and has no provider behind it.
    """

    def test_session_closed_never_carries_a_badge(self, test_db):
        session = make_session(test_db)
        _run(test_db, session, PaymentMode.REPLAY)

        closed = _entry(test_db, session.id, EventType.SESSION_CLOSED)
        assert closed is not None
        assert closed.replayed_from_fixture is False

    def test_chain_strips_the_flag_from_a_non_provider_event(self, test_db):
        """Enforced centrally, so a new call site cannot reintroduce the leak."""
        from server.ledger.chain import append

        session = make_session(test_db)
        entry = append(
            test_db, session.id, EventType.POLICY_EVALUATED,
            {"decision": "ALLOW"},
            replayed_from_fixture=True,          # a caller getting it wrong
        )
        assert entry.replayed_from_fixture is False

    def test_chain_keeps_the_flag_on_a_provider_event(self, test_db):
        from server.ledger.chain import append

        session = make_session(test_db)
        entry = append(
            test_db, session.id, EventType.PAYMENT_SIMULATED,
            {"amount_paise": 100},
            replayed_from_fixture=True,
        )
        assert entry.replayed_from_fixture is True

    def test_only_provider_events_are_badge_bearing(self):
        from server.ledger.chain import PROVENANCE_BEARING_EVENTS

        for name in PROVENANCE_BEARING_EVENTS:
            assert (
                name.startswith("ORDER_")
                or name.startswith("PAYMENT_")
                or name.startswith("REFUND_")
            ), name


# ── An incoherent recording is not replayable ─────────────────────────────────

class TestFixtureCoherence:
    """
    Every field can come from the real API while the set as a whole describes a
    transaction that never happened. A live payment-link test overwrote the
    stored order and link, leaving an unpaid Rs.799 order beside a Rs.1 payment
    from a different order — and a replay of that still wore a REPLAYED badge.
    """

    def test_mismatched_order_and_payment_is_flagged(self, monkeypatch):
        monkeypatch.setattr(fixtures, "get", lambda kind: {
            "order": {"response": {"id": "order_AAA"}},
            "payment_link": {"response": {"id": "plink_X", "status": "paid"}},
            "payment": {"response": {"id": "pay_X", "order_id": "order_BBB",
                                     "status": "captured"}},
        }.get(kind))

        problem = fixtures.coherence_problem()
        assert problem is not None
        assert "order_BBB" in problem and "order_AAA" in problem

    def test_uncaptured_payment_is_flagged(self, monkeypatch):
        monkeypatch.setattr(fixtures, "get", lambda kind: {
            "order": {"response": {"id": "order_AAA"}},
            "payment": {"response": {"id": "pay_X", "order_id": "order_AAA",
                                     "status": "failed"}},
        }.get(kind))
        assert "not captured" in (fixtures.coherence_problem() or "")

    def test_the_real_recording_is_coherent(self):
        if not fixtures.has("payment"):
            pytest.skip("no recorded capture on disk")
        assert fixtures.coherence_problem() is None

    def test_replay_falls_back_to_synthetic_when_incoherent(self, test_db, monkeypatch):
        monkeypatch.setattr(
            fixtures, "coherence_problem",
            lambda: "payment belongs to a different order",
        )
        session = make_session(test_db)
        result = _run(test_db, session, PaymentMode.REPLAY)

        assert result["payment_id"].startswith("harness_")
        assert result["replayed_from_fixture"] is False

    def test_recording_is_off_unless_explicitly_enabled(self, monkeypatch, tmp_path):
        """A live call must not overwrite the recording as a side effect."""
        target = tmp_path / "capture.json"
        monkeypatch.setattr(fixtures, "FIXTURE_PATH", target)

        fixtures.record("order", {"id": "order_should_not_persist"})
        assert not target.exists()

        with fixtures.recording():
            fixtures.record("order", {"id": "order_deliberate"})
        assert target.exists()


# ── Latency attributes time to whoever spent it ───────────────────────────────

class TestLatencyAttribution:
    """
    Approving two escalations by hand pushed the reported engine mean from 245ms
    to 34.4s. Nothing had got slower — a person took six minutes to click a
    button, and that was being counted as compute.
    """

    def test_human_decision_time_is_excluded_from_engine_latency(self, test_db):
        session = _session_with_wait(
            test_db,
            EventType.ESCALATED, EventType.HUMAN_APPROVED,
            gap_seconds=300,
        )
        stats = analytics.latency_stats([session], _entries(test_db, session.id))

        assert stats["mean_ms"] > 250_000            # wall clock includes it
        assert stats["engine_mean_ms"] < 5_000       # engine time does not
        assert stats["human_wait"]["mean_ms"] > 250_000
        assert stats["human_wait"]["samples"] == 1

    def test_provider_wait_is_excluded_and_reported_separately(self, test_db):
        session = _session_with_wait(
            test_db,
            EventType.ORDER_CREATED, EventType.PAYMENT_CAPTURED,
            gap_seconds=120,
            first_payload={"awaiting_capture": True},
        )
        stats = analytics.latency_stats([session], _entries(test_db, session.id))

        assert stats["engine_mean_ms"] < 5_000
        assert stats["provider_wait"]["mean_ms"] > 100_000
        assert stats["provider_wait"]["samples"] == 1

    def test_a_session_with_no_waiting_reports_none(self, test_db):
        session = make_session(test_db)
        _run(test_db, session, PaymentMode.SYNTHETIC)
        stats = analytics.latency_stats([session], _entries(test_db, session.id))

        assert stats["human_wait"]["samples"] == 0
        assert stats["provider_wait"]["samples"] == 0
        # With nothing to exclude, engine time is the whole span.
        assert stats["engine_mean_ms"] == stats["mean_ms"]

    def test_engine_time_is_never_negative(self, test_db):
        """Overlapping waits must not drive the subtraction below zero."""
        session = _session_with_wait(
            test_db, EventType.ESCALATED, EventType.HUMAN_APPROVED,
            gap_seconds=10_000,
        )
        stats = analytics.latency_stats([session], _entries(test_db, session.id))
        assert stats["engine_mean_ms"] >= 0


def _entries(db, session_id):
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .all()
    )


def _session_with_wait(
    db, first: EventType, second: EventType, *, gap_seconds: int,
    first_payload: dict | None = None,
):
    """
    A settled session whose two bracketing entries are `gap_seconds` apart.

    The timestamps are written directly because the wait being measured is
    wall-clock time this test is not going to spend.
    """
    from datetime import datetime, timedelta, timezone

    from server.ledger.chain import append

    session = make_session(db)
    append(db, session.id, EventType.POLICY_EVALUATED, {"decision": "ALLOW"})
    a = append(db, session.id, first, first_payload or {})
    b = append(db, session.id, second, {})
    append(db, session.id, EventType.SESSION_CLOSED, {"final_status": "captured"})

    start = datetime.now(timezone.utc)
    for entry, offset in ((a, 0), (b, gap_seconds)):
        entry.ts = (start + timedelta(seconds=offset)).isoformat(timespec="milliseconds")
    closing = _entry(db, session.id, EventType.SESSION_CLOSED)
    closing.ts = (start + timedelta(seconds=gap_seconds)).isoformat(timespec="milliseconds")
    session.status = "captured"
    db.commit()
    return session


# ── No shadowed definitions ───────────────────────────────────────────────────

class TestNoDuplicateDefinitions:
    """
    Twice now a refactor has left two definitions of the same function in
    saga.py, and Python silently bound the later one. Both times the stale copy
    won and quietly reinstated old behaviour: once an attempt_refund that
    returned a bool instead of classifying the rejection, once a _settle_replay
    with no fixture-coherence guard. Neither failed loudly; the tests that
    caught them were testing something else.
    """

    @pytest.mark.parametrize("module_path", [
        "server/payments/saga.py",
        "server/payments/reconciler.py",
        "server/api/routes.py",
        "server/api/analytics.py",
        "server/api/narrative.py",
        "server/payments/fixtures.py",
        "server/payments/confirm.py",
        "server/payments/settlement.py",
    ])
    def test_no_top_level_name_is_defined_twice(self, module_path):
        import ast as _ast
        from collections import Counter
        from pathlib import Path as _Path

        tree = _ast.parse(_Path(module_path).read_text(encoding="utf-8"))
        names = [
            node.name for node in tree.body
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
        ]
        duplicates = [n for n, count in Counter(names).items() if count > 1]
        assert not duplicates, (
            f"{module_path} defines {duplicates} more than once; "
            f"the later definition silently shadows the earlier one"
        )
