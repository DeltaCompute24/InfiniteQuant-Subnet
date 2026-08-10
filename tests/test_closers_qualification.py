"""Closers entry requires BASE-or-better qualification AT SUBMISSION TIME.

Enforced at ingest, not only at payout: an unqualified miner used to submit
freely, be graded, and rank publicly on a score that could never be paid
(Ljay, 2026-08-05). The fail-open case matters as much as the gate — an
unreadable standing file must not refuse everybody.

Qualification has TWO sources and a miner may hold only one. LF standing is
`meets_gate` on a roster row; HF is `qualified` on a scoreboard leaderboard row.
Until 2026-08-10 only LF was read, so an HF-qualified miner was refused
`not_qualified` on a competition they were ranked in (@ColdiePips: LF hit rate
29.6% and meets_gate false, HF 149W/111L and qualified true). EVERY test pins
BOTH paths — leaving one unset silently reads the real production file.
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


def _scoreboard(tmp_path, rows):
    p = tmp_path / "hf-scoreboard.json"
    p.write_text(json.dumps({"leaderboard": rows}))
    return str(p)


def _sources(tmp_path, monkeypatch, lf=(), hf=()):
    """Pin both qualification sources. lf/hf are (hotkey, qualified) pairs."""
    monkeypatch.setattr(closers, "STANDING_PATH", _standing(
        tmp_path, [{"hotkey": hk, "meets_gate": q} for hk, q in lf]))
    monkeypatch.setattr(closers, "HF_SCOREBOARD_PATH", _scoreboard(
        tmp_path, [{"bittensor_hotkey": hk, "qualified": q} for hk, q in hf]))


def test_qualified_miner_is_accepted(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, lf=[("hkA", True)])
    closers.check_qualified("hkA")            # must not raise


def test_unqualified_miner_is_refused(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, lf=[("hkA", True), ("hkB", False)])
    with pytest.raises(closers.ClosersRejected) as e:
        closers.check_qualified("hkB")
    assert "not_qualified" in str(e.value)


def test_absent_from_roster_is_refused(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, lf=[("hkA", True)])
    with pytest.raises(closers.ClosersRejected):
        closers.check_qualified("hkZ")


def test_hf_qualified_is_accepted_without_lf(tmp_path, monkeypatch):
    """The 2026-08-10 fix. Thin LF record, qualified on HF -> accepted."""
    _sources(tmp_path, monkeypatch,
             lf=[("hkColdie", False)], hf=[("hkColdie", True)])
    closers.check_qualified("hkColdie")       # must not raise


def test_lf_qualified_is_accepted_without_hf(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch,
             lf=[("hkA", True)], hf=[("hkA", False)])
    closers.check_qualified("hkA")            # must not raise


def test_gate_is_the_union_not_a_relaxation(tmp_path, monkeypatch):
    """Unqualified on BOTH is still refused — reading HF widens the gate to the
    HF-qualified, it does not open it to the field (Ljay: LF false, HF false)."""
    _sources(tmp_path, monkeypatch,
             lf=[("hkA", True), ("hkLjay", False)],
             hf=[("hkColdie", True), ("hkLjay", False)])
    assert closers.qualified_hotkeys() == {"hkA", "hkColdie"}
    with pytest.raises(closers.ClosersRejected):
        closers.check_qualified("hkLjay")


def test_unreadable_standing_does_not_refuse_everyone(tmp_path, monkeypatch):
    """Fail OPEN. A missing file is 'cannot decide', not 'nobody qualifies' —
    refusing the whole field on an unreadable local file would be a far worse
    failure than briefly admitting one unqualified vote."""
    _sources(tmp_path, monkeypatch, hf=[("hkColdie", True)])
    monkeypatch.setattr(closers, "STANDING_PATH", str(tmp_path / "nope.json"))
    assert closers.qualified_hotkeys() is None
    closers.check_qualified("anyone")         # must not raise


def test_unreadable_hf_scoreboard_does_not_refuse_everyone(tmp_path, monkeypatch):
    """A PARTIAL read is unknowable too. Unioning only the half that loaded
    would refuse everyone who qualifies via the other half — i.e. it would
    reintroduce the original bug as an intermittent one."""
    _sources(tmp_path, monkeypatch, lf=[("hkA", True)])
    monkeypatch.setattr(closers, "HF_SCOREBOARD_PATH", str(tmp_path / "nope.json"))
    assert closers.qualified_hotkeys() is None
    closers.check_qualified("hkColdie")       # must not raise
