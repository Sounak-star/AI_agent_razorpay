"""
Append-only hash-chained ledger.

Design:
  - Each entry carries prev_hash and hash = sha256(canonical_json(entry_without_hash)).
  - Genesis entry has prev_hash = "0" * 64.
  - Canonical JSON: sorted keys, no whitespace, UTF-8 — deterministic across platforms.
  - Write-ahead pattern: the row is inserted (without hash), the hash is computed,
    then the row is updated with the hash in a single transaction.
    This ensures the hash is always present when the transaction commits.
  - verify_chain() re-derives every hash and checks linkage end-to-end.

Tamper detection:
  - POST /ledger/tamper (env-gated) mutates one payload in-place.
  - GET /ledger/verify will then return {valid: false, broken_at_seq: N}.
  - This is the live demo of the integrity guarantee.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.db.models import LedgerEntry
from server.ledger.events import EventType


# ── Hash helpers ──────────────────────────────────────────────────────────────

_GENESIS_HASH = "0" * 64


def _canonical_json(d: dict) -> bytes:
    """Deterministic JSON serialisation used for hashing."""
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _entry_dict_for_hashing(entry: LedgerEntry) -> dict:
    """Build the dict representation of an entry that is fed into sha256."""
    return {
        "seq": entry.seq,
        "ts": entry.ts,
        "session_id": entry.session_id,
        "event_type": entry.event_type,
        "payload": entry.payload,
        "prev_hash": entry.prev_hash,
        "replayed_from_fixture": entry.replayed_from_fixture,
    }


def _compute_hash(entry: LedgerEntry) -> str:
    return hashlib.sha256(_canonical_json(_entry_dict_for_hashing(entry))).hexdigest()


# ── Append ────────────────────────────────────────────────────────────────────

# Events that may carry a provenance badge: the ones that describe something a
# payment provider actually did. Everything else is written locally whatever the
# mode, so REPLAYED on it would be a claim about a file it never touched.
PROVENANCE_BEARING_EVENTS = frozenset({
    EventType.ORDER_CREATED.value,
    EventType.PAYMENT_CAPTURED.value,
    EventType.PAYMENT_SIMULATED.value,
    EventType.REFUND_INITIATED.value,
    EventType.REFUND_CONFIRMED.value,
    EventType.REFUND_SIMULATED.value,
    EventType.REFUND_FAILED.value,
    EventType.REFUND_PENDING_SETTLEMENT.value,
    EventType.REFUND_RETRY_SCHEDULED.value,
})


def append(
    db: Session,
    session_id: str,
    event_type: EventType | str,
    payload: dict[str, Any],
    replayed_from_fixture: bool = False,
) -> LedgerEntry:
    """
    Append an entry to the ledger and return the committed row.

    Thread safety note: the seq allocation uses MAX(seq) + 1 inside the same
    transaction. Under SQLite (WAL mode) this is safe for the demo; under
    Postgres, the UNIQUE constraint on seq handles concurrent appenders.
    """
    event_type_str = event_type.value if isinstance(event_type, EventType) else event_type

    # A provenance badge only means something on an event a payment provider
    # produced. Enforced here rather than at each call site, because it already
    # leaked once: close_session passed the settlement's flag straight through,
    # so SESSION_CLOSED wore a REPLAYED badge claiming the closing entry had
    # been read out of a recorded capture. It is written locally, in every mode.
    if replayed_from_fixture and event_type_str not in PROVENANCE_BEARING_EVENTS:
        replayed_from_fixture = False

    # Determine seq and prev_hash atomically
    max_seq_row = db.execute(select(func.max(LedgerEntry.seq))).scalar()
    if max_seq_row is None:
        seq = 1
        prev_hash = _GENESIS_HASH
    else:
        seq = max_seq_row + 1
        last_entry = db.query(LedgerEntry).filter(LedgerEntry.seq == max_seq_row).one()
        prev_hash = last_entry.hash or _GENESIS_HASH

    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    entry = LedgerEntry(
        seq=seq,
        ts=ts,
        session_id=session_id,
        event_type=event_type_str,
        payload=payload,
        prev_hash=prev_hash,
        hash=None,                       # set after insert
        replayed_from_fixture=replayed_from_fixture,
    )
    db.add(entry)
    db.flush()   # populate entry.id so we can compute the hash

    entry.hash = _compute_hash(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ── Chain verification ─────────────────────────────────────────────────────────

def verify_chain(db: Session) -> dict:
    """
    Re-derive every hash and check the chain linkage.

    Returns:
        {"valid": True} on success
        {"valid": False, "broken_at_seq": N} on first mismatch
    """
    entries = db.query(LedgerEntry).order_by(LedgerEntry.seq).all()

    if not entries:
        return {"valid": True, "broken_at_seq": None}

    expected_prev = _GENESIS_HASH

    for entry in entries:
        # Check prev_hash linkage
        if entry.prev_hash != expected_prev:
            return {"valid": False, "broken_at_seq": entry.seq}

        # Re-derive hash
        expected_hash = _compute_hash(entry)
        if entry.hash != expected_hash:
            return {"valid": False, "broken_at_seq": entry.seq}

        expected_prev = entry.hash

    return {"valid": True, "broken_at_seq": None}
