"""Emission-cap (Mantis-style burn), qualified-win decay model, no-cliff, and
probation floor — the pieces added on top of the base track-record scoring.

Pure: imports only config + scoring.

    pytest tests/test_emission_cap.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, scoring   # noqa: E402

DAY = 86_400.0


@pytest.fixture(autouse=True)
def _confidence_on(monkeypatch):
    monkeypatch.setattr(config, "CONFIDENCE_SCORING", True)


def _ms(uid, first_seen, rw, rd, qwins):
    return scoring.MinerState(hotkey=f"hk{uid}", uid=uid, first_seen_unix=first_seen,
                              rep_wins=rw, rep_decisive=rd, trailing_wins=0, qwins=qwins)


# ── the 20% burn cap ─────────────────────────────────────────────────────────
class TestEmissionCap:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def test_field_capped_and_burn_absorbs_the_rest(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 0.20),))
        w = scoring.compute_weights(
            [_ms(1, self.OLD, 30, 40, [(self.NOW, 2.0)])], self.NOW)
        field = sum(v for u, v in w.items() if u != config.BURN_UID)
        assert field == pytest.approx(0.20, abs=1e-6)
        assert w[config.BURN_UID] == pytest.approx(0.80, abs=1e-6)

    def test_relative_shares_preserved_under_cap(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 0.20),))
        w = scoring.compute_weights([
            _ms(1, self.OLD, 30, 40, [(self.NOW, 2.0)] * 6),
            _ms(2, self.OLD, 30, 40, [(self.NOW, 2.0)] * 3),
        ], self.NOW)
        assert w[1] / w[2] == pytest.approx(2.0)          # 6:3, untouched by the cap

    def test_cap_disabled_is_full_passthrough(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))
        w = scoring.compute_weights(
            [_ms(1, self.OLD, 30, 40, [(self.NOW, 2.0)])], self.NOW)
        assert w[1] > 0.99

    def test_cap_never_inflates_a_weak_field(self, monkeypatch):
        # only immune dust, no earners → the cap must NOT scale dust UP to 20%.
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 0.20),))
        w = scoring.compute_weights(
            [_ms(1, self.NOW - DAY, 0, 0, [])], self.NOW)   # immune, no qwins
        field = sum(v for u, v in w.items() if u != config.BURN_UID)
        assert field < 0.01                                 # stays dust, not 0.20


# ── qualified-win extraction (point-in-time gate) ────────────────────────────
class TestQualifiedWins:
    NOW = 300 * DAY

    def test_losses_and_pre_warmup_wins_excluded(self):
        fs = self.NOW - 100 * DAY
        we = fs + config.IMMUNITY_S
        dec = [(we + i * DAY, True, False) for i in range(12)]   # 12 post-warmup wins
        dec.append((we - DAY, True, False))                     # pre-warmup win
        dec.append((we + 4 * DAY, False, False))                # a loss
        qw = scoring.qualified_wins(dec, fs)
        assert all(t0 >= we for t0, _ in qw)          # never a pre-warmup win
        assert all(wt >= 1.0 for _, wt in qw)         # every weight ≥ base tier
        # a loss timestamp is never emitted
        assert (we + 4 * DAY) not in [t0 for t0, _ in qw]

    def test_win_while_unqualified_is_not_counted(self):
        fs = self.NOW - 100 * DAY
        we = fs + config.IMMUNITY_S
        dec = [(we + i * DAY, True, False) for i in range(10)]   # 10 wins → qualified
        dec += [(we + (11 + i) * DAY, False, False) for i in range(15)]  # 15 losses → unqualifies
        dec.append((we + 30 * DAY, True, False))                # a win while unqualified
        qw = [t0 for t0, _ in scoring.qualified_wins(dec, fs)]
        assert (we + 9 * DAY) in qw            # the 10th win (qualified) counts
        assert (we + 30 * DAY) not in qw       # the post-collapse win does NOT


# ── no cliff: a loss/unqualification never zeroes banked earnings ────────────
class TestNoCliff:
    NOW = 200 * DAY

    def test_fallen_miner_keeps_earning_off_banked_qwins(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))
        assert not scoring._qualifies(5, 12)          # 42% now — below the gate
        fallen = _ms(1, self.NOW - 60 * DAY, 5, 12,   # currently UNqualified …
                     [(self.NOW - 1 * DAY, 2.0), (self.NOW - 2 * DAY, 2.0)])  # … banked WOLF wins
        active = _ms(2, self.NOW - 60 * DAY, 9, 12, [(self.NOW - DAY, 1.0)])
        w = scoring.compute_weights([fallen, active], self.NOW)
        assert w.get(1, 0.0) > 0.0                    # NO cliff — still earning
        assert w[1] > w[2]                            # 2 recent WOLF wins > 1 base win

    def test_more_recent_qwins_earn_more(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))
        recent = _ms(1, self.NOW - 60 * DAY, 9, 12, [(self.NOW - 1 * DAY, 1.0)])
        old = _ms(2, self.NOW - 60 * DAY, 9, 12,
                  [(self.NOW - 0.8 * config.EMISSION_DECAY_S, 1.0)])   # nearly decayed out
        w = scoring.compute_weights([recent, old], self.NOW)
        assert w[1] > w[2]                            # linear decay favours recency

    def test_qwin_fully_decayed_out_earns_nothing_from_pool(self, monkeypatch):
        # a single qualified win older than EMISSION_DECAY_S contributes 0 to the pool
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))
        monkeypatch.setattr(config, "PROBATION_S", 0)     # isolate the pool math
        w = scoring.compute_weights(
            [_ms(1, self.NOW - 90 * DAY, 5, 12,
                  [(self.NOW - config.EMISSION_DECAY_S - DAY, 2.0)])], self.NOW)
        assert w == {config.BURN_UID: 1.0}


# ── probation dust floor ─────────────────────────────────────────────────────
class TestProbation:
    NOW = 200 * DAY

    @pytest.fixture(autouse=True)
    def _cap_off(self, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))
        monkeypatch.setattr(config, "PROBATION_S", int(30 * DAY))

    def test_warmup_qualified_no_qwin_gets_dust(self):
        # qualified in warmup, no qualified win yet, just past warmup → dust floor
        s = _ms(1, self.NOW - config.IMMUNITY_S - 1, 30, 40, [])
        w = scoring.compute_weights([s], self.NOW)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_decayed_out_within_probation_gets_dust(self):
        # last qualified win (decay window + 5d) ago → tally 0, but only 5d past the
        # earning-window close → inside the 30d probation runway → dust
        s = _ms(1, self.NOW - 90 * DAY, 5, 12,
                [(self.NOW - config.EMISSION_DECAY_S - 5 * DAY, 2.0)])
        w = scoring.compute_weights([s], self.NOW)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_probation_expires_after_30d(self):
        # last qualified win (decay window + 40d) ago → 40d past the earning-window
        # close → runway expired → nothing (burns)
        s = _ms(1, self.NOW - 120 * DAY, 5, 12,
                [(self.NOW - config.EMISSION_DECAY_S - 40 * DAY, 2.0)])
        w = scoring.compute_weights([s], self.NOW)
        assert 1 not in w
        assert w[config.BURN_UID] == pytest.approx(1.0)

    def test_never_qualified_gets_no_probation(self):
        # no qualified win ever and not currently qualified → no floor
        s = _ms(1, self.NOW - 90 * DAY, 3, 12, [])   # 25% — never cleared the gate
        w = scoring.compute_weights([s], self.NOW)
        assert 1 not in w
