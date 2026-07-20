"""Close-based bracket grading — a level is HIT only when a 1-minute candle's
CLOSE crosses it, not when the high/low wick pierces it. Mirrors the SN8/PTN
standard (fills price off the traded/quoted price, never a candle wick) and
needs no 1-second confirmation feed. Standalone so it doesn't pull the
crypto/timelock import chain. Run: python -m pytest tests/test_median_fill.py -q
or just `python tests/test_median_fill.py`."""
from sn89_signals.grader import grade, WON, LOST, PENDING, WASHED
from sn89_signals.schema import Signal

T0 = 1_700_000_000_000
ENTRY = 100.0
HK = "5" + "F" * 47


def _sig(direction="LONG"):
    # BTC band: tp/sl 150bps → LONG tp 101.5 / sl 98.5
    return Signal(trade_pair="BTCUSD", direction=direction, tp_bps=150, sl_bps=150,
                  ts_miner=T0, hotkey=HK, asset_class="crypto")


def _minute(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def _grade(direction, bars, now_off_h=1):
    return grade(_sig(direction), T0, T0 + now_off_h * 3_600_000,
                 entry_price=ENTRY, bars=bars)


def test_wick_to_tp_but_close_below_does_not_win():
    # High pierces TP (101.6 >= 101.5) but the minute closes back at 101.0.
    # The wick is untradeable — no fill.
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 101.6, 99.9, 101.0)])
    assert g.status == PENDING, g


def test_wick_to_sl_but_close_above_does_not_lose():
    # #3546 regression: low wicks below SL (98.4 <= 98.5) but closes back at 99.0
    # — exactly the 1-pip flicker that must NOT stop the call.
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 100.5, 98.4, 99.0)])
    assert g.status == PENDING, g


def test_close_beyond_tp_wins():
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 101.8, 100.5, 101.7)])
    assert (g.status, g.exit_reason, g.outcome_bps) == (WON, "tp_touch", 150), g


def test_close_beyond_sl_loses():
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 100.2, 98.0, 98.2)])
    assert (g.status, g.exit_reason, g.outcome_bps) == (LOST, "sl_touch", -150), g


def test_close_beyond_sl_loses_despite_tp_wick():
    # A single candle whose high pierced TP but whose close fell to SL grades
    # LOST — the close wins over the wick.
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 101.6, 98.0, 98.2)])
    assert (g.status, g.exit_reason) == (LOST, "sl_touch"), g


def test_wick_then_later_real_close():
    # Minute A: untradeable TP wick (ignored). Minute B: close crosses SL (graded).
    a, b = T0 + 60_000, T0 + 120_000
    bars = [_minute(a, 100, 101.6, 100.5, 101.0), _minute(b, 101, 101.2, 98.0, 98.1)]
    g = _grade("LONG", bars)
    assert (g.status, g.exit_reason) == (LOST, "sl_touch"), g


def test_short_close_beyond_tp():
    # SHORT BTC: tp 98.5, sl 101.5. Close below tp → WON.
    g = _grade("SHORT", [_minute(T0 + 60_000, 100, 100.2, 98.0, 98.3)])
    assert (g.status, g.outcome_bps) == (WON, 150), g


def test_no_close_cross_washes_at_horizon():
    # Wicks poke both sides all window but every close sits mid-band → WASH.
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 101.6, 98.4, 100.1)], now_off_h=80)
    assert (g.status, g.exit_reason) == (WASHED, "horizon_expired"), g


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
