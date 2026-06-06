"""Walk-forward touch grading.

Grading semantics:
  * brackets fill on TOUCH of the 1-minute bar's high/low
  * first bar that touches either level decides
  * a single bar touching BOTH levels grades LOST (conservative)
  * no touch by horizon ⇒ WASHED (non-decisive)
  * outcome_bps is ±band on touch, mark-to-horizon on wash

Equities EOD path intentionally NOT ported for v1 — the subnet board grades
everything on the intraday touch path; equities signals grade on 1-minute
aggs the same way (Polygon serves 1m stock aggs; market-hours gaps just mean
no bars, which the touch loop already tolerates).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import polygon
from .schema import Signal

WON, LOST, WASHED, PENDING = "won", "lost", "washed", "pending"


@dataclass
class Grade:
    status: str                  # won | lost | washed | pending
    outcome_bps: float | None
    exit_reason: str | None      # tp_touch | sl_touch | horizon_expired | None
    exit_at_ms: int | None
    entry_price: float | None


def grade(sig: Signal, t0_ms: int, now_ms: int,
          entry_price: float | None = None,
          bars: list[dict] | None = None) -> Grade:
    """Grade one signal walk-forward from its commit time.

    `entry_price` / `bars` injectable for golden tests; fetched live otherwise.
    """
    if entry_price is None:
        entry_price = polygon.entry_price_at(sig.trade_pair, sig.asset_class, t0_ms)
    if entry_price is None:
        return Grade(PENDING, None, None, None, None)   # no data yet — retry later

    sign = 1 if sig.direction == "LONG" else -1
    tp_price = entry_price * (1 + sign * sig.tp_bps / 10_000)
    sl_price = entry_price * (1 - sign * sig.sl_bps / 10_000)
    horizon_ms = t0_ms + sig.horizon_h * 3_600_000
    scan_to = min(now_ms, horizon_ms)

    if bars is None:
        bars = polygon.minute_aggs(sig.trade_pair, sig.asset_class, t0_ms, scan_to)

    for bar in bars:
        if bar["t"] < t0_ms or bar["t"] > horizon_ms:
            continue
        tp_touched = bar["h"] >= tp_price if sig.direction == "LONG" else bar["l"] <= tp_price
        sl_touched = bar["l"] <= sl_price if sig.direction == "LONG" else bar["h"] >= sl_price
        if tp_touched or sl_touched:
            if sl_touched:  # both-touched ⇒ SL first (conservative)
                return Grade(LOST, -sig.sl_bps, "sl_touch", bar["t"], entry_price)
            return Grade(WON, sig.tp_bps, "tp_touch", bar["t"], entry_price)

    if now_ms >= horizon_ms:
        # mark-to-horizon for the wash record (informational bps)
        mark = bars[-1]["c"] if bars else None
        bps = ((mark - entry_price) / entry_price) * 10_000 * sign if mark else None
        return Grade(WASHED, bps, "horizon_expired", horizon_ms, entry_price)

    return Grade(PENDING, None, None, None, entry_price)
