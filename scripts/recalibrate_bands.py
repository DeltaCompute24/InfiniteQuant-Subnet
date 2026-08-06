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
NZDUSD looked over-vol and was narrowed when it needed widening). The window is
calendar-aligned so any third party replaying the journal recomputes the identical
number.

LOOKBACK_DAYS was 90 until 2026-07-30 on the reasoning that longer is safer. A
walk-forward says otherwise. Fit the 40%-target band on the prior W days, then
measure the wash it actually produced over the next 21 days; 12 pairs, 10 evaluation
dates, 5m bars (levels run low on 5m, the ranking does not):

    W      mean|err|   bias    p90|err|   churn
    7d       11.6pp   +3.8pp    24.6pp    14.0%
    14d       9.0     +1.1      20.4       9.0%
    21d       8.5     +0.3      16.8       4.2%
    30d       8.4     -0.2      16.5       4.1%
    45d       7.9     +0.5      15.1       3.5%   <- adopted
    60d       8.9     +2.4      16.5       2.6%
    90d      10.1     +6.4      18.6       2.1%

90d is not merely noisier, it is BIASED: bands fitted on 90 days wash 6.4pp above
target going forward, i.e. they come out systematically too wide, which is the
complaint a trader raised on NZDCHF the day this was measured. The optimum is broad
and flat from 21d to 45d and 45d costs only 1.4pp of extra churn over 90d. Below
21d the 2026-07-21 failure mode returns. Windows overlap across evaluation dates, so
this is a ranking and not a significance test — re-run it when the 1m corpus is deep
enough to test 90d+ honestly.

THE WINDOW IS A REQUEST, NOT A GUARANTEE
----------------------------------------
`ohlc_intraday` only holds 1m bars from the day a pair joined the tick corpus, so a
recently-added pair is silently fitted on a shorter window than the board claims.
Measured 2026-07-30 on a nominal 90d pass: 77d delivered on the genesis assets, 48-55d
on the seven FX pairs added in June, and MIN_SAMPLES never fired because entries are
sampled every 15 min (3,200+ samples off 50 days). A window that differs by pair breaks
both the cross-pair fairness the equal-wash target exists to provide and the
replayability claim above. MIN_COVERAGE now holds any pair whose delivered span falls
short, and every proposal row carries `window_days` so the shortfall is auditable.

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

LOOKBACK_DAYS = int(os.getenv("SN89_BANDS_LOOKBACK_DAYS", "45"))
LF_TARGET = float(os.getenv("SN89_BANDS_LF_TARGET", "0.40"))
HF_TARGET = float(os.getenv("SN89_BANDS_HF_TARGET", "0.14"))
DEADBAND = float(os.getenv("SN89_BANDS_DEADBAND", "0.07"))      # wash pp
RATE_LIMIT = float(os.getenv("SN89_BANDS_RATE_LIMIT", "0.20"))  # band change/cycle
MIN_SAMPLES = int(os.getenv("SN89_BANDS_MIN_SAMPLES", "600"))
MIN_COVERAGE = float(os.getenv("SN89_BANDS_MIN_COVERAGE", "0.80"))  # of requested span
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


