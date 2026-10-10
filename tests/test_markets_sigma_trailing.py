"""Markets V3.1: 15m/1h markets are sized from the 3 h before their start; days/weeks keep the day rule."""
import math
from decimal import Decimal

from sn89_signals import config, hf_grade, markets

S = 1791676800                                      # 2026-10-11 00:00 UTC (the arm)


def _rows(lo_s, hi_s, step_ret, asset="BTCUSD"):
    """A tick per 10 s whose minute averages alternate by +-step_ret (log) minute to minute."""
    out, px = [], 100.0
    for m in range((hi_s - lo_s) // 60):
        px *= math.exp(step_ret if m % 2 else -step_ret)
        for k in range(6):
            out.append({"a": asset, "t": (lo_s + m * 60 + k * 10) * 1000, "b": None, "k": None, "p": px})
    return out


def test_gate_is_by_window_and_start(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_SIGMA_TRAILING_FROM_UNIX", S)
    assert markets.uses_trailing_sigma(f"UD:BTCUSD:15m:{S}")
    assert markets.uses_trailing_sigma(f"UD:BTCUSD:1h:{S}")
    assert not markets.uses_trailing_sigma(f"UD:BTCUSD:15m:{S - 900}")
    monkeypatch.setattr(config, "MARKETS_SIGMA_TRAILING_FROM_UNIX", 0)
    assert not markets.uses_trailing_sigma(f"UD:BTCUSD:15m:{S}")


def test_trailing_sigma_reads_only_the_span_before_start():
    quiet = _rows(S - 10800, S, 0.0005)
    loud = _rows(S, S + 3600, 0.005)                 # after the start: must not count
    old = _rows(S - 20000, S - 10800, 0.005)         # before the span: must not count
    s = markets.trailing_sigma(old + quiet + loud, S)
    s_quiet = markets.sigma_span(quiet, (S - 10800) * 1000, S * 1000, 90)
    assert s == s_quiet and s is not None
    assert float(s) * math.sqrt(60) < 0.0006          # ~ the quiet step, not the loud one
    # too few returns -> None (the caller falls back to the day rule)
    assert markets.trailing_sigma(_rows(S - 1800, S, 0.0005), S) is None


def test_sigma_for_market_uses_trailing_then_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MARKETS_SIGMA_TRAILING_FROM_UNIX", S)
    db = markets._db(str(tmp_path))
    day = markets.sigma_day(S)
    db.execute("INSERT INTO v3_sigma VALUES (?,?,?,?)", ("BTCUSD", day, "0.00020", 1))
    rows = _rows(S - 10800, S, 0.0005)
    calls = []

    def fake(base, tick_dir, pair, t0, end):
        calls.append((t0, end))
        return rows, []
    monkeypatch.setattr(hf_grade, "_ticks_for", fake)
    s = markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S, window="15m")
    assert s == markets.trailing_sigma(rows, S) and calls == [((S - 10800) * 1000, S * 1000 - 1)]
    # cached: no second read
    assert markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S, window="15m") == s and len(calls) == 1
    # a daily market keeps the previous-day sigma
    assert markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S, window="1d") == Decimal("0.00020")
    # a market before the arm keeps the previous-day sigma
    s_old = markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S - 900, window="15m")
    assert s_old == Decimal(db.execute("SELECT sigma FROM v3_sigma WHERE asset='BTCUSD' AND day=?",
                                       (markets.sigma_day(S - 900),)).fetchone()[0])


def test_sparse_span_falls_back_and_a_missing_window_waits(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MARKETS_SIGMA_TRAILING_FROM_UNIX", S)
    db = markets._db(str(tmp_path))
    db.execute("INSERT INTO v3_sigma VALUES (?,?,?,?)", ("BTCUSD", markets.sigma_day(S), "0.00020", 1))
    monkeypatch.setattr(hf_grade, "_ticks_for", lambda *a: (_rows(S - 600, S, 0.0005), []))
    assert markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S, window="1h") == Decimal("0.00020")
    monkeypatch.setattr(hf_grade, "_ticks_for", lambda *a: ([], [S * 1000 - 180_000]))
    assert markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", S + 900, window="15m") is None


def test_day_rule_is_unchanged():
    day = 1791504000
    rows = _rows(day, day + 86400, 0.001)
    assert markets.sigma_per_s(rows, day) == markets.sigma_span(rows, day * 1000, (day + 86400) * 1000,
                                                                  config.MARKETS_V3_SIGMA_MIN_RETURNS)
