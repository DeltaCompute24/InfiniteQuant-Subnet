#!/usr/bin/env python3
"""SN89 band recalibration — LF (mecid 0) and HF (mecid 1).

WHAT IT HOLDS CONSTANT, AND WHY THAT MATTERS
--------------------------------------------
The controller targets STRUCTURAL wash: the probability that a price path never
travels `band` bps from entry, either way, inside the horizon. Bands are symmetric
so this is direction-independent, which makes it a property of PRICE ALONE.

That choice is load-bearing. Targeting OBSERVED wash would create a feedback loop
with the wash penalty: miners get more selective -> observed wash falls -> the
controller widens -> the contest gets harder -> miners over-correct. Structural
wash contains no miner behaviour, so miners improving cannot move the controller's
input. It responds to genuine volatility regime change and to nothing else.

Corollary: observed wash falling below the target is the wash penalty WORKING and
must not be corrected. The health metric for the contest is DISCRIMINATION
(true hit-rate SD / mean standard error), not the wash rate. See
SN89-SIGNALS-ARCHITECTURE.md.

WHY 1m BARS ARE A VALID INPUT
-----------------------------
Measured 2026-07-26: bar-high/low single-touch, tick single-touch, and the live
tick >=2-touch rule agree within 2pp (BTCUSD 22.4 / 22.5 / 24.5%). The grading
substrate does not materially change the calibration.

THE PARAMETER THAT ACTUALLY MATTERS IS THE LOOKBACK
---------------------------------------------------
BTCUSD structural wash was 10.6% over 2.5 months but 22.4% over 2.5 days — a 2x
swing from vol regime alone. Calibrating on a short window is what broke the
2026-07-21 FX pass (it ran in a week where FX sigma was 0.60-0.75x normal, so
NZDUSD looked over-vol and was narrowed when it needed widening). LOOKBACK_DAYS
is deliberately long and the window is calendar-aligned so any third party
replaying the journal recomputes the identical number.

SAFETY
------
propose-only unless SN89_BANDS_AUTO_APPLY=1. Deadband suppresses churn, a rate
limit prevents a regime shift whipsawing the board, HF is floored at
MIN_BAND_SPREAD_RATIO x spread, and any applied change is a NEW history entry
with effective_from in the future — never retroactive.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import hf

OHLC_DB = os.getenv("SN89_OHLC_DB", "/opt/iq-platform/quant-kb/ohlc.db")
REPO = Path(os.getenv("SN89_REPO", "/opt/sn89-signals"))
BOARD_PATH = REPO / "data" / "signals-bands.json"
HISTORY_PATH = REPO / "data" / "signals-bands-history.json"
PROPOSAL_PATH = Path(os.getenv("SN89_BANDS_PROPOSAL",
                               "/opt/iq-platform/data/live/sn89-bands-proposal.json"))

LOOKBACK_DAYS = int(os.getenv("SN89_BANDS_LOOKBACK_DAYS", "90"))
LF_TARGET = float(os.getenv("SN89_BANDS_LF_TARGET", "0.40"))
HF_TARGET = float(os.getenv("SN89_BANDS_HF_TARGET", "0.14"))
DEADBAND = float(os.getenv("SN89_BANDS_DEADBAND", "0.07"))      # wash pp
RATE_LIMIT = float(os.getenv("SN89_BANDS_RATE_LIMIT", "0.20"))  # band change/cycle
MIN_SAMPLES = int(os.getenv("SN89_BANDS_MIN_SAMPLES", "600"))
EFFECTIVE_IN_H = int(os.getenv("SN89_BANDS_EFFECTIVE_IN_H", "48"))
AUTO_APPLY = os.getenv("SN89_BANDS_AUTO_APPLY", "0") == "1"

LF_HORIZON_H = {"crypto": 8, "forex": 12, "forex-commodities": 12}
FX_DROP_HOUR = 21   # corrupt 21:00 UTC bar in quant-kb/ohlc.db (FX-BOOK-RESEARCH.md)

_cache: dict = {}


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [bands] {m}", flush=True)


def window_bounds(now: float | None = None) -> tuple[int, int]:
    """Calendar-aligned [start, end) so the window is reproducible by anyone."""
    d = datetime.fromtimestamp(now or time.time(), timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int((d - timedelta(days=LOOKBACK_DAYS)).timestamp()), int(d.timestamp())


def excursions(db, asset: str, horizon_s: int, drop_h21: bool,
               lo_ts: int, hi_ts: int) -> list:
    """(max_favourable, max_adverse) in bps for each sampled entry."""
    key = (asset, horizon_s, drop_h21, lo_ts, hi_ts)
    if key in _cache:
        return _cache[key]
    rows = [(ts, h, l, c) for ts, h, l, c in db.execute(
        "SELECT ts,high,low,close FROM ohlc_intraday WHERE asset=? AND timeframe='1m' "
        "AND ts>=? AND ts<? ORDER BY ts", (asset, lo_ts, hi_ts))
        if not (drop_h21 and ((ts // 3600) % 24) == FX_DROP_HOUR)]
    out = []
    if len(rows) >= 3000:
        ts = [r[0] for r in rows]; hi = [r[1] for r in rows]
        lo = [r[2] for r in rows]; cl = [r[3] for r in rows]
        step = max(1, min(15, horizon_s // 120))
        for i in range(0, len(rows) - 1, step):
            t0, e = ts[i], cl[i]
            if e <= 0:
                continue
            j, mx, mn = i, e, e
            while j < len(rows) and ts[j] <= t0 + horizon_s:
                if hi[j] > mx: mx = hi[j]
                if lo[j] < mn: mn = lo[j]
                j += 1
            k = min(j, len(rows) - 1)
            if ts[k] - t0 < horizon_s * 0.9:
                continue
            out.append(((mx / e - 1) * 1e4, (1 - mn / e) * 1e4))
    _cache[key] = out
    return out


def wash_rate(ex: list, band: float) -> float:
    return sum(1 for u, d in ex if u < band and d < band) / len(ex)


def solve_band(ex: list, target: float, lo=0.2, hi=1500.0) -> float:
    for _ in range(70):
        mid = (lo + hi) / 2
        if wash_rate(ex, mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def evaluate(db, asset, cur_band, horizon_s, drop21, target, floor, lo_ts, hi_ts,
             integer: bool):
    ex = excursions(db, asset, horizon_s, drop21, lo_ts, hi_ts)
    if len(ex) < MIN_SAMPLES:
        return {"asset": asset, "action": "hold", "reason":
                f"insufficient samples ({len(ex)} < {MIN_SAMPLES})",
                "current": cur_band}
    cur_w = wash_rate(ex, cur_band)
    if abs(cur_w - target) <= DEADBAND:
        return {"asset": asset, "action": "hold", "reason":
                f"within deadband ({cur_w:.1%} vs {target:.0%})",
                "current": cur_band, "wash_now": round(cur_w, 4)}
    raw = solve_band(ex, target)
    limited = min(max(raw, cur_band * (1 - RATE_LIMIT)), cur_band * (1 + RATE_LIMIT))
    floored = max(limited, floor) if floor else limited
    new = int(round(floored)) if integer else round(floored, 1)
    if new == cur_band:
        return {"asset": asset, "action": "hold", "reason": "rounds to no change",
                "current": cur_band, "wash_now": round(cur_w, 4)}
    notes = []
    if abs(raw - limited) > 1e-6:
        notes.append(f"rate-limited from {raw:.1f}")
    if floor and floored > limited + 1e-6:
        notes.append(f"raised to {floor:.1f} spread floor")
    return {"asset": asset, "action": "change", "current": cur_band, "proposed": new,
            "wash_now": round(cur_w, 4), "wash_target": target,
            "wash_at_proposed": round(wash_rate(ex, new), 4),
            "pct_change": round((new - cur_band) / cur_band, 4),
            "samples": len(ex), "notes": "; ".join(notes) or None}


def run(now: float | None = None) -> dict:
    lo_ts, hi_ts = window_bounds(now)
    db = sqlite3.connect(OHLC_DB)
    _log(f"window {datetime.fromtimestamp(lo_ts, timezone.utc):%F} -> "
         f"{datetime.fromtimestamp(hi_ts, timezone.utc):%F} ({LOOKBACK_DAYS}d)")

    lf_board = json.loads(BOARD_PATH.read_text())
    lf_rows = []
    for a, spec in lf_board["bands"].items():
        cls = spec["asset_class"]
        lf_rows.append(evaluate(
            db, a, spec["tp_bps"], LF_HORIZON_H.get(cls, 12) * 3600,
            cls != "crypto", LF_TARGET, None, lo_ts, hi_ts, integer=True))

    hf_rows = []
    for a, (tp, _sl, hs, cls) in hf.HF_BOARD_V1.items():
        floor = hf.MIN_BAND_SPREAD_RATIO * hf.HF_TYPICAL_SPREAD_BPS.get(a, 0)
        hf_rows.append(evaluate(db, a, float(tp), hs, cls != "crypto",
                                HF_TARGET, floor, lo_ts, hi_ts, integer=False))

    prop = {
        "generated_at": datetime.fromtimestamp(now or time.time(),
                                               timezone.utc).isoformat(),
        "window": {"from": lo_ts, "to": hi_ts, "lookback_days": LOOKBACK_DAYS},
        "params": {"lf_target": LF_TARGET, "hf_target": HF_TARGET,
                   "deadband": DEADBAND, "rate_limit": RATE_LIMIT,
                   "min_samples": MIN_SAMPLES},
        "lf": lf_rows, "hf": hf_rows,
        "auto_apply": AUTO_APPLY,
    }
    for label, rows in (("LF", lf_rows), ("HF", hf_rows)):
        ch = [r for r in rows if r["action"] == "change"]
        _log(f"{label}: {len(ch)} change / {len(rows) - len(ch)} hold")
        for r in ch:
            _log(f"  {r['asset']:8} {r['current']} -> {r['proposed']} "
                 f"({r['pct_change']:+.0%})  wash {r['wash_now']:.1%} -> "
                 f"{r['wash_at_proposed']:.1%}"
                 + (f"  [{r['notes']}]" if r.get("notes") else ""))
    return prop


def verify_propagation() -> int:
    """All user-facing copies must equal the consensus board. This is the check
    that would have caught the BTC 103-vs-76 drift found 2026-07-26."""
    copies = {
        "board": BOARD_PATH,
        "platform seed": Path("/opt/iq-platform/data/seed/signals-bands.json"),
        "dashboard": Path(
            "/opt/iq-platform/apps/dashboard-public/lib/signals-bands.json"),
    }
    ref = {k: v["tp_bps"] for k, v in json.loads(BOARD_PATH.read_text())["bands"].items()}
    bad = 0
    for label, p in copies.items():
        if label == "board" or not p.exists():
            continue
        other = json.loads(p.read_text()).get("bands", {})
        for a, tp in ref.items():
            got = (other.get(a) or {}).get("tp_bps")
            if got != tp:
                print(f"DRIFT {label:14} {a:8} consensus={tp} copy={got}")
                bad += 1
    print("propagation OK" if not bad else f"{bad} drifted entries")
    return bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-propagation", action="store_true")
    ap.add_argument("--now", type=float, default=None,
                    help="override wall clock (testing/replay)")
    a = ap.parse_args()
    if a.verify_propagation:
        sys.exit(1 if verify_propagation() else 0)
    p = run(a.now)
    PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL_PATH.write_text(json.dumps(p, indent=1))
    _log(f"proposal written to {PROPOSAL_PATH}")
    if not AUTO_APPLY:
        _log("PROPOSE-ONLY (SN89_BANDS_AUTO_APPLY != 1) — no board written")
