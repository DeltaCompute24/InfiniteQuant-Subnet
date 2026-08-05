"""Closers entry requires BASE-or-better qualification AT SUBMISSION TIME.

Enforced at ingest, not only at payout: an unqualified miner used to submit
freely, be graded, and rank publicly on a score that could never be paid
(Ljay, 2026-08-05). The fail-open case matters as much as the gate — an
unreadable standing file must not refuse everybody.
"""
import json

import pytest

from sn89_signals import closers


@pytest.fixture(autouse=True)
def _clear_cache():
    closers._qual_cache.update(at=0.0, hks=None)
    yield
    closers._qual_cache.update(at=0.0, hks=None)


def _standing(tmp_path, rows):
    p = tmp_path / "standing.json"
    p.write_text(json.dumps({"roster": rows}))
    return str(p)


def test_qualified_miner_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(closers, "STANDING_PATH",
                        _standing(tmp_path, [{"hotkey": "hkA", "meets_gate": True}]))
    closers.check_qualified("hkA")            # must not raise


def test_unqualified_miner_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(closers, "STANDING_PATH", _standing(
        tmp_path, [{"hotkey": "hkA", "meets_gate": True},
                   {"hotkey": "hkB", "meets_gate": False}]))
    with pytest.raises(closers.ClosersRejected) as e:
        closers.check_qualified("hkB")
    assert "not_qualified" in str(e.value)


def test_absent_from_roster_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(closers, "STANDING_PATH",
                        _standing(tmp_path, [{"hotkey": "hkA", "meets_gate": True}]))
    with pytest.raises(closers.ClosersRejected):
        closers.check_qualified("hkZ")


def test_unreadable_standing_does_not_refuse_everyone(tmp_path, monkeypatch):
    """Fail OPEN. A missing file is 'cannot decide', not 'nobody qualifies' —
    refusing the whole field on an unreadable local file would be a far worse
    failure than briefly admitting one unqualified vote."""
    monkeypatch.setattr(closers, "STANDING_PATH", str(tmp_path / "nope.json"))
    assert closers.qualified_hotkeys() is None
    closers.check_qualified("anyone")         # must not raise
