"""Efficiency (wash) multiplier — scoring.efficiency_multiplier + qualified_wins."""
import sys

import pytest

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, scoring

DAY = 86400.0
T0 = 1790000000.0


def _graded(n_wash, n_dec, t_end=T0, span=30 * DAY):
    """n_wash washes + n_dec decisive, spread evenly ending at t_end."""
    n = n_wash + n_dec
    return [(t_end - span + i * (span / max(n, 1)), i < n_wash) for i in range(n)]


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(config, "EFFICIENCY_FROM", int(T0 - 365 * DAY))
    monkeypatch.setattr(config, "EFFICIENCY_BASELINE_WASH", 0.46)


class TestEfficiencyMultiplier:
    def test_off_before_the_as_of_gate(self, monkeypatch):
        monkeypatch.setattr(config, "EFFICIENCY_FROM", 0)
        assert scoring.efficiency_multiplier(_graded(40, 10), T0) == 1.0

    def test_a_win_before_the_gate_is_never_revalued(self, monkeypatch):
        monkeypatch.setattr(config, "EFFICIENCY_FROM", int(T0 + DAY))
        assert scoring.efficiency_multiplier(_graded(40, 10), T0) == 1.0

    def test_thin_sample_is_neutral(self):
        # below EFFICIENCY_MIN_N we cannot separate skill from luck, so do not tax it
        g = _graded(config.EFFICIENCY_MIN_N - 1, 0)
        assert scoring.efficiency_multiplier(g, T0) == 1.0

    def test_baseline_wash_is_neutral(self):
        m = scoring.efficiency_multiplier(_graded(46, 54), T0)
        assert m == pytest.approx(1.0, abs=0.01)

    def test_excess_wash_is_penalised(self):
        assert scoring.efficiency_multiplier(_graded(80, 20), T0) < 0.95

    def test_low_wash_is_rewarded(self):
        assert scoring.efficiency_multiplier(_graded(20, 80), T0) > 1.0

    def test_clamped_both_ways(self):
        assert scoring.efficiency_multiplier(_graded(200, 0), T0) == config.EFFICIENCY_MIN
        assert scoring.efficiency_multiplier(_graded(0, 200), T0) == config.EFFICIENCY_MAX

    def test_shrinkage_makes_small_samples_milder(self):
        """Same 55% wash rate: 20 calls must be penalised LESS than 200.
        Chosen to sit off the EFFICIENCY_MIN clamp, which both saturate at 100%."""
        small = scoring.efficiency_multiplier(_graded(11, 9), T0)
        large = scoring.efficiency_multiplier(_graded(110, 90), T0)
        assert small > large
        assert config.EFFICIENCY_MIN < large < 1.0

    def test_the_floor_binds_only_for_the_worst(self):
        """~1 SD of excess wash (9pp) must NOT already saturate the penalty --
        otherwise the multiplier stops discriminating across most of the field."""
        one_sd = scoring.efficiency_multiplier(_graded(55, 45), T0)   # +9pp excess
        assert one_sd > config.EFFICIENCY_MIN

    def test_only_the_reputation_window_counts(self):
        """Ancient washes must not follow a miner forever."""
        old = [(T0 - config.HIT_RATE_WINDOW_S - DAY - i, True) for i in range(100)]
        recent = _graded(5, 45, span=DAY)
        assert scoring.efficiency_multiplier(old + recent, T0) > 1.0

    def test_as_of_not_retroactive(self):
        """Judged at the WIN's t0, so later washes cannot devalue a banked win."""
        early = _graded(2, 40, t_end=T0 - 10 * DAY, span=20 * DAY)
        later_washes = [(T0 - i, True) for i in range(60)]
        at_win = scoring.efficiency_multiplier(early + later_washes, T0 - 10 * DAY)
        assert at_win > 1.0


class TestQualifiedWinsIntegration:
    def _dec(self, n_win, n_loss, t_end=T0):
        """Wins and losses INTERLEAVED, so the point-in-time hit rate at each win
        reflects the overall rate. Front-loading the wins would hand the early
        wins a ~100% as-of hit rate and a top-tier stamp."""
        seq = []
        w = l = 0
        while w < n_win or l < n_loss:
            if w * n_loss <= l * n_win and w < n_win:
                seq.append(True); w += 1
            else:
                seq.append(False); l += 1
        n = len(seq)
        return [(t_end - (n - i) * 3600.0, won, False) for i, won in enumerate(seq)]

    def test_efficiency_scales_banked_win_value(self):
        dec = self._dec(30, 10)
        base = scoring.qualified_wins(dec, T0 - 365 * DAY)
        washy = scoring.qualified_wins(
            dec, T0 - 365 * DAY,
            graded=[(t, False) for t, _, _ in dec] + [(t - 1.0, True) for t, _, _ in dec] * 2)
        assert base and washy
        assert sum(w for _, w in washy) < sum(w for _, w in base)

    def test_applied_outside_the_1x_floor(self):
        """The floor must not clamp the penalty away for the ~80% of the field
        sitting at exactly 1.00x."""
        # 55% over 200 clears the Wilson qualify gate while the K=12 shrunk rate
        # (116/212 = 0.547) stays under the 0.55 SHARP anchor, so tier == 1.00x.
        dec = self._dec(110, 90)
        clean = scoring.qualified_wins(dec, T0 - 365 * DAY)
        assert clean, "fixture must produce qualified wins"
        assert max(w for _, w in clean) == 1.0, "fixture must sit at the 1.00x floor"
        g = [(t, False) for t, _, _ in dec] + [(t - 1.0, True) for t, _, _ in dec] * 3
        washy = scoring.qualified_wins(dec, T0 - 365 * DAY, graded=g)
        assert min(w for _, w in washy) < 1.0

    def test_none_graded_is_backwards_compatible(self):
        dec = self._dec(30, 10)
        assert (scoring.qualified_wins(dec, T0 - 365 * DAY)
                == scoring.qualified_wins(dec, T0 - 365 * DAY, graded=None))


def test_efficiency_range_stays_inside_the_tier_range():
    """Load-bearing invariant. A wash penalty rewards firing into high volatility,
    where outcomes are microstructure rather than opinion. If efficiency could
    swing value more than the tier does, trashing hit-rate to cut washes would
    become profitable and the contest would invert."""
    tier_span = max(m for _, m in config.WIN_RATE_TIERS) / min(
        m for _, m in config.WIN_RATE_TIERS)
    eff_span = config.EFFICIENCY_MAX / config.EFFICIENCY_MIN
    assert eff_span < tier_span
