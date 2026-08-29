"""Signed points: the properties the whole scheme rests on.

The load-bearing one is TestNoViewEarnsZero. If that ever fails, gamma stops
being a free product dial and becomes a risk parameter, and every argument for
rewarding difficulty goes with it.
"""
import math

import pytest

from sn89_signals import config, hf, scoring

T_LATE = 2_000_000_000            # comfortably after every board cutover


def board():
    return hf.hf_bands_as_of(T_LATE)


class TestArmingIsOffByDefault:
    def test_stamp_is_zero_in_source(self):
        assert config.HF_POINTS_FROM == 0, (
            "points must ship disarmed; a network arms it from the env")

    def test_never_enforced_when_disarmed(self):
        for t in (0, hf.HF_LAUNCH_FROM, T_LATE, 4_000_000_000):
            assert config.points_enforced_as_of(t) is False


class TestNoViewEarnsZero:
    """A miner with no view expects exactly zero, at every shape, for any gamma.

    This is why a loss costs what the win would have paid. It is also why gamma
    can be tuned for product reasons without opening a farming surface: +pts and
    -pts cancel whatever pts() looks like.
    """

    @pytest.mark.parametrize("gamma", [0.5, 1.0, 1.5, 2.0, 5.0, 25.0])
    @pytest.mark.parametrize("z", [0.3, 0.7475, 1.0, 1.5, 2.5])
    def test_expectation_is_zero(self, gamma, z):
        sigma, horizon = 0.5, 1800
        tp = z * sigma * math.sqrt(horizon)
        win = scoring.signed_points("won", tp, horizon, sigma, gamma)
        loss = scoring.signed_points("lost", tp, horizon, sigma, gamma)
        pr = scoring.resolve_probability(z)
        ev = pr * 0.5 * win + pr * 0.5 * loss + (1 - pr) * 0.0
        assert abs(ev) < 1e-12

    def test_a_wash_scores_nothing(self):
        assert scoring.signed_points("washed", 19.0, 1800, 0.6) == 0.0

    def test_a_void_scores_nothing(self):
        assert scoring.signed_points("void", 19.0, 1800, 0.6) == 0.0


class TestBoardIsOnePointOnTheCurve:
    """sigma is derived from the board, so every board pair pays the same."""

    def test_every_board_pair_pays_the_same(self):
        vals = []
        for pair, (tp, sl, hz, cls) in board().items():
            sg = scoring.sigma_from_board(tp, hz)
            vals.append(scoring.points_for(tp, hz, sg))
        assert max(vals) - min(vals) < 1e-9, dict(zip(board(), vals))

    def test_board_resolves_at_the_target(self):
        for pair, (tp, sl, hz, cls) in board().items():
            sg = scoring.sigma_from_board(tp, hz)
            z = tp / (sg * math.sqrt(hz))
            assert abs(scoring.resolve_probability(z)
                       - config.HF_POINTS_TARGET_RESOLVE) < 1e-6, pair


class TestOnlyZMatters:
    """Band and horizon are not independent knobs."""

    def test_double_band_equals_quarter_horizon(self):
        sg = 0.6
        a = scoring.points_for(38.0, 1800, sg)
        b = scoring.points_for(19.0, 450, sg)
        assert abs(a - b) < 1e-9

    def test_wider_pays_more(self):
        sg = 0.6
        assert scoring.points_for(38.0, 1800, sg) > scoring.points_for(19.0, 1800, sg)

    def test_shorter_pays_more(self):
        sg = 0.6
        assert scoring.points_for(19.0, 450, sg) > scoring.points_for(19.0, 1800, sg)


class TestDegenerateInputs:
    """A bad shape scores nothing rather than raising into the weight path."""

    @pytest.mark.parametrize("tp,hz,sg", [(0, 1800, 0.6), (19.0, 0, 0.6),
                                          (19.0, 1800, 0), (-5, 1800, 0.6)])
    def test_returns_zero(self, tp, hz, sg):
        assert scoring.points_for(tp, hz, sg) == 0.0
        assert scoring.signed_points("won", tp, hz, sg) == 0.0


class TestDeterminism:
    """Every validator must compute the identical number from identical inputs."""

    def test_repeatable(self):
        a = [scoring.points_for(19.0, 1800, 0.59907) for _ in range(50)]
        assert len(set(a)) == 1

    def test_resolve_probability_is_monotone(self):
        prev = 1.1
        for i in range(1, 60):
            v = scoring.resolve_probability(i / 20.0)
            assert v <= prev + 1e-15
            prev = v
