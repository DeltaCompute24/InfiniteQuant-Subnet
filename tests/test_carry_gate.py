"""The earning gate on the points path is the points test, as of each call,
over the carried mainnet record plus earlier calls here (Whit 2026-09-09).
"""
import pytest

from sn89_signals import config, scoring

SIG = 1.0
FS = -1e9          # first_seen far in the past: warmup is not what is under test


def sigma_for(pair, t0):
    return SIG


def row(t0, won, tp=20.0, hz=1800, pair="BTCUSD"):
    return (t0, won, False, None, tp, hz, pair)


def strong_prior(n=60, t0=0.0):
    # 60 wins: passes t >= Z, n >= 30, staked >= 40 comfortably
    return [row(t0 + i, True) for i in range(n)]


class TestCarriedMinerEarnsFromCallOne:
    def test_first_beta_call_banks_with_a_qualified_prior(self):
        out = scoring.qualified_calls([row(10_000, True)], FS,
                                      sigma_for=sigma_for, prior=strong_prior())
        assert len(out) == 1 and out[0][1] > 0

    def test_no_prior_and_two_calls_banks_nothing(self):
        out = scoring.qualified_calls([row(10_000, True), row(10_100, False)], FS,
                                      sigma_for=sigma_for)
        assert out == []

    def test_prior_after_the_call_does_not_count(self):
        # prior rows dated AFTER the beta call are not evidence for it
        out = scoring.qualified_calls([row(10_000, True)], FS,
                                      sigma_for=sigma_for,
                                      prior=strong_prior(t0=20_000))
        assert out == []

    def test_a_losing_prior_does_not_qualify(self):
        prior = [row(i, i % 2 == 0) for i in range(60)]      # coin flip
        out = scoring.qualified_calls([row(10_000, True)], FS,
                                      sigma_for=sigma_for, prior=prior)
        assert out == []


class TestGateMatchesTheBadge:
    def test_incremental_verdict_equals_points_test(self):
        rows = [row(i, i % 3 != 0) for i in range(80)]
        acc = scoring._PointsAcc()
        for r in rows:
            acc.add(r[1], scoring._price_row(r, sigma_for))
        assert acc.qualified() == scoring.points_test(rows, sigma_for=sigma_for)["qualified"]

    def test_a_new_miner_qualifies_here_once_their_own_record_passes(self):
        # 60 wins here, no prior: ~1 pt each, so the 40-pt stake floor clears around call 41
        rows = [row(1000 + i, True) for i in range(60)]
        out = scoring.qualified_calls(rows, FS, sigma_for=sigma_for)
        assert 0 < len(out) < 60
        assert out[0][0] >= 1000 + config.POINTS_QUALIFY_MIN_RESOLVED


class TestMainnetUnchanged:
    def test_no_prior_argument_is_the_old_signature(self):
        # callers that never pass prior still work
        out = scoring.qualified_calls([], FS, sigma_for=sigma_for)
        assert out == []
