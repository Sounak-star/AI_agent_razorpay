"""
Ledger chain tests.

Covers:
  - Genesis entry (prev_hash = "0"*64)
  - Sequential append with correct linkage
  - verify_chain() returning valid on clean ledger
  - verify_chain() returning broken_at_seq after in-place mutation
  - replayed_from_fixture flag is preserved and hashed
  - Canonical JSON is deterministic (no import order sensitivity)
"""

from __future__ import annotations

import json

import pytest

from server.ledger.chain import _GENESIS_HASH, _canonical_json, _entry_dict_for_hashing, append, verify_chain
from server.ledger.events import EventType


# ── Append + linkage ──────────────────────────────────────────────────────────

class TestAppend:
    def test_first_entry_uses_genesis_hash(self, test_db):
        entry = append(test_db, "sess_1", EventType.INTENT_SIGNED, {"buyer_id": "b1"})
        assert entry.seq == 1
        assert entry.prev_hash == _GENESIS_HASH
        assert entry.hash is not None
        assert len(entry.hash) == 64

    def test_second_entry_chains_to_first(self, test_db):
        e1 = append(test_db, "sess_1", EventType.INTENT_SIGNED, {})
        e2 = append(test_db, "sess_1", EventType.CATALOG_QUERIED, {})
        assert e2.prev_hash == e1.hash
        assert e2.seq == 2

    def test_seq_is_monotonic_across_sessions(self, test_db):
        e1 = append(test_db, "sess_A", EventType.INTENT_SIGNED, {})
        e2 = append(test_db, "sess_B", EventType.INTENT_SIGNED, {})
        e3 = append(test_db, "sess_A", EventType.CART_BUILT, {})
        assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]

    def test_replayed_fixture_flag_stored(self, test_db):
        entry = append(
            test_db, "sess_1", EventType.PAYMENT_SIMULATED,
            {"order_id": "order_test"},
            replayed_from_fixture=True,
        )
        assert entry.replayed_from_fixture is True

    def test_hash_changes_when_payload_changes(self, test_db):
        e1 = append(test_db, "s", EventType.CART_BUILT, {"x": 1})
        test_db.close()

        # Start fresh DB
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker, Session
        from server.db.models import Base
        eng2 = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng2)
        db2 = sessionmaker(bind=eng2, class_=Session)()

        e2 = append(db2, "s", EventType.CART_BUILT, {"x": 2})
        assert e1.hash != e2.hash
        db2.close()
        eng2.dispose()


# ── verify_chain ──────────────────────────────────────────────────────────────

class TestVerifyChain:
    def test_empty_ledger_is_valid(self, test_db):
        result = verify_chain(test_db)
        assert result == {"valid": True, "broken_at_seq": None}

    def test_clean_chain_is_valid(self, test_db):
        for evt in [EventType.INTENT_SIGNED, EventType.CATALOG_QUERIED, EventType.CART_BUILT]:
            append(test_db, "sess_1", evt, {})
        result = verify_chain(test_db)
        assert result["valid"] is True
        assert result["broken_at_seq"] is None

    def test_tampered_payload_detected(self, test_db):
        e1 = append(test_db, "sess_1", EventType.INTENT_SIGNED, {"buyer": "honest"})
        e2 = append(test_db, "sess_1", EventType.CART_BUILT, {})

        # Mutate e1's payload in-place (simulating /ledger/tamper)
        from server.db.models import LedgerEntry
        row = test_db.query(LedgerEntry).filter(LedgerEntry.seq == e1.seq).one()
        row.payload = {"buyer": "attacker"}
        test_db.commit()

        result = verify_chain(test_db)
        assert result["valid"] is False
        assert result["broken_at_seq"] == e1.seq

    def test_tampered_hash_field_detected(self, test_db):
        e1 = append(test_db, "sess_1", EventType.INTENT_SIGNED, {})
        append(test_db, "sess_1", EventType.CART_BUILT, {})

        # Directly corrupt the stored hash
        from server.db.models import LedgerEntry
        row = test_db.query(LedgerEntry).filter(LedgerEntry.seq == e1.seq).one()
        row.hash = "a" * 64
        test_db.commit()

        result = verify_chain(test_db)
        assert result["valid"] is False
        assert result["broken_at_seq"] == e1.seq

    def test_broken_chain_reports_first_bad_seq(self, test_db):
        """If two entries are corrupted, broken_at_seq should be the earlier one."""
        for _ in range(5):
            append(test_db, "sess_1", EventType.CATALOG_QUERIED, {})

        from server.db.models import LedgerEntry
        for seq in [2, 4]:
            row = test_db.query(LedgerEntry).filter(LedgerEntry.seq == seq).one()
            row.payload = {"corrupted": True}
            test_db.commit()

        result = verify_chain(test_db)
        assert result["valid"] is False
        assert result["broken_at_seq"] == 2  # first corruption


# ── Canonical JSON ─────────────────────────────────────────────────────────────

class TestCanonicalJson:
    def test_key_order_is_deterministic(self):
        d1 = {"b": 2, "a": 1}
        d2 = {"a": 1, "b": 2}
        assert _canonical_json(d1) == _canonical_json(d2)

    def test_no_whitespace(self):
        result = _canonical_json({"k": "v"}).decode()
        assert " " not in result
        assert "\n" not in result

    def test_nested_dicts_also_sorted(self):
        d = {"outer": {"z": 3, "a": 1}}
        raw = _canonical_json(d).decode()
        parsed = json.loads(raw)
        assert list(parsed["outer"].keys()) == sorted(parsed["outer"].keys())
