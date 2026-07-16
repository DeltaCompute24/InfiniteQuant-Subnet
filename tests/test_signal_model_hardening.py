"""Miner-supplied fields must not be trusted as internal/consensus data.

Standalone (no crypto/timelock import chain — runs on macOS too). Covers:
  #1  grade() derives asset_class from the BOARD (keyed by pair), never the
      miner-committed sig.asset_class — so a forged class can't bend the wash
      window / feed routing / wick tolerance.
  #2  the board is resolved AS OF the commit block, so a band update between
      commit and reveal never retroactively voids an in-flight signal.

Run: python -m pytest tests/test_signal_model_hardening.py -q
"""
import json

import pytest

from sn89_signals import config
from sn89_signals.grader import grade, WON, WASHED
from sn89_signals.schema import Signal, ValidationError, validate

T0 = 1_700_000_000_000          # ms
HK = "5" + "F" * 47
H = 3_600_000                   # one hour in ms


def _minute(t_ms, hi):
    return {"t": t_ms, "o": 1.0, "h": hi, "l": 1.0, "c": 1.0}


# ── #1: forged asset_class is ignored by grading ──────────────────────────────
def test_asset_class_for_uses_board_not_payload():
    # EURUSD is forex on the vendored board, whatever a payload might claim.
    # (Deliberately a pair on the CURRENT board: this call passes no t0, so it
    # resolves against the live board rather than the as-of history.)
    assert config.asset_class_for("EURUSD") == "forex"
    assert config.horizon_h_for("EURUSD") == config.CLASS_HORIZON_H["forex"]


def test_grade_ignores_forged_asset_class():
    """AUDCAD is forex (12 h window). Forge asset_class=crypto (8 h) in the
    payload and place a TP touch at t0+10 h — inside the real forex window but
    outside the forged crypto one. Grading must honour the board class: WON."""
    assert config.CLASS_HORIZON_H["forex"] == 12      # guards the test's premise
    assert config.CLASS_HORIZON_H["crypto"] == 8
    sig = Signal(trade_pair="AUDCAD", direction="LONG", tp_bps=100, sl_bps=100,
                 ts_miner=T0, hotkey=HK, asset_class="crypto", horizon_h=8)  # FORGED
    touch = _minute(T0 + 10 * H, hi=1.02)             # tp_price = 1.01 → touched
    g = grade(sig, T0, T0 + 20 * H, entry_price=1.0, bars=[touch])
    assert g.status == WON, (
        "grade trusted the forged crypto class (8 h) and dropped the 10 h touch")


def test_grade_forged_class_does_not_extend_window():
    """Symmetric check: a crypto pair (8 h) forged as equities (48 h) must still
    expire at the crypto horizon — a touch at t0+20 h is out of window."""
    sig = Signal(trade_pair="BTCUSD", direction="LONG", tp_bps=150, sl_bps=150,
                 ts_miner=T0, hotkey=HK, asset_class="equities", horizon_h=48)  # FORGED
    late = _minute(T0 + 20 * H, hi=2.0)               # well past crypto 8 h
    g = grade(sig, T0, T0 + 40 * H, entry_price=1.0, bars=[late])
    assert g.status == WASHED, "forged equities class extended the crypto window"


# ── #2: bands resolved as of the commit block ─────────────────────────────────
def _history(tmp_path):
    hist = [
        {"version": "old", "effective_from_unix": 0,
         "bands": {"AUDCAD": {"asset_class": "forex", "tp_bps": 50, "sl_bps": 50}}},
        {"version": "new", "effective_from_unix": 2000,
         "bands": {"AUDCAD": {"asset_class": "forex", "tp_bps": 80, "sl_bps": 80}}},
    ]
    p = tmp_path / "signals-bands-history.json"
    p.write_text(json.dumps(hist))
    return p


def test_bands_as_of_picks_commit_time_version(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_BANDS_HISTORY_PATH", _history(tmp_path))
    monkeypatch.setattr(config, "_bands_history_cache", None)
    assert config.allowed_assets_as_of(1000)["AUDCAD"]["tp_bps"] == 50   # before cutover
    assert config.allowed_assets_as_of(3000)["AUDCAD"]["tp_bps"] == 80   # after cutover


def test_band_update_does_not_void_in_flight_signal(tmp_path, monkeypatch):
    """A signal valid under the band in force AT COMMIT must still validate at
    reveal, even though the current board has since changed."""
    monkeypatch.setattr(config, "_BANDS_HISTORY_PATH", _history(tmp_path))
    monkeypatch.setattr(config, "_bands_history_cache", None)
    committed_under_old = Signal(
        trade_pair="AUDCAD", direction="LONG", tp_bps=50, sl_bps=50,
        ts_miner=0, hotkey=HK, asset_class="forex")
    # committed at t0=1000 → NORMALIZED to the t0=1000 band (50/50), never voided
    out1 = validate(committed_under_old, t0_unix=1000)
    assert (out1.tp_bps, out1.sl_bps) == (50.0, 50.0)
    # bands_as_of still governs WHICH band applies: a signal committed post-cutover
    # normalizes to the new board (80/80) instead of raising on a stale band.
    stale = Signal(trade_pair="AUDCAD", direction="LONG", tp_bps=50, sl_bps=50,
                   ts_miner=0, hotkey=HK, asset_class="forex")
    out2 = validate(stale, t0_unix=3000)
    assert (out2.tp_bps, out2.sl_bps) == (80.0, 80.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