def coverage_days(db, asset: str, lo_ts: int, hi_ts: int) -> float:
    """Span of 1m history actually present inside the requested window, in days.

    Max-minus-min, not a bar count, so weekend gaps in FX do not read as a short
    corpus — this is here to catch a pair whose tick history STARTS after the window
    opens. See "THE WINDOW IS A REQUEST" above.
    """
    row = db.execute(
        "SELECT MIN(ts), MAX(ts) FROM ohlc_intraday WHERE asset=? AND timeframe='1m' "
        "AND ts>=? AND ts<?", (asset, lo_ts, hi_ts)).fetchone()
    if not row or row[0] is None:
        return 0.0
    return (row[1] - row[0]) / 86400.0


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
    have = coverage_days(db, asset, lo_ts, hi_ts)
    want = (hi_ts - lo_ts) / 86400.0
    base = {"asset": asset, "current": cur_band,
            "window_days": round(have, 1), "window_days_requested": round(want, 1)}
    if len(ex) < MIN_SAMPLES:
        return {**base, "action": "hold", "reason":
                f"insufficient samples ({len(ex)} < {MIN_SAMPLES})"}
    if have < want * MIN_COVERAGE:
        return {**base, "action": "hold", "reason":
                f"short corpus ({have:.0f}d of {want:.0f}d requested, "
                f"floor {MIN_COVERAGE:.0%})"}
    cur_w = wash_rate(ex, cur_band)
    if abs(cur_w - target) <= DEADBAND:
        return {**base, "action": "hold", "reason":
                f"within deadband ({cur_w:.1%} vs {target:.0%})",
                "wash_now": round(cur_w, 4)}
    raw = solve_band(ex, target)
    limited = min(max(raw, cur_band * (1 - RATE_LIMIT)), cur_band * (1 + RATE_LIMIT))
    floored = max(limited, floor) if floor else limited
    new = int(round(floored)) if integer else round(floored, 1)
    if new == cur_band:
        return {**base, "action": "hold", "reason": "rounds to no change",
                "wash_now": round(cur_w, 4)}
    notes = []
    if abs(raw - limited) > 1e-6:
        notes.append(f"rate-limited from {raw:.1f}")
    if floor and floored > limited + 1e-6:
        notes.append(f"raised to {floor:.1f} spread floor")
    return {**base, "action": "change", "proposed": new,
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
                   "min_samples": MIN_SAMPLES, "min_coverage": MIN_COVERAGE},
        "lf": lf_rows, "hf": hf_rows,
        "auto_apply": AUTO_APPLY,
    }
    short = sorted({(r["asset"], r["window_days"]) for r in lf_rows + hf_rows
                    if r.get("window_days") is not None
                    and r["window_days"] < r["window_days_requested"] * MIN_COVERAGE})
    if short:
        _log(f"SHORT CORPUS — held, fitted window under {MIN_COVERAGE:.0%} of "
             f"{LOOKBACK_DAYS}d: "
             + ", ".join(f"{a} {d:.0f}d" for a, d in short))
    for label, rows in (("LF", lf_rows), ("HF", hf_rows)):
        ch = [r for r in rows if r["action"] == "change"]
        _log(f"{label}: {len(ch)} change / {len(rows) - len(ch)} hold")
        for r in ch:
            _log(f"  {r['asset']:8} {r['current']} -> {r['proposed']} "
                 f"({r['pct_change']:+.0%})  wash {r['wash_now']:.1%} -> "
                 f"{r['wash_at_proposed']:.1%}"
                 + (f"  [{r['notes']}]" if r.get("notes") else ""))
    return prop


def _write_proposal(p: dict) -> None:
    """Write the proposal atomically.

    Uses a temp file + os.replace rather than write_text. write_text opens the
    EXISTING file, so it needs write permission on that FILE; os.replace needs it
    on the DIRECTORY. On 2026-08-01 the monthly run computed a correct proposal
    (the only change it found was HF XAUUSD 12.0 -> 10.1) and then lost it to
    PermissionError because the stale proposal had drifted to another owner. A
    propose-only controller whose output vanishes is worse than no controller: it
    reports success in the log right up to the line that throws.
    """
    PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROPOSAL_PATH.with_name(PROPOSAL_PATH.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(p, indent=1))
        os.chmod(tmp, 0o644)
        os.replace(tmp, PROPOSAL_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _log(f"proposal written to {PROPOSAL_PATH}")

def verify_propagation() -> int:
    """All user-facing copies must equal the consensus board. This is the check
    that would have caught the BTC 103-vs-76 drift found 2026-07-26.

    `data/seed/signals-bands.json` was checked here until 2026-07-30 and reported
    16 drifts on every run, because it is NOT a copy of the board — it is the
    EWMA-7d vol table `signals-vol-bands` rewrites every 15 min, and it is supposed
    to differ. A permanently-red alarm is an ignored alarm, and this one hid a real
    defect for as long as it ran: 22 strategy agents plus the Arabic landing page
    were reading that seed as if it were the board (USDJPY 53 vs 14, XAUUSD 59 vs
    96). Those consumers now read the board directly (IQ_BANDS_FILE), so the seed
    is no longer a copy of anything and does not belong in this check. Anything
    that adds a NEW copy of the board belongs here; a vol input does not.
    """
    copies = {
        "board": BOARD_PATH,
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
    _write_proposal(p)
    if not AUTO_APPLY:
        _log("PROPOSE-ONLY (SN89_BANDS_AUTO_APPLY != 1) — no board written")
