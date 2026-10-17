"""Gold daily / weekly windows (from 2026-10-18 17:00 EDT): Sunday-Thursday day starts, Friday weeks."""
import calendar

from sn89_signals import config, markets


def U(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))


ARM = U(2026, 10, 18, 21)                                        # Sun 5 pm EDT


def test_arm_default_is_sunday_5pm_new_york():
    assert config.MARKETS_CAL_GOLD_FROM_UNIX == ARM
    assert markets.window_start("1d", ARM) == ARM


def test_gold_days_start_sunday_to_thursday_only():
    for d in (18, 19, 20, 21, 22):                               # Sun .. Thu
        assert markets.market_exists(f"UD:XAUUSD:1d:{U(2026, 10, d, 21)}"), d
    assert not markets.market_exists(f"UD:XAUUSD:1d:{U(2026, 10, 23, 21)}")   # Fri
    assert not markets.market_exists(f"UD:XAUUSD:1d:{U(2026, 10, 24, 21)}")   # Sat
    # after the November change the day starts 22:00 UTC; still Sunday in New York
    assert markets.market_exists(f"UD:XAUUSD:1d:{U(2026, 11, 1, 22)}")


def test_gold_weeks_run_friday_to_friday_from_the_arm():
    assert markets.market_exists(f"UD:XAUUSD:1w:{U(2026, 10, 23, 21)}")
    assert not markets.market_exists(f"UD:XAUUSD:1w:{U(2026, 10, 16, 21)}")   # before the arm
    assert sorted(markets.markets_for(U(2026, 10, 23, 21), "1w")) == [
        f"UD:BTCUSD:1w:{U(2026, 10, 23, 21)}", f"UD:XAUUSD:1w:{U(2026, 10, 23, 21)}"]


def test_nothing_changes_before_the_arm():
    assert not markets.market_exists(f"UD:XAUUSD:1d:{U(2026, 10, 15, 21)}")   # a Thursday, pre-arm
    assert markets.market_exists(f"UD:BTCUSD:1d:{U(2026, 10, 16, 21)}")
    assert markets.markets_for(U(2026, 10, 16, 21), "1w") == [f"UD:BTCUSD:1w:{U(2026, 10, 16, 21)}"]


def test_bitcoin_days_keep_every_weekday():
    for d in (23, 24):
        assert markets.market_exists(f"UD:BTCUSD:1d:{U(2026, 10, d, 21)}")


def test_gold_calendar_sigma_skips_the_weekend():
    fri = U(2026, 10, 16)
    assert markets.sigma_day(U(2026, 10, 18, 21), "XAUUSD", "1d") == fri      # Sun start: Sat -> Fri
    assert markets.sigma_day(U(2026, 10, 19, 21), "XAUUSD", "1d") == fri      # Mon start: Sun -> Fri
    assert markets.sigma_day(U(2026, 10, 20, 21), "XAUUSD", "1d") == U(2026, 10, 19)
    assert markets.sigma_day(U(2026, 10, 23, 21), "XAUUSD", "1w") == U(2026, 10, 22)
    # unchanged: Bitcoin, gold 15m/1h, and the one-argument call
    assert markets.sigma_day(U(2026, 10, 19, 21), "BTCUSD", "1d") == U(2026, 10, 18)
    assert markets.sigma_day(U(2026, 10, 19, 21), "XAUUSD", "1h") == U(2026, 10, 18)
    assert markets.sigma_day(U(2026, 10, 19, 21)) == U(2026, 10, 18)
