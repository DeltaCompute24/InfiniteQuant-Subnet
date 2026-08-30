"""The points qualify gate.

Two properties carry the design and both were WRONG in the first implementation,
in ways no unit test would have caught -- only a simulation against the gate it
has to replace:

  * Var[X_i] = p^2, not p^2 * p_res. The p_res factor is right for a sum over all
    submissions (a wash contributes zero); this function is handed decisive rows
    only. Shipping it understated the denominator by sqrt(0.86) and made the gate
    uniformly more permissive than Wilson >= 0.55 -- 141 disagreements in 2400
    trials, in the SAME direction in all 12 cells. After the fix: 0 of 2400.

  * eps must be strictly positive, or a coin-flipper passes at ~10% for ANY n.
"""
import math

import pytest

from sn89_signals import config, hf, scoring

BOARD_TP, BOARD_HZ = 19.0, 1800
SG = scoring.sigma_from_board(BOARD_TP, BOARD_HZ)


def sf(pair, t0):
    return SG


def rows(n_win, n_loss, tp=BOARD_TP, hz=BOARD_HZ):
    r = [(1000.0 + i, True, False, None, tp, hz, "BTCUSD") for i in range(n_win)]
    r += [(5000.0 + i, False, False, None, tp, hz, "BTCUSD") for i in range(n_loss)]
    return r


class TestTheAlgebra:
    """T must be computable by hand. Pin it, so a refactor cannot drift it."""

    def test_t_matches_the_closed_form(self):
        k, n = 30, 50
        out = scoring.points_test(rows(k, n - k), sf)
        p = scoring.points_for(BOARD_TP, BOARD_HZ, SG)
        expect = ((p * (2 * k - n) - config.POINTS_QUALIFY_EPS * n * p)
                  / math.sqrt(n * p * p))
        assert out["t"] == pytest.approx(expect, rel=1e-12)
        assert out["n"] == n
        assert out["staked"] == pytest.approx(n * p, rel=1e-12)

    def test_variance_has_no_p_res_factor(self):
        """The regression. With p_res folded in, T is inflated by 1/sqrt(p_res)."""
        out = scoring.points_test(rows(30, 20), sf)
        p = scoring.points_for(BOARD_TP, BOARD_HZ, SG)
        z = BOARD_TP / (SG * math.sqrt(BOARD_HZ))
        wrong = ((p * 10 - config.POINTS_QUALIFY_EPS * 50 * p)
                 / math.sqrt(50 * p * p * scoring.resolve_probability(z)))
        assert out["t"] != pytest.approx(wrong, rel=1e-9)
        assert out["t"] < wrong, "the p_res factor makes the gate too permissive"

    def test_a_perfect_coin_is_negative(self):
        # 2k = n, so the numerator is just -eps*staked: strictly below zero.
        assert scoring.points_test(rows(50, 50), sf)["t"] < 0


class TestEpsIsLoadBearing:
    def test_eps_ships_positive(self):
        assert config.POINTS_QUALIFY_EPS > 0, (
            "at eps=0 a coin-flipper passes ~10% of the time at EVERY n")

    def test_raising_eps_lowers_t(self, monkeypatch):
        r = rows(60, 40)
        a = scoring.points_test(r, sf)["t"]
        monkeypatch.setattr(config, "POINTS_QUALIFY_EPS", 0.20)
        assert scoring.points_test(r, sf)["t"] < a


