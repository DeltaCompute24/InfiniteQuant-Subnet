"""The signed tally: losses count, the cap cannot be gamed, the clamp is at the edge."""
import pytest

from sn89_signals import config, hf, scoring

DAY = 86_400
T0 = 1_700_000_000
BOARD_TP, BOARD_HZ = 19.0, 1800          # BTCUSD-shaped


def row(t, won, tp=BOARD_TP, hz=BOARD_HZ):
    """A decisive row in the canonical layout: index 3 is resolved_unix."""
    return (t, won, False, None, tp, hz)


@pytest.fixture
def always_qualified(monkeypatch):
    monkeypatch.setattr(scoring, "_qualifies", lambda w, d: True)


class TestLossesAreInTheTally:
    def test_a_loss_produces_negative_points(self, always_qualified):
        out = scoring.qualified_calls([row(T0, False)], first_seen_unix=0.0)
        assert len(out) == 1 and out[0][1] < 0

    def test_a_win_produces_positive_points(self, always_qualified):
        out = scoring.qualified_calls([row(T0, True)], first_seen_unix=0.0)
        assert len(out) == 1 and out[0][1] > 0

    def test_win_and_loss_on_one_shape_cancel(self, always_qualified):
        out = scoring.qualified_calls([row(T0, True), row(T0 + 60, False)],
                                      first_seen_unix=0.0)
        assert abs(sum(p for _, p in out)) < 1e-12

    def test_an_unpriceable_row_contributes_nothing(self, always_qualified):
        # No band and no horizon: score nothing rather than guess a board value
        # that may not be the one this call was graded against.
        assert scoring.qualified_calls([(T0, True, False, None)],
                                       first_seen_unix=0.0) == []


class TestTheDailyCapCannotBeGamed:
    def test_keeps_the_first_n_of_a_day_not_the_best(self):
        cap = config.HF_POINTS_DAILY_CAP
        # cap losses first, then a pile of wins in the same UTC day.
        calls = [(T0 + i, -1.0) for i in range(cap)]
        calls += [(T0 + cap + i, +1.0) for i in range(20)]
        t = scoring.decayed_points_tally(calls, T0 + 60)
        assert t < 0, ("the cap kept the best calls of the day instead of the "
                       "first -- a miner could shed losses after the fact")

    def test_a_later_day_gets_its_own_allowance(self):
        cap = config.HF_POINTS_DAILY_CAP
        d1 = [(T0 + i, +1.0) for i in range(cap + 5)]
        d2 = [(T0 + DAY + i, +1.0) for i in range(cap + 5)]
        one = scoring.decayed_points_tally(d1, T0 + DAY + 100)
        both = scoring.decayed_points_tally(d1 + d2, T0 + DAY + 100)
        assert both > one

    def test_grinding_past_the_cap_adds_nothing(self):
        cap = config.HF_POINTS_DAILY_CAP
        few = [(T0 + i, +1.0) for i in range(cap)]
        many = [(T0 + i, +1.0) for i in range(cap * 4)]
        now = T0 + 60
        assert abs(scoring.decayed_points_tally(few, now)
                   - scoring.decayed_points_tally(many, now)) < 1e-12


class TestWindowAndDecay:
    def test_a_call_older_than_the_window_is_worth_nothing(self):
        old = [(T0, +5.0)]
        assert scoring.decayed_points_tally(old, T0 + config.HF_POINTS_WINDOW_S + 1) == 0.0

    def test_value_decays_toward_the_window_edge(self):
        fresh = scoring.decayed_points_tally([(T0, +5.0)], T0 + 60)
        stale = scoring.decayed_points_tally(
            [(T0, +5.0)], T0 + int(config.HF_POINTS_WINDOW_S * 0.9))
        assert fresh > stale > 0

    def test_the_window_is_a_month(self):
        assert config.HF_POINTS_WINDOW_S == 30 * DAY


class TestTheTallyItselfIsNotClamped:
    """The clamp belongs in compute_weights, not here.

    Every other caller -- the referrer score, any reporting surface -- needs to
    see how far underwater a miner is. A self-clamping tally hides that.
    """

    def test_a_losing_miner_reads_negative(self):
        assert scoring.decayed_points_tally([(T0, -3.0)], T0 + 60) < 0


class TestClampAtTheWeightEdge:
    def test_underwater_earns_nothing_but_is_not_eliminated(self, monkeypatch):
        monkeypatch.setattr(config, "HF_POINTS_FROM", 1)
        now = T0 + 120
        losing = scoring.MinerState(hotkey="hkA", uid=1, first_seen_unix=0.0,
                                    rep_wins=1, rep_decisive=10, trailing_wins=1,
                                    qcalls=[(T0, -9.0)])
        winning = scoring.MinerState(hotkey="hkB", uid=2, first_seen_unix=0.0,
                                     rep_wins=9, rep_decisive=10, trailing_wins=9,
                                     qcalls=[(T0, +9.0)])
        w = scoring.compute_weights([losing, winning], now)
        assert w.get(1, 0.0) == 0.0, "an underwater miner must not earn"
        assert w.get(2, 0.0) > 0.0
        assert all(v >= 0.0 for v in w.values()), "no weight may be negative"
