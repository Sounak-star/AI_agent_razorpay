"""
Transaction history for the policy engine.

One place builds the history every rule reads. It used to be written inline at
each call site, and they drifted: the REST checkout path queried the database,
the seeder queried it and injected a synthetic row, and the live demo passed an
empty list — so FIRST_CONTACT_BUYER fired on every live run regardless of what
the buyer had already settled, and the history-based rules could never fire
there at all.

The amount matters as much as the count. DAILY_CAP sums `total_paise` and calls
it "today's settled spend", so that field has to be what was actually settled,
not what was authorised. Reading the budget instead — as every call site did —
counted a buyer with a 50,000 budget who spent 500 as having spent 50,000.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from server.db.models import LedgerEntry, SessionRecord
from server.ledger.events import EventType
from server.policy.rules import TxnHistoryItem

# Statuses that mean money actually moved and came to rest.
SETTLED_STATUSES = ("captured", "refunded")


def _settled_totals(db: Session, session_ids: list[str]) -> dict[str, int]:
    """
    The amount each session actually settled, from its SESSION_CLOSED entry.

    Falls back to the session's budget only when no closing entry recorded a
    total, and that fallback is the old behaviour — kept so a session closed by
    an older build still contributes something rather than vanishing from the
    daily total.
    """
    if not session_ids:
        return {}

    totals: dict[str, int] = {}
    rows = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id.in_(session_ids),
            LedgerEntry.event_type == EventType.SESSION_CLOSED.value,
        )
        .all()
    )
    for row in rows:
        amount = (row.payload or {}).get("final_total_paise")
        if isinstance(amount, int):
            totals[row.session_id] = amount
    return totals


def _epoch(created_at: datetime | None) -> float:
    """
    Session timestamps as a true UTC epoch.

    created_at is written by the database as naive UTC. Calling .timestamp() on
    a naive datetime makes Python interpret it in the *local* zone, so on any
    machine east of UTC every past transaction reads as hours older than it is.
    At UTC+5:30 that is a 5.5-hour skew — enough that VELOCITY's one-hour window
    never contained a single transaction and the rule could not fire at all.
    Nothing failed loudly; the rule just silently never matched.
    """
    if created_at is None:
        return time.time()
    aware = (
        created_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None
        else created_at.astimezone(timezone.utc)
    )
    return aware.timestamp()


def build_buyer_history(db: Session, buyer_id: str) -> list[TxnHistoryItem]:
    """
    Every settled transaction for this buyer, as the policy rules expect it.

    Scoped to one buyer: that is what FIRST_CONTACT_BUYER, DAILY_CAP and
    VELOCITY are all defined over. `ts` is the session's creation time, so the
    rolling windows those rules apply are measured against when the transaction
    actually happened.
    """
    sessions = (
        db.query(SessionRecord)
        .filter(
            SessionRecord.buyer_id == buyer_id,
            SessionRecord.status.in_(SETTLED_STATUSES),
        )
        .all()
    )
    if not sessions:
        return []

    totals = _settled_totals(db, [s.id for s in sessions])

    return [
        TxnHistoryItem(
            session_id=s.id,
            merchant_id=s.merchant_id,
            total_paise=totals.get(s.id, s.budget_paise),
            settled=True,
            ts=_epoch(s.created_at),
        )
        for s in sessions
    ]
