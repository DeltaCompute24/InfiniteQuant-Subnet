"""HF ends 2026-11-05 00:00 UTC (Whit, 2026-10-29). Its share moves to `agent` and `reserve`,
which burn until agent arms; LF and Markets are untouched; new HF calls are refused."""
import pytest

from sn89_signals import competitions, config, hf, scoring

END = 1793836800


def test_shares_before_end_are_todays():
    assert config.comp_weights_as_of(END - 1) == pytest.approx(
        {"lf": 0.375, "hf": 0.375, "markets": 0.25})


def test_shares_after_end_drop_hf_without_renormalizing_lf():
    s = config.comp_weights_as_of(END)
    assert "hf" not in s
    assert s == pytest.approx({"lf": 0.375, "agent": 0.25, "reserve": 0.125, "markets": 0.25})


def test_unarmed_agent_and_reserve_burn_their_share():
    lf = {1: 1.0}
    markets = {config.BURN_UID: 1.0}          # markets as it runs today: all burn
    out = competitions.combine({"lf": lf, "markets": markets},
                               config.comp_weights_as_of(END), burn_uid=config.BURN_UID)
    assert out[1] == pytest.approx(0.375)
    assert out[config.BURN_UID] == pytest.approx(0.625)


def test_referrer_shares_ignore_missing_competitions():
    # a recruit's LF tally is all that scores until agent publishes tallies
    shares = scoring.referrer_shares(config.comp_weights_as_of(END))
    blended = scoring.blended_recruit_tallies({"lf": {"a": 2.0, "b": 1.0}}, shares)
    assert blended["a"] / blended["b"] == pytest.approx(2.0)


def test_hf_ended_flag():
    assert not config.hf_ended_as_of(END - 1)
    assert config.hf_ended_as_of(END)


def test_new_hf_call_refused_after_end():
    with pytest.raises(hf.HFRejected, match="hf_ended"):
        hf.validate_submission({"trade_pair": "BTCUSD", "direction": "LONG"}, END)


def test_hf_call_before_end_is_not_refused_as_ended():
    try:
        hf.validate_submission({"trade_pair": "BTCUSD", "direction": "LONG"}, END - 60)
    except hf.HFRejected as e:
        assert "hf_ended" not in str(e)
