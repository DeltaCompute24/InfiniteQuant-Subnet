"""Median-fill confirmation — standalone so it doesn't pull the crypto/timelock
import chain. Run: python -m pytest tests/test_median_fill.py -q
or just `python tests/test_median_fill.py`."""
from sn89_signals import config
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


def _secs(minute_ms, closes, step_ms=1000, start_off_ms=0):
    """1-second bars within a minute; each close drives o/h/l/c (only close used)."""
    return [{"t": minute_ms + start_off_ms + i * step_ms, "o": c, "h": c, "l": c, "c": c}
            for i, c in enumerate(closes)]


def _grade(direction, bars, sbm, now_off_h=1):
    return grade(_sig(direction), T0, T0 + now_off_h * 3_600_000,
                 entry_price=ENTRY, bars=bars, second_bars_by_minute=sbm)


def test_backcompat_no_second_bars_candle_decides():
    # No 1s data → candle touch decides, exactly as before.
    g = _grade("LONG", [_minute(T0 + 60_000, 100, 101.6, 99.9, 101)], None)
    assert (g.status, g.exit_reason) == (WON, "tp_touch")


def test_untradeable_wick_does_not_win():
    # Candle high pierces TP (101.6 >= 101.5) but only one 1s tick is above TP;
    # the rest sit at ~101.0, so the median never crosses → not a fill.
    mb = T0 + 60_000
    closes = [101.0] * 20
    closes[5] = 101.9          # lone spike — the untradeable wick
    g = _grade("LONG", [_minute(mb, 100, 101.6, 100.5, 101.0)], {mb: _secs(mb, closes)})
    assert g.status == PENDING, g          # within horizon, no confirmed fill yet


def test_sustained_move_confirms_win():
    # Price holds above TP for the back half of the window → median crosses.
    mb = T0 + 60_000
    closes = [101.0] * 8 + [101.7] * 14    # sustained beyond TP=101.5
    g = _grade("LONG", [_minute(mb, 100, 101.8, 100.5, 101.7)], {mb: _secs(mb, closes)})
    assert (g.status, g.exit_reason) == (WON, "tp_touch"), g


def test_sustained_sl_confirms_loss():
    mb = T0 + 60_000
    closes = [99.0] * 8 + [98.2] * 14      # sustained below SL=98.5
    g = _grade("LONG", [_minute(mb, 100, 100.2, 98.0, 98.2)], {mb: _secs(mb, closes)})
    assert (g.status, g.exit_reason) == (LOST, "sl_touch"), g


def test_wick_then_later_real_fill():
    # Minute A: untradeable TP wick (ignored). Minute B: sustained SL (graded).
    a, b = T0 + 60_000, T0 + 120_000
    a_closes = [101.0] * 20; a_closes[3] = 101.9
    b_closes = [99.0] * 6 + [98.1] * 16
    bars = [_minute(a, 100, 101.6, 100.5, 101.0), _minute(b, 101, 101.2, 98.0, 98.1)]
    g = _grade("LONG", bars, {a: _secs(a, a_closes), b: _secs(b, b_closes)})
    assert (g.status, g.exit_reason) == (LOST, "sl_touch"), g


def test_short_sustained_tp():
    # SHORT BTC: tp 98.5, sl 101.5. Sustained below tp → WON.
    mb = T0 + 60_000
    closes = [99.0] * 8 + [98.3] * 14
    g = _grade("SHORT", [_minute(mb, 100, 100.2, 98.0, 98.3)], {mb: _secs(mb, closes)})
    assert (g.status, g.outcome_bps) == (WON, 150), g


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
