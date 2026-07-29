"""Walk-forward touch grading.

TWO HIT SUBSTRATES. Which one governs a call is chosen from its COMMIT TIME by
config.grading_rule_as_of(t0) and is never applied retroactively:

  * touch_ticks  (t0 >= config.TOUCH_TICKS_FROM = 2026-07-25T00:00:00Z) — CURRENT.
    A level scores once config.MIN_TOUCH_TICKS (default 2) ticks TOUCH it. Ticks
    come from the anchored public HF windows, so the grade is still replayable by
    anyone: the tick series is published and Merkle-anchored, not a private feed.
    Requiring >=2 ticks is what keeps a single stray print from scoring a level.

  * close_1m  (t0 < TOUCH_TICKS_FROM) — LEGACY, retained so historical calls
    regrade identically. A bracket level is HIT only when a 1-minute candle's
    CLOSE crosses it — the price that persisted to the end of the minute, not the
    intrabar high/low wick. That mirrored the SN8/PTN fill standard and needed
    only 1-minute aggregates, so it was reproducible without a sub-minute feed.

  ⚠ Do not read the close_1m paragraph as describing today's behaviour. It was the
  whole of this docstring until 2026-07-25 and is the reason a reader (and an
  agent) can conclude SN89 grades on minute closes. It does not.
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
    missing: list = []
    abandon_ms = None
    if ticks is None:
        from . import hf, hf_grade   # lazy: avoid an import cycle at module load
        tick_dir = os.path.join(os.path.expanduser("~/.sn89/hf-grade"), "ticks")
        ticks, missing = hf_grade._ticks_for(hf.HF_PUBLIC_BASE, tick_dir,
                                             sig.trade_pair, t0_ms, scan_to)
        abandon_ms = horizon_ms + hf_grade.GRADE_ABANDON_S * 1000
    if entry_price is None:
        pre = [t for t in ticks if int(t["t"]) <= t0_ms]
        entry_price = float(pre[-1]["p"]) if pre else None
    if entry_price is None:
        # No tick at or before t0. Usually the window simply has not published
        # yet, so waiting is right — but this used to return PENDING
        # UNCONDITIONALLY, before the abandon deadline below, so a call whose
        # entry can NEVER resolve had no deadline to free it and stayed pending
        # forever. Seen live: XAUUSD t0 2026-07-28T21:09:12Z, 35 784 ticks in
        # range and ZERO at or before t0 — t0 landed inside the 20:55-21:20 UTC
        # FX rollover, where gold quotes thin out, and _ticks_for reaches only
        # ONE window back, so no pre-t0 tick existed at any point.
        #
        # Past the SAME deadline the wash path uses, give up and record a
        # non-decisive wash. A call we cannot price can never be a win or a loss,
        # so this costs the miner nothing that a correct grade would have paid;
        # `no_entry_price` records WHY rather than implying the market did
        # nothing. Every validator crosses the deadline against the same (by then
        # final) published set, so they still converge.
        #
        # Deliberately NOT fixed by widening the entry lookback: reaching further
        # back through a rollover gap returns a price minutes stale, which is a
        # worse entry than admitting we have none.
        if abandon_ms is not None and now_ms >= abandon_ms:
            return Grade(WASHED, None, "no_entry_price", horizon_ms, None)
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
        if missing and abandon_ms is not None and now_ms < abandon_ms:
            # A touch found on a short series is still a real touch — a hole can
            # hide a level, never invent one — so WON/LOST above stand. WASHED is
            # the one conclusion a hole can fabricate: "nothing was touched" is
            # indistinguishable from "we are missing the prices". Stay PENDING and
            # retry until the window publishes (or until the abandon deadline, so
            # a permanently unsealed window cannot wedge the call forever).
            return Grade(PENDING, None, None, None, entry_price)
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
