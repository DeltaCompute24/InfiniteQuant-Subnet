"""Excess wash: penalise the surprise, never the wash.

The measured constraint behind this whole design: a flat per-wash cost above
~0.10 points -- under a tenth of one board win -- collapses the optimal shape to
the easy corner at every skill level, because a wider band mechanically washes
more and so widening would buy expected punishment. A 24h emission cut is worth
far more than 0.10. So the penalty has to be neutral to width, which means
measuring against the miner's own declaration.
"""
import pytest

from sn89_signals import config, scoring


def calls(n_wash, n_resolve, q):
    """[(t0, was_wash, declared_wash_probability)]"""
    return ([(i, True, q) for i in range(n_wash)]
            + [(1000 + i, False, q) for i in range(n_resolve)])


class TestNeutralToWidth:
    """The property the whole design rests on.

    A miner drawing bold washes more AND predicts washing more. If those move
    together, expected surprise is zero at every band -- so nothing about
    choosing a wide shape buys punishment.
    """

    @pytest.mark.parametrize("q", [0.05, 0.14, 0.30, 0.50, 0.77])
    def test_washing_at_the_declared_rate_is_no_surprise(self, q):
        n = 400
        s = scoring.wash_surprise(calls(int(n * q), n - int(n * q), q))
        assert abs(s) < 0.5, "washing as predicted must not register as excess"

    @pytest.mark.parametrize("q", [0.05, 0.14, 0.30, 0.50])
    def test_no_debt_for_an_honest_miner_at_any_width(self, q):
        n = 400
        s = scoring.wash_surprise(calls(int(n * q), n - int(n * q), q))
        assert scoring.wash_debt(tally=100.0, surprise=s) == 0.0


class TestExcessIsCaught:
    def test_washing_far_above_the_declared_rate_registers(self):
        # declared 14%, actually washed 60%
        s = scoring.wash_surprise(calls(60, 40, 0.14))
        assert s > config.HF_WASH_EXCESS_Z

    def test_washing_below_the_declared_rate_is_never_penalised(self):
        s = scoring.wash_surprise(calls(2, 98, 0.30))
        assert s < 0
        assert scoring.wash_debt(tally=100.0, surprise=s) == 0.0

    def test_nothing_to_judge_is_zero_not_an_error(self):
        assert scoring.wash_surprise([]) == 0.0
        # every shape a near-certainty: no variance, so no verdict
        assert scoring.wash_surprise([(1, False, 0.0), (2, False, 0.0)]) == 0.0


class TestTheDebtLandsOnNonEarnersToo:
    """'No emissions for 24h' costs a non-earner exactly nothing.

    The debt is denominated in points so it reaches both.
    """

    def test_an_earner_pays_about_a_day_of_earnings(self):
        tally = 300.0
        d = scoring.wash_debt(tally, surprise=5.0, standing_pct=0.0)
        expected = tally * (config.HF_WASH_DEBT_HOURS * 3600.0) / config.HF_POINTS_WINDOW_S
        assert d == pytest.approx(expected, rel=1e-9)

    def test_an_underwater_miner_still_pays(self):
        # tally is negative: they earn nothing today, and 'suspend emissions'
        # would cost them nothing at all.
        d = scoring.wash_debt(-50.0, surprise=5.0, standing_pct=0.0)
        assert d > 0.0

    def test_the_debt_scales_with_the_miner_not_a_constant(self):
        small = scoring.wash_debt(10.0, surprise=5.0)
        big = scoring.wash_debt(1000.0, surprise=5.0)
        assert big > small * 50


class TestRanked:
    """A good miner who washes sits above a worse one who washes at the same moment."""

    def test_the_top_of_the_board_pays_nothing(self):
        assert scoring.wash_debt(300.0, surprise=9.0, standing_pct=1.0) == 0.0

    def test_the_bottom_pays_in_full(self):
        full = scoring.wash_debt(300.0, surprise=9.0, standing_pct=0.0)
        assert full > 0

    def test_it_is_continuous_so_nobody_sits_on_a_threshold(self):
        vals = [scoring.wash_debt(300.0, surprise=9.0, standing_pct=p / 10.0)
                for p in range(11)]
        assert vals == sorted(vals, reverse=True)
        for a, b in zip(vals, vals[1:]):
            assert a - b < full_step(vals), "a cliff, not a ramp"


def full_step(vals):
    return (max(vals) - min(vals)) * 0.5 + 1e-9
