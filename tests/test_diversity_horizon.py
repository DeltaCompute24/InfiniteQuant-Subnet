"""The diversity floor scales with the declared horizon.

Under a fixed board this was unnecessary: everyone traded the same clock, so the
horizon was a constant folded into the breadth ladder. Custom sizing makes it a
choice, and a long-only book proves less the longer the window -- drift capture
scales with sqrt(H), measured at 50.4% for a long-only bot at 30 minutes against
52-58% at 24 hours. Without this a miner escapes the gate by lengthening the
clock instead of by trading both sides.
"""
import pytest

from sn89_signals import hf

REF = hf.HF_DIVERSITY_HORIZON_REF_S          # 1800s -- the board's short clock


class TestTheFloorIsReachable:
    """share = sum(min(long, short)) / n, so it can never exceed 0.5.

    A floor above that is not a strict gate; it is a gate that has stopped
    measuring anything, and it fails a perfectly balanced book with a number it
    had no way to beat. Scaling 20% at 2 pairs by 3x produces exactly that.
    """

    @pytest.mark.parametrize("pairs", [1, 2, 3, 4, 6, 8, 20])
    @pytest.mark.parametrize("hz", [1800, 7200, 28800, 86400, 172800])
    def test_never_demands_more_than_a_balanced_book_can_give(self, pairs, hz):
        assert hf.hf_diversity_floor(pairs, hz) < 0.5

    def test_the_ceiling_binds_where_it_must(self):
        # 2 pairs at 20% scaled 3x would be 60% without the clamp.
        assert hf.hf_diversity_floor(2, 172800) == hf.HF_DIVERSITY_FLOOR_CEIL


class TestBoardHorizonIsUnscaled:
    """Nothing about the fixed-board era changes."""

    @pytest.mark.parametrize("pairs", [1, 2, 3, 5, 7, 12])
    def test_reference_horizon_equals_the_bare_ladder(self, pairs):
        assert hf.hf_diversity_floor(pairs, REF) == hf.hf_diversity_floor(pairs)

    @pytest.mark.parametrize("pairs", [1, 2, 5, 9])
    def test_a_legacy_row_with_no_horizon_is_unscaled(self, pairs):
        assert hf.hf_diversity_floor(pairs, None) == hf.hf_diversity_floor(pairs)
        assert hf.hf_diversity_floor(pairs, 0) == hf.hf_diversity_floor(pairs)

    def test_shorter_than_the_reference_is_not_discounted(self):
        # A miner on a 5-minute clock does not get an EASIER floor than the
        # board: the scaling only ever tightens.
        assert hf.hf_diversity_floor(4, 300) == hf.hf_diversity_floor(4)


class TestLongerHorizonsDemandMore:
    def test_monotone_in_horizon(self):
        prev = 0.0
        for hz in (1800, 3600, 7200, 14400, 28800):
            f = hf.hf_diversity_floor(6, hz)
            assert f >= prev
            prev = f

    def test_two_hours_is_about_double_the_board(self):
        # sqrt(7200/1800) = 2
        assert hf.hf_diversity_floor(6, 7200) == pytest.approx(
            hf.hf_diversity_floor(6) * 2.0, rel=1e-9)

    def test_breadth_still_relaxes_it(self):
        assert hf.hf_diversity_floor(8, 28800) < hf.hf_diversity_floor(2, 28800)


class TestTheVerdictCarriesTheHorizon:
    def _subs(self, n, hz, direction="LONG"):
        # (t0_ms, pair, direction, horizon_s) -- element 3 is APPENDED, because
        # hf_diversity indexes s[0..2] positionally and so does every other reader.
        return [(1_790_000_000_000 + i * 1000, "BTCUSD", direction, hz)
                for i in range(n)]

    def test_mean_horizon_is_reported(self):
        v = hf.hf_diversity(self._subs(60, 7200), 1_790_000_100.0)
        assert v["mean_horizon_s"] == 7200

    def test_a_one_sided_long_horizon_book_fails_where_a_short_one_passes(self):
        now = 1_790_000_100.0
        # 90% long / 10% short on one pair: 10% minority share.
        subs_short = (self._subs(54, 1800, "LONG") + self._subs(6, 1800, "SHORT"))
        subs_long = (self._subs(54, 28800, "LONG") + self._subs(6, 28800, "SHORT"))
        vs = hf.hf_diversity(subs_short, now)
        vl = hf.hf_diversity(subs_long, now)
        assert vl["floor"] > vs["floor"], "a longer clock must demand more"

    def test_legacy_three_tuples_still_work(self):
        # Records written before the horizon column exists must not raise and
        # must not be scaled.
        subs = [(1_790_000_000_000 + i * 1000, "BTCUSD", "LONG") for i in range(60)]
        v = hf.hf_diversity(subs, 1_790_000_100.0)
        assert v["mean_horizon_s"] is None
        assert v["floor"] == hf.hf_diversity_floor(v["pairs"])
