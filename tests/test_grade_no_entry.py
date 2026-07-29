"""The no-entry path must respect the abandon deadline.

INCIDENT: _grade_touch_ticks returned PENDING unconditionally when no tick
resolved at or before t0, and it did so BEFORE the abandon branch — so a call
whose entry could never resolve had no deadline to free it. Live case: XAUUSD
t0 2026-07-28T21:09:12Z, 35 784 ticks in range and ZERO at or before t0, because
t0 landed in the 20:55-21:20 UTC FX rollover where gold quotes thin out and
_ticks_for reaches only one window back. It sat pending indefinitely.
"""
import pytest

from sn89_signals import config, grader
from sn89_signals.grader import grade, PENDING, WASHED, WON
from sn89_signals.schema import Signal

T0 = (config.TOUCH_TICKS_FROM + 4 * 86_400) * 1000      # safely on the touch_ticks side
HK = "5" + "F" * 47
HORIZON_MS = 8 * 3_600_000                              # crypto
ABANDON_MS = 21_600 * 1000                              # GRADE_ABANDON_S


def _sig(direction="LONG"):
    return Signal(trade_pair="BTCUSD", direction=direction, tp_bps=105, sl_bps=105,
                  ts_miner=T0, hotkey=HK, asset_class="crypto")


def _tick(t, p):
    return {"a": "BTCUSD", "b": p - 1, "k": p + 1, "p": p, "t": t}


def test_touch_ticks_path_is_actually_selected():
    assert config.grading_rule_as_of(T0 / 1000.0) == "touch_ticks"


def test_injected_ticks_with_no_pre_t0_tick_stay_pending():
    """With ticks injected there is no abandon deadline (tests are deterministic),
    so the behaviour is unchanged: wait."""
    ticks = [_tick(T0 + 60_000, 64_000.0)]              # nothing at or before t0
    g = grade(_sig(), T0, T0 + HORIZON_MS + ABANDON_MS * 2, ticks=ticks)
    assert g.status == PENDING
    assert g.entry_price is None


def _live(monkeypatch, ticks, missing=()):
    from sn89_signals import hf_grade
    monkeypatch.setattr(hf_grade, "_ticks_for",
                        lambda base, d, pair, t0, end: (list(ticks), list(missing)))


def test_unresolvable_entry_still_pending_before_the_deadline(monkeypatch):
    _live(monkeypatch, [_tick(T0 + 60_000, 64_000.0)])
    now = T0 + HORIZON_MS + ABANDON_MS - 60_000         # one minute short
    g = grade(_sig(), T0, now)
    assert g.status == PENDING, g
    assert g.entry_price is None


def test_unresolvable_entry_is_abandoned_past_the_deadline(monkeypatch):
    """THE REGRESSION — this hung forever before the fix."""
    _live(monkeypatch, [_tick(T0 + 60_000, 64_000.0)])
    now = T0 + HORIZON_MS + ABANDON_MS + 1
    g = grade(_sig(), T0, now)
    assert g.status == WASHED, g
    assert g.exit_reason == "no_entry_price"
    assert g.entry_price is None
    assert g.outcome_bps is None


def test_abandoned_no_entry_is_non_decisive():
    """It must never land as a win or a loss — we could not price the call."""
    assert WASHED not in ("won", "lost")


def test_a_resolvable_entry_past_the_deadline_still_grades_normally(monkeypatch):
    """The new branch must not hijack calls that CAN be priced."""
    _live(monkeypatch, [_tick(T0 - 1_000, 64_000.0),           # entry
                        _tick(T0 + 60_000, 64_000.0 * 1.02),   # well through TP
                        _tick(T0 + 61_000, 64_000.0 * 1.02)])  # >= MIN_TOUCH_TICKS
    now = T0 + HORIZON_MS + ABANDON_MS + 1
    g = grade(_sig(), T0, now)
    assert g.status == WON, g
    assert g.entry_price == 64_000.0
