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
    monkeypatch.setattr(config, "EFFICIENCY_PRIOR_WASH", 0.40)


class TestEfficiencyMultiplier:
    def test_off_before_the_as_of_gate(self, monkeypatch):
        monkeypatch.setattr(config, "EFFICIENCY_FROM", 0)
        assert scoring.efficiency_multiplier(_graded(40, 10), T0) == 1.0

    def test_a_win_before_the_gate_is_never_revalued(self, monkeypatch):
        monkeypatch.setattr(config, "EFFICIENCY_FROM", int(T0 + DAY))
        assert scoring.efficiency_multiplier(_graded(40, 10), T0) == 1.0

    def test_zero_washes_approaches_the_top_of_the_scale(self):
        """Asymptotic, not exact: the prior always retains some weight, so a clean
        record converges on 1.0 from below as evidence accumulates."""
        few = scoring.efficiency_multiplier(_graded(0, 40), T0)
        many = scoring.efficiency_multiplier(_graded(0, 400), T0)
        assert few < many < 1.0
        assert many > 0.95

    def test_nothing_ever_exceeds_1x(self):
        """Pure penalty: efficiency is never a bonus on top of the tier."""
        for w, d in ((0, 400), (0, 40), (1, 400), (5, 200)):
            assert scoring.efficiency_multiplier(_graded(w, d), T0) <= 1.0

    def test_more_washes_always_costs_more(self):
        prev = 1.1
        for w in (0, 10, 25, 40, 55, 70):
            m = scoring.efficiency_multiplier(_graded(w, 100 - w), T0)
            assert m < prev, f"not monotone at {w}% wash"
            prev = m
        assert prev > config.EFFICIENCY_MIN, "floor must not bind across the field"

    def test_a_fresh_hotkey_starts_at_the_prior_not_at_perfect(self):
        """Otherwise churning hotkeys would launder a wash record."""
        fresh = scoring.efficiency_multiplier([], T0)
        expected = 1.0 - config.EFFICIENCY_SLOPE * config.EFFICIENCY_PRIOR_WASH
        assert fresh == pytest.approx(expected, abs=1e-9)
        assert fresh < scoring.efficiency_multiplier(_graded(0, 400), T0)

    def test_floored(self):
        assert scoring.efficiency_multiplier(_graded(400, 0), T0) == config.EFFICIENCY_MIN

    def test_shrinkage_pulls_thin_samples_toward_the_prior(self):
        """A 70%-wash miner with 20 calls must be judged more gently than one with
        200 — the thin sample is mostly prior, not evidence."""
        small = scoring.efficiency_multiplier(_graded(14, 6), T0)
        large = scoring.efficiency_multiplier(_graded(140, 60), T0)
        assert large > config.EFFICIENCY_MIN
        assert small > large
        assert config.EFFICIENCY_MIN < large < 1.0

    def test_the_floor_binds_only_for_the_worst(self):
        """A typical miner must not already be pinned to the floor, or the
        multiplier stops discriminating across the field."""
        assert scoring.efficiency_multiplier(_graded(46, 54), T0) > config.EFFICIENCY_MIN

    def test_only_the_reputation_window_counts(self):
        """Washes age out on the same 60-day clock as the W/L history."""
        recent = _graded(5, 45, span=DAY)
        old = [(T0 - config.HIT_RATE_WINDOW_S - DAY - i, True) for i in range(100)]
        assert (scoring.efficiency_multiplier(old + recent, T0)
                == scoring.efficiency_multiplier(recent, T0))

    def test_as_of_not_retroactive(self):
        """Judged at the WIN's t0, so washes AFTER it cannot devalue a banked win."""
        early = _graded(2, 40, t_end=T0 - 10 * DAY, span=20 * DAY)
        later = [(T0 - i, True) for i in range(60)]
        assert (scoring.efficiency_multiplier(early + later, T0 - 10 * DAY)
                == scoring.efficiency_multiplier(early, T0 - 10 * DAY))


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
    eff_span = 1.0 / config.EFFICIENCY_MIN
    assert eff_span < tier_span