class TestFloorsBind:
    def test_a_short_hot_streak_does_not_qualify(self):
        # 100% wins, but under the resolved-call floor.
        out = scoring.points_test(rows(config.POINTS_QUALIFY_MIN_RESOLVED - 1, 0), sf)
        assert out["t"] > config.POINTS_QUALIFY_Z
        assert out["qualified"] is False

    def test_enough_calls_and_a_real_edge_qualifies(self):
        out = scoring.points_test(rows(45, 15), sf)
        assert out["n"] >= config.POINTS_QUALIFY_MIN_RESOLVED
        assert out["staked"] >= config.POINTS_QUALIFY_MIN_STAKED
        assert out["qualified"] is True

    def test_staked_floor_bites_on_cheap_shapes(self, monkeypatch):
        # Raise the staked floor above anything this record can reach: the miner
        # has the edge and the sample, and still must not pass.
        monkeypatch.setattr(config, "POINTS_QUALIFY_MIN_STAKED", 1e9)
        assert scoring.points_test(rows(45, 15), sf)["qualified"] is False


class TestUnpriceableContributesNothing:
    def test_no_band_is_skipped_not_defaulted(self):
        r = [(1000.0, True, False, None, None, None, "BTCUSD")] * 40
        assert scoring.points_test(r, sf)["n"] == 0

    def test_no_sigma_is_skipped(self):
        assert scoring.points_test(rows(40, 10), lambda p, t: 0.0)["n"] == 0

    def test_empty_is_zero_not_an_error(self):
        assert scoring.points_test([], sf) == {
            "t": 0.0, "staked": 0.0, "n": 0, "qualified": False}


class TestWhereDifficultyDoesAndDoesNotShOW_UP:
    """The subtlety the spec understates.

    "A hit rate stops meaning anything once the miner picks the difficulty" is
    true of RANKING and false of QUALIFYING. Under the null, P(win | resolved) is
    exactly 0.5 at EVERY z -- symmetric barriers, driftless -- so 60% at z=0.3 is
    exactly as surprising as 60% at z=1.4. T is therefore scale-free in p when
    difficulty is uniform, and that is correct rather than a defect.

    Difficulty is paid in the TALLY (points per win), which is the right place
    for it. In the GATE it appears only through the staked floor, which makes a
    cheap-shape miner produce more calls before they are licensed.
    """

    def test_t_is_scale_free_in_p(self):
        easy = scoring.points_test(rows(36, 24, tp=4.4), sf)
        hard = scoring.points_test(rows(36, 24, tp=35.4), sf)
        assert hard["t"] == pytest.approx(easy["t"], rel=1e-9)

    def test_difficulty_shows_up_in_what_is_staked(self):
        easy = scoring.points_test(rows(36, 24, tp=4.4), sf)
        hard = scoring.points_test(rows(36, 24, tp=35.4), sf)
        assert easy["staked"] < hard["staked"]

    def test_the_staked_floor_demands_more_calls_from_a_cheap_shape(self):
        """The floor's real job, stated as the thing a miner would feel."""
        def calls_needed(tp):
            n = config.POINTS_QUALIFY_MIN_RESOLVED
            while n < 500:
                w = int(round(n * 0.75))
                if scoring.points_test(rows(w, n - w, tp=tp), sf)["qualified"]:
                    return n
                n += 1
            return None
        cheap = calls_needed(4.4)          # z ~ 0.3, ~1.00 pts a win
        bold = calls_needed(35.4)          # z ~ 1.4, several pts a win
        assert bold == config.POINTS_QUALIFY_MIN_RESOLVED, (
            "a bold miner should be gated by the call floor alone")
        assert cheap > bold, (
            "a cheap-shape miner must show more evidence before being licensed")


class TestRealBoardPairs:
    @pytest.mark.parametrize("pair", list(hf.hf_bands_as_of(2_000_000_000))[:6])
    def test_every_board_pair_stakes_the_same(self, pair):
        tp, sl, hz, cls = hf.hf_bands_as_of(2_000_000_000)[pair]
        sg = scoring.sigma_from_board(tp, hz)
        out = scoring.points_test(
            [(1000.0 + i, True, False, None, tp, hz, pair) for i in range(10)],
            lambda p, t: sg)
        assert out["staked"] == pytest.approx(
            10 * scoring.points_for(BOARD_TP, BOARD_HZ, SG), rel=1e-6)
