"""Markets daily / weekly windows: 5 pm New York, weeks ending Friday, Bitcoin only, sparse grading."""
import calendar

import pytest

from sn89_signals import config, markets, oracle


def U(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))


def test_ny_offset_follows_us_dst():
    assert markets._ny_offset_s(U(2026, 10, 10, 21)) == -4 * 3600
    assert markets._ny_offset_s(U(2026, 12, 1, 12)) == -5 * 3600
    # 2026: DST starts Sun Mar 8 07:00 UTC, ends Sun Nov 1 06:00 UTC
    assert markets._ny_offset_s(U(2026, 3, 8, 6, 59)) == -5 * 3600
    assert markets._ny_offset_s(U(2026, 3, 8, 7)) == -4 * 3600
    assert markets._ny_offset_s(U(2026, 11, 1, 5, 59)) == -4 * 3600
    assert markets._ny_offset_s(U(2026, 11, 1, 6)) == -5 * 3600


def test_daily_window_is_5pm_new_york():
    t = U(2026, 10, 10, 18)                                      # Sat 2 pm EDT
    s = markets.window_start("1d", t)
    assert s == U(2026, 10, 9, 21)                               # Fri 5 pm EDT
    assert markets.window_end("1d", s) == U(2026, 10, 10, 21)
    assert markets.window_start("1d", U(2026, 10, 10, 21)) == U(2026, 10, 10, 21)
    # the day that crosses the November change is 25 hours, back to 22:00 UTC
    s = markets.window_start("1d", U(2026, 11, 1, 12))
    assert s == U(2026, 10, 31, 21) and markets.window_end("1d", s) == U(2026, 11, 1, 22)
    assert markets.window_end("1d", U(2026, 11, 1, 22)) == U(2026, 11, 2, 22)


def test_weekly_window_runs_friday_to_friday():
    s = markets.window_start("1w", U(2026, 10, 10, 18))
    assert s == U(2026, 10, 9, 21)
    assert markets.window_end("1w", s) == U(2026, 10, 16, 21)
    assert markets.window_start("1w", U(2026, 10, 16, 20, 59)) == s
    assert markets.window_start("1w", U(2026, 10, 16, 21)) == U(2026, 10, 16, 21)
    assert markets.window_end("1w", U(2026, 10, 30, 21)) == U(2026, 11, 6, 22)


def test_ids_parse_and_misaligned_ids_refuse():
    s = U(2026, 10, 16, 21)
    assert markets.parse_market_id(f"UD:BTCUSD:1w:{s}") == ("BTCUSD", "1w", s, U(2026, 10, 23, 21))
    with pytest.raises(markets.MarketError):
        markets.parse_market_id(f"UD:BTCUSD:1w:{U(2026, 10, 15, 21)}")     # a Thursday
    with pytest.raises(markets.MarketError):
        markets.parse_market_id(f"UD:BTCUSD:1d:{U(2026, 10, 15, 0)}")
    assert markets.parse_market_id(f"UD:BTCUSD:15m:{U(2026, 10, 15, 0)}")[3] == U(2026, 10, 15, 0, 15)


def test_calendar_markets_are_bitcoin_only_and_from_the_arm(monkeypatch):
    s = U(2026, 10, 16, 21)
    monkeypatch.setattr(config, "MARKETS_CAL_FROM_UNIX", s)
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", U(2026, 10, 1))
    assert markets.market_exists(f"UD:BTCUSD:1w:{s}")
    assert markets.market_exists(f"UD:BTCUSD:1d:{s}")
    assert not markets.market_exists(f"UD:ETHUSD:1d:{s}")
    assert not markets.market_exists(f"UD:BTCUSD:1d:{U(2026, 10, 15, 21)}")    # before the arm
    assert markets.markets_for(s, "1w") == [f"UD:BTCUSD:1w:{s}"]
    monkeypatch.setattr(config, "MARKETS_CAL_FROM_UNIX", 0)
    assert not markets.market_exists(f"UD:BTCUSD:1w:{s}")


def test_calendar_markets_take_bets_only_in_window():
    s = U(2026, 10, 16, 21)
    mid = f"UD:BTCUSD:1d:{s}"
    pre, live = markets.entry_intervals(mid)
    assert pre[0] == pre[1]                                       # empty: no pre-start bets
    assert live == (s + config.MARKETS_V3_OPEN_DELAY_S, U(2026, 10, 17, 21) - 1800)
    assert not markets.in_entry(mid, s - 30)
    assert markets.in_entry(mid, s + 3600)
    assert markets.late_cutoff_s("1w") == 3600
    # the 15m rule is unchanged: opens one window early
    m15 = f"UD:BTCUSD:15m:{s}"
    assert markets.entry_intervals(m15)[0] == (s - 900, s - config.MARKETS_ENTRY_CLOSE_LEAD_S)


def test_rows_for_spans_reads_only_the_windows_it_needs(tmp_path, monkeypatch):
    W = oracle.WINDOW_MS
    fetched = []

    def load(base, cache, w):
        fetched.append(w)
        return [{"t": w + 1000, "a": "BTCUSD", "o": "1"}, {"t": w + 2000, "a": "ETHUSD", "o": "2"}]
    monkeypatch.setattr(oracle, "load_window", load)
    t = 1000 * W
    rows, missing = oracle.rows_for_spans("B", str(tmp_path), "BTCUSD",
                                          [(t, t + 1000), (t + 500 * W, t + 500 * W + 1000)])
    assert missing == [] and all(r["a"] == "BTCUSD" for r in rows)
    assert len(fetched) == 4                                       # 2 spans x (lead-in + its window)
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)


def test_cal_spans_cover_target_settle_and_each_fill(tmp_path):
    s = U(2026, 10, 16, 21)
    mid = f"UD:BTCUSD:1d:{s}"
    db = markets._db(str(tmp_path))
    db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
               ("hk:1", mid, "acct", "UP", "10", 0, (s + 7200) * 1_000_000, "hk", 1))
    e = markets.window_end("1d", s)
    spans = markets._cal_spans(db, mid, s, e)
    assert ((s - 60) * 1000, s * 1000) in spans and ((e - 60) * 1000, e * 1000) in spans
    assert ((s + 7200) * 1000, (s + 7200 + config.MARKETS_ORACLE_FILL_WAIT_S) * 1000) in spans
