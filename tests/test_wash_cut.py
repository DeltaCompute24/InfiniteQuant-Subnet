"""Per-wash emission cut (Whit, 2026-09-09): a wash that resolved inside the
last 24h cuts the miner's share of the pool for the rest of that window.

Per event, non-stacking, never dust, testnet-armed only. Runs on top of the
excess-wash debt in test_wash_penalty.py, which is unchanged.
"""
import pytest

from sn89_signals import config, hf, scoring

NOW = 1_800_000_000.0
DAY = 86_400.0


def _state(hk, uid, wash_resolved=(), pts=5.0):
    return scoring.MinerState(
        hotkey=hk, uid=uid, first_seen_unix=NOW - 30 * DAY,
        rep_wins=20, rep_decisive=30, trailing_wins=20,
        qcalls=[(NOW - 3600.0 * i, pts) for i in range(1, 6)],
        wash_resolved=list(wash_resolved))


def _weights(states, monkeypatch, armed=True, keep=None):
    monkeypatch.setattr(config, "HF_WASH_CUT_FROM", int(NOW - DAY) if armed else 0)
    if keep is not None:
        monkeypatch.setattr(config, "HF_WASH_CUT_KEEP", keep)
    return scoring.compute_weights(states, NOW, burn_uid=0, use_points=True)


class TestTheCut:
    def test_disarmed_is_byte_identical(self, monkeypatch):
        a = _weights([_state("A", 1, [NOW - 3600]), _state("B", 2)], monkeypatch, armed=False)
        b = _weights([_state("A", 1), _state("B", 2)], monkeypatch, armed=False)
        assert a == b

    def test_a_wash_in_the_window_cuts_to_keep(self, monkeypatch):
        w = _weights([_state("A", 1, [NOW - 3600]), _state("B", 2)], monkeypatch)
        assert w[1] / w[2] == pytest.approx(config.wash_cut_keep(), rel=1e-9)

    def test_a_wash_outside_the_window_does_not(self, monkeypatch):
        w = _weights([_state("A", 1, [NOW - config.HF_WASH_CUT_S - 1]), _state("B", 2)],
                     monkeypatch)
        assert w[1] == pytest.approx(w[2], rel=1e-9)

    def test_two_washes_cut_once(self, monkeypatch):
        one = _weights([_state("A", 1, [NOW - 3600]), _state("B", 2)], monkeypatch)
        two = _weights([_state("A", 1, [NOW - 3600, NOW - 7200]), _state("B", 2)], monkeypatch)
        assert one[1] == pytest.approx(two[1], rel=1e-9)

    def test_a_wash_not_yet_resolved_is_ignored(self, monkeypatch):
        w = _weights([_state("A", 1, [NOW + 60]), _state("B", 2)], monkeypatch)
        assert w[1] == pytest.approx(w[2], rel=1e-9)

    def test_it_is_a_cut_and_never_dust(self, monkeypatch):
        w = _weights([_state("A", 1, [NOW - 3600]), _state("B", 2)], monkeypatch, keep=0.0)
        assert w[1] > 0.0
        assert w[1] / w[2] == pytest.approx(config.HF_WASH_CUT_KEEP_FLOOR, rel=1e-9)

    def test_an_underwater_miner_is_not_cut_further(self, monkeypatch):
        # negative tally: no share to cut; the excess debt is what reaches them
        s = _state("A", 1, [NOW - 3600], pts=-5.0)
        assert not scoring.wash_cut_active([], NOW)
        w = _weights([s, _state("B", 2)], monkeypatch)
        assert w.get(1, 0.0) == w.get(1, 0.0)   # no exception, no share


class TestResolvedTimes:
    def test_only_resolved_washes_are_returned(self):
        rows = [(NOW - 7200, True, 10.0, 1800, "BTCUSD"),     # resolved at NOW-5400
                (NOW - 600, True, 10.0, 1800, "BTCUSD"),      # still open
                (NOW - 7200, False, 10.0, 1800, "BTCUSD")]    # not a wash
        assert hf._wash_resolved(rows, NOW) == [NOW - 5400]

    def test_active_window_is_half_open(self):
        assert scoring.wash_cut_active([NOW], NOW)
        assert not scoring.wash_cut_active([NOW - config.HF_WASH_CUT_S], NOW)
