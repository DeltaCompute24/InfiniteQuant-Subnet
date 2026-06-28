"""Genesis commitment backfill (late-validator catch-up).

Covers the pure window logic and the persisted-cursor round-trip. Stubs
bittensor/timelock and chain.Chain so it runs without a live chain or network.

    pytest tests/test_backfill.py -q
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# stub the heavy/native deps the validator imports at module top (same pattern as
# tests/test_protocol.py) so importing neurons.validator works on a dev box.
if "bittensor" not in sys.modules:
    try:
        import bittensor  # noqa: F401
    except ImportError:
        sys.modules["bittensor"] = types.ModuleType("bittensor")
if "timelock" not in sys.modules:
    try:
        import timelock  # noqa: F401
    except ImportError:
        _tl = types.ModuleType("timelock")
        _tl.Timelock = type("Timelock", (), {"__init__": lambda self, *a, **k: None})
        sys.modules["timelock"] = _tl

from sn89_signals import chain, config  # noqa: E402
import neurons.validator as V  # noqa: E402


# ── pure window logic ────────────────────────────────────────────────────────
class TestBackfillWindow:
    def test_marches_from_genesis_to_head_in_bounded_chunks(self):
        cursor, head, chunk = 0, 1000, 120
        seen = []
        while True:
            win = V.Validator._backfill_window(cursor, head, chunk)
            if win is None:
                break
            frm, to = win
            assert frm == cursor                 # contiguous, no gaps
            assert to <= head                    # never overshoots head
            assert to - frm <= chunk             # bounded per poll
            seen.append((frm, to))
            cursor = to
        # full coverage genesis→head, terminates exactly at head
        assert seen[0][0] == 0 and seen[-1][1] == head
        assert all(seen[i][1] == seen[i + 1][0] for i in range(len(seen) - 1))

    def test_none_when_caught_up(self):
        assert V.Validator._backfill_window(1000, 1000, 120) is None
        assert V.Validator._backfill_window(1001, 1000, 120) is None  # past head

    def test_last_window_clips_to_head_not_chunk(self):
        # cursor 950, head 1000, chunk 120 → window (950, 1000], not (950, 1070]
        assert V.Validator._backfill_window(950, 1000, 120) == (950, 1000)

    def test_respects_genesis_floor(self):
        # a backfill that starts at a real registration block stays >= it
        win = V.Validator._backfill_window(8_000_000, 8_000_500, 120)
        assert win == (8_000_000, 8_000_120)


# ── persisted cursor round-trip ──────────────────────────────────────────────
class _FakeChain:
    def __init__(self): pass


@pytest.fixture
def validator(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "v.db"))
    monkeypatch.setattr(chain, "Chain", _FakeChain)
    monkeypatch.setattr(config, "SCAN_GENESIS_BLOCK", 8_000_000)
    return V.Validator(wallet=None)


class TestBackfillPersistence:
    def test_fresh_db_starts_at_genesis_block(self, validator):
        # a brand-new validator backfills from the configured genesis, not block-1
        assert validator._backfill_block == 8_000_000

    def test_cursor_persists_across_restart(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "v.db"))
        monkeypatch.setattr(chain, "Chain", _FakeChain)
        monkeypatch.setattr(config, "SCAN_GENESIS_BLOCK", 8_000_000)
        v1 = V.Validator(wallet=None)
        v1._backfill_block = 8_000_360
        v1._save_backfill(8_000_360)
        v1.db.commit()
        v1.db.close()
        # a restart on the same DB resumes from the saved cursor, not genesis
        v2 = V.Validator(wallet=None)
        assert v2._backfill_block == 8_000_360

    def test_save_is_idempotent_upsert(self, validator):
        validator._save_backfill(8_000_120)
        validator._save_backfill(8_000_240)
        validator.db.commit()
        rows = validator.db.execute(
            "SELECT value FROM vali_state WHERE key='backfill_block'").fetchall()
        assert rows == [(8_000_240,)]   # single row, latest value


# ── async drain: worker → queue → main-loop journaling ───────────────────────
class TestDrainBackfill:
    def test_drain_moves_all_windows_into_sources_in_order(self, validator):
        validator._bf_queue.append((8_000_120, [{"commit": "a"}, {"commit": "b"}]))
        validator._bf_queue.append((8_000_240, [{"commit": "c"}]))
        sources = [{"commit": "snapshot"}]
        drained_to = validator._drain_backfill(sources)
        assert drained_to == 8_000_240                 # highest block drained → cursor
        assert [s["commit"] for s in sources] == ["snapshot", "a", "b", "c"]
        assert len(validator._bf_queue) == 0           # queue emptied

    def test_drain_empty_returns_none(self, validator):
        assert validator._drain_backfill([]) is None

    def test_cursor_persists_only_what_was_drained(self, validator):
        # mirrors ingest(): drain → (journal) → persist drained cursor
        validator._bf_queue.append((8_000_360, [{"commit": "x"}]))
        sources = []
        validator._bf_pending_cursor = validator._drain_backfill(sources)
        validator._backfill_block = validator._bf_pending_cursor
        validator._save_backfill(validator._backfill_block)
        validator.db.commit()
        row = validator.db.execute(
            "SELECT value FROM vali_state WHERE key='backfill_block'").fetchone()
        assert row == (8_000_360,)
