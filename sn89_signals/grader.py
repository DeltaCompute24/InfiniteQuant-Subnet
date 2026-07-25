"""Walk-forward touch grading.

Grading semantics:
  * a bracket level (TP/SL) is HIT when a 1-minute candle's CLOSE crosses it —
    the price that actually persisted to the end of the minute, NOT the intrabar
    high/low wick. This mirrors the SN8/PTN fill standard: a fill is priced off
    the traded/quoted price at a point in time, never a candle's extreme. A
    momentary wick that reverts by the close cannot move the close, so a level
    merely pierced for a tick does not score. Deciding on the close needs only
    the 1-minute aggregates — no secondary (1-second) feed and no
    availability-dependent fallback, so the grade is fully reproducible from the
    same minute bars every replay.
  * bars are bad-tick sanitized first (polygon.sanitize_minute_bars): a spike
    wick beyond the candle body and both neighbours by >tolerance (1% crypto,
    0.25% forex/metals/equities) is clamped unless a second feed (Hyperliquid,
    crypto only) traded the level in the same minute; non-crypto bars in the
    daily forex-rollover window [20:55,21:20) UTC are dropped outright — a
    single off-market print can't distort the close.
  * first candle whose close crosses a level decides
  * SL is checked before TP (conservative); a single close cannot satisfy both
    levels, but the ordering is kept for safety
  * no close crosses a level by horizon ⇒ WASHED (non-decisive)
  * outcome_bps is ±band on a hit, mark-to-horizon on wash

Equities EOD path intentionally NOT ported for v1 — the subnet board grades
everything on the intraday touch path; equities signals grade on 1-minute
aggs the same way (Polygon serves 1m stock aggs; market-hours gaps just mean
no bars, which the touch loop already tolerates).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import config, polygon
from .schema import Signal

WON, LOST, WASHED, PENDING = "won", "lost", "washed", "pending"


@dataclass
class Grade:
    status: str                  # won | lost | washed | pending
    outcome_bps: float | None
    exit_reason: str | None      # tp_touch | sl_touch | horizon_expired | None
    exit_at_ms: int | None
    entry_price: float | None


def touch_hit(px: float, is_long: bool, tp_price: float, sl_price: float):
    """THE canonical hit rule, shared by both mechanisms.

    A level is hit when a traded/quoted price touches it. TP sits strictly on one
    side of entry and SL on the other, so one price can never satisfy both -- there
    is no "gapped through both levels" case to arbitrate.

    The SUBSTRATE is the caller's business (ticks for mechanism 1; whatever
    config.grading_rule_as_of selects for mechanism 0). The RULE does not vary
    between mechanisms -- that is the entire point of this function existing.
    """
    if is_long:
        if px >= tp_price:
            return "won"
        if px <= sl_price:
            return "lost"
    else:
        if px <= tp_price:
            return "won"
        if px >= sl_price:
            return "lost"
    return None


def _grade_touch_ticks(sig: Signal, t0_ms: int, now_ms: int, horizon_ms: int,
                       is_long: bool, entry_price: float | None,
                       ticks: list[dict] | None) -> Grade:
    """touch_ticks substrate (config.grading_rule_as_of == 'touch_ticks').

    A level scores only after config.MIN_TOUCH_TICKS ticks TOUCH it — a lone
    reverting wick (1 tick) never scores. Grades off the tick mid `p`, the same
    series the trader's card shows as 'mark'. Entry = the last tick at or before
    t0 (mirrors HF), so card price == graded price. `ticks` injectable for tests;
    fetched from the anchored public windows otherwise."""
    scan_to = min(now_ms, horizon_ms)
    if ticks is None:
        from . import hf, hf_grade   # lazy: avoid an import cycle at module load
        tick_dir = os.path.join(os.path.expanduser("~/.sn89/hf-grade"), "ticks")
        ticks = hf_grade._ticks_for(hf.HF_PUBLIC_BASE, tick_dir, sig.trade_pair, t0_ms, scan_to)
    if entry_price is None:
        pre = [t for t in ticks if int(t["t"]) <= t0_ms]
        entry_price = float(pre[-1]["p"]) if pre else None
    if entry_price is None:
        return Grade(PENDING, None, None, None, None)   # no tick at/before t0 yet
    sign = 1 if is_long else -1
    tp_price = entry_price * (1 + sign * sig.tp_bps / 10_000)
    sl_price = entry_price * (1 - sign * sig.sl_bps / 10_000)
    need = config.MIN_TOUCH_TICKS
    tp_ct = sl_ct = 0
    last_p = None
    for t in ticks:
        tm = int(t["t"])
        if tm <= t0_ms:
            continue
        if tm > horizon_ms:
            break
        last_p = float(t["p"])
        r = touch_hit(last_p, is_long, tp_price, sl_price)
        if r == "lost":                      # SL touch — conservative, still gated by `need`
            sl_ct += 1
            if sl_ct >= need:
                return Grade(LOST, -sig.sl_bps, "sl_touch", tm, entry_price)
        elif r == "won":
            tp_ct += 1
            if tp_ct >= need:
                return Grade(WON, sig.tp_bps, "tp_touch", tm, entry_price)
    if now_ms >= horizon_ms:
        bps = ((last_p - entry_price) / entry_price) * 10_000 * sign if last_p is not None else None
        return Grade(WASHED, bps, "horizon_expired", horizon_ms, entry_price)
    return Grade(PENDING, None, None, None, entry_price)


def grade(sig: Signal, t0_ms: int, now_ms: int,
          entry_price: float | None = None,
          bars: list[dict] | None = None,
          ticks: list[dict] | None = None) -> Grade:
    """Grade one signal walk-forward from its commit time.

    The hit substrate is chosen as-of the call's t0 by config.grading_rule_as_of:
    'close_1m' (1-minute candle CLOSE crosses the level; `bars`) or 'touch_ticks'
    (≥ MIN_TOUCH_TICKS ticks touch it; `ticks`). `entry_price`/`bars`/`ticks` are
    injectable for golden tests; fetched live otherwise.
    """
    # Asset class is CANONICAL from the board, keyed by pair + commit time — NOT
    # the miner-committed sig.asset_class. The payload field is forgeable, and it
    # drives three outcome-affecting things below (price-feed routing, the wash
    # window, and the wick-sanitisation tolerance), so trusting it would let a
    # miner mis-class a pair to bend its own grade. Resolving from the pair closes
    # that; resolving as-of t0 keeps it stable across a board update.
    asset_class = config.asset_class_for(sig.trade_pair, t0_ms / 1000.0)
    is_long = sig.direction == "LONG"
    # wash window is class-fixed (crypto develops longer than fx/metals), §6.4
    horizon_h = config.class_horizon_h(asset_class)
    horizon_ms = t0_ms + horizon_h * 3_600_000
    scan_to = min(now_ms, horizon_ms)

    # Hit substrate chosen as-of t0 (never retroactive) — config.grading_rule_as_of.
    if config.grading_rule_as_of(t0_ms / 1000.0) == "touch_ticks":
        return _grade_touch_ticks(sig, t0_ms, now_ms, horizon_ms, is_long, entry_price, ticks)

    # close_1m substrate: a 1-minute candle CLOSE must cross the level.
    if entry_price is None:
        entry_price = polygon.entry_price_at(sig.trade_pair, asset_class, t0_ms)
    if entry_price is None:
        return Grade(PENDING, None, None, None, None)   # no data yet — retry later
    sign = 1 if is_long else -1
    tp_price = entry_price * (1 + sign * sig.tp_bps / 10_000)
    sl_price = entry_price * (1 - sign * sig.sl_bps / 10_000)
    if bars is None:
        bars = polygon.minute_aggs(sig.trade_pair, asset_class, t0_ms, scan_to)
        bars = polygon.sanitize_minute_bars(sig.trade_pair, asset_class, bars)
    for bar in bars:
        if bar["t"] < t0_ms or bar["t"] > horizon_ms:
            continue
        # Bracket hit decided on the CLOSE — the price that persisted to the end
        # of the minute — not the high/low wick. A wick that reverts by the close
        # cannot score (matches the SN8/PTN standard; needs no 1s feed).
        px = bar["c"]
        sl_hit = px <= sl_price if is_long else px >= sl_price
        tp_hit = px >= tp_price if is_long else px <= tp_price
        if sl_hit:      # both levels in one bar ⇒ SL first (conservative)
            return Grade(LOST, -sig.sl_bps, "sl_touch", bar["t"], entry_price)
        if tp_hit:
            return Grade(WON, sig.tp_bps, "tp_touch", bar["t"], entry_price)

    if now_ms >= horizon_ms:
        # mark-to-horizon for the wash record (informational bps)
        mark = bars[-1]["c"] if bars else None
        bps = ((mark - entry_price) / entry_price) * 10_000 * sign if mark else None
        return Grade(WASHED, bps, "horizon_expired", horizon_ms, entry_price)

    return Grade(PENDING, None, None, None, entry_price)
