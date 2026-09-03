"""
What a session actually did, read only from its ledger.

This exists because the seeder and the eval report were both judging scenarios
against flags the scenarios had set on themselves. A scenario carrying
`_simulate_refund: true` was reported as a refund because of that flag, not
because a refund happened — and when the refund leg silently produced nothing,
the seeder still printed `refund 1 ok` while no REFUND_* entry existed anywhere
and no session was in a refunded state.

A scenario's own label is a statement of intent. The ledger is a record of
events. Only the second one can say whether something worked, so only the second
one is consulted here.

The distinction that matters most: a refund that was *attempted and rejected* is
not a refund. REFUND_FAILED means the buyer still has not been repaid, so it
must never satisfy an expectation of "refund" — that is precisely the substitution
this module exists to prevent.
"""

from __future__ import annotations

from server.db.models import LedgerEntry
from server.ledger.events import EventType

# What each expected-outcome label requires the ledger to show.
EXPECTED_DECISION = {
    "allow": "ALLOW",
    "deny": "DENY",
    "escalate": "ESCALATE",
    # "refund" deliberately has no entry: a policy verdict cannot satisfy it.
    # It is judged on whether money actually went back. See `matches()`.
}

# Refund states, in the order they are checked. Only the first two mean the
# buyer got their money.
_MONEY_RETURNED = {"confirmed", "simulated"}


def _entries(db, session_id: str) -> list[LedgerEntry]:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.session_id == session_id)
        .order_by(LedgerEntry.seq)
        .all()
    )


def refund_state(entries: list[LedgerEntry]) -> str:
    """
    none | initiated | failed | pending | confirmed | simulated

    Ordered so that a terminal answer wins over an interim one: a session that
    initiated a refund and then had it rejected is `failed`, not `initiated`.
    """
    types = {e.event_type for e in entries}
    if EventType.REFUND_CONFIRMED.value in types:
        return "confirmed"
    if EventType.REFUND_SIMULATED.value in types:
        return "simulated"
    if EventType.REFUND_PENDING_SETTLEMENT.value in types:
        return "pending"
    if EventType.REFUND_FAILED.value in types:
        return "failed"
    if EventType.REFUND_INITIATED.value in types:
        return "initiated"
    return "none"


def session_outcome(db, session_id: str) -> dict:
    """
    Derive this session's outcome from its own ledger entries.

    Returns decision/code from the last POLICY_EVALUATED, the refund state, and
    a single `outcome` label comparable against a scenario's expectation.
    """
    entries = _entries(db, session_id)
    if not entries:
        return {
            "decision": None, "code": None, "mandate_reason": None,
            "refund_state": "none", "outcome": "error",
            "detail": "no ledger entries",
        }

    verdicts = [
        e for e in entries if e.event_type == EventType.POLICY_EVALUATED.value
    ]
    last = verdicts[-1].payload if verdicts else {}
    decision = (last or {}).get("decision")
    code = (last or {}).get("code")
    refund = refund_state(entries)

    types = {e.event_type for e in entries}
    approved = EventType.HUMAN_APPROVED.value in types
    rejected = EventType.HUMAN_REJECTED.value in types

    if decision is None:
        outcome = "error"
        detail = "no POLICY_EVALUATED entry"
    elif decision == "DENY":
        outcome, detail = "deny", code or "DENY"
    elif decision == "ESCALATE" and not (approved or rejected):
        outcome, detail = "escalate", code or "ESCALATE"
    elif refund in _MONEY_RETURNED:
        outcome, detail = "refund", f"refund {refund}"
    elif refund in ("failed", "pending", "initiated"):
        # Attempted but not completed. The cart was allowed and the money moved
        # out; it has not come back. Reported as allow, with the refund state
        # named so the gap is visible rather than rounded away.
        outcome, detail = "allow", f"refund {refund} — buyer not repaid"
    else:
        outcome, detail = "allow", code or "ALLOW"

    return {
        "decision": decision,
        "code": code,
        "mandate_reason": (last or {}).get("mandate_reason"),
        "refund_state": refund,
        "outcome": outcome,
        "detail": detail,
        "human_approved": approved,
        "human_rejected": rejected,
    }


def matches(expected: str | None, recorded: dict) -> tuple[bool, str]:
    """
    Did the ledger show what the scenario said it would?

    Returns (ok, why_not). `expected` is the scenario's label; `recorded` is a
    session_outcome() result. An unrecognised or absent expectation is not
    silently accepted — an unjudgeable scenario is reported as such.
    """
    if not expected:
        return False, "no expected outcome declared"

    expected = expected.lower()
    recorded_outcome = recorded.get("outcome")

    if expected == "refund":
        # Judged on money, not on the verdict that preceded it.
        if recorded.get("refund_state") in _MONEY_RETURNED:
            return True, ""
        return False, (
            f"expected a completed refund, ledger shows refund_state="
            f"{recorded.get('refund_state')}"
        )

    want = EXPECTED_DECISION.get(expected)
    if want is None:
        return False, f"unrecognised expected outcome {expected!r}"

    if recorded.get("decision") == want:
        return True, ""
    return False, (
        f"expected {want}, ledger recorded {recorded.get('decision') or 'nothing'}"
        + (f" ({recorded_outcome})" if recorded_outcome else "")
    )
