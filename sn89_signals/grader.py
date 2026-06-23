"""Walk-forward touch grading.

Grading semantics:
  * a 1-minute candle high/low touch is only a CANDIDATE fill; it is confirmed
    against the median CLOSE of 1-second bars over a rolling window before it
    grades WON/LOST (config.LIMIT_FILL_*). A practically tradeable fill requires
    price to persist through the level, not just tick it once — a momentary wick
    can never move the median, so a level merely pierced for a tick does not
    score. When 1-second data is unavailable (sparse FX/metals off-hours, or
    test-injected minute bars) the candle touch decides, preserving the old
    behaviour. A median cross implies a candle touch, so the cheap 1-minute scan
    never misses a confirmable fill.
  * bars are bad-tick sanitized first (polygon.sanitize_minute_bars): a spike
    wick beyond the candle body and both neighbours by >tolerance (1% crypto,
    0.25% forex/metals/equities) is clamped unless a second feed (Hyperliquid,
    crypto only) traded the level in the same minute; non-crypto bars in the
    daily forex-rollover window [20:55,21:20) UTC are dropped outright — a
    single off-market print can't trigger TP or SL
  * first candle touch whose median confirms decides
  * a single bar/median satisfying BOTH levels grades LOST (conservative)
  * no confirmed touch by horizon ⇒ WASHED (non-decisive)
  * outcome_bps is ±band on touch, mark-to-horizon on wash

Equities EOD path intentionally NOT ported for v1 — the subnet board grades
everything on the intraday touch path; equities signals grade on 1-minute
aggs the same way (Polygon serves 1m stock aggs; market-hours gaps just mean
no bars, which the touch loop already tolerates).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, polygon
from .schema import Signal

WON, LOST, WASHED, PENDING = "won", "lost", "washed", "pending"


def _median_close(samples: list[dict]) -> float:
    """Median close of 1s bars — sort by close and take sorted[len // 2]
    (upper-mid on even counts)."""
    closes = sorted(s["c"] for s in samples)
    return closes[len(closes) // 2]


def _confirm_touch_on_minute(direction: str, second_bars: list[dict],
                             minute_ms: int, tp_price: float, sl_price: float):
    """Confirm a candidate touch. Walk each 1-second bar in [minute, minute+60s)
    and, at each second, take the median close of a trailing window — the recent
    LIMIT_FILL_RECENT_S sub-window when it holds >LIMIT_FILL_MIN_SAMPLES bars,
    else the full LIMIT_FILL_WINDOW_S window. A fill triggers on a strict
    crossing, SL checked first (conservative). Returns ("lost"|"won", exit_ms)
    or (None, None) if the median never crosses within the minute (i.e. an
    untradeable wick)."""
    if not second_bars:
        return None, None
    is_long = direction == "LONG"
    win_ms = config.LIMIT_FILL_WINDOW_S * 1000
    recent_ms = config.LIMIT_FILL_RECENT_S * 1000
    minute_end = minute_ms + 60_000
    for b in second_bars:
        s = b["t"]
        if s < minute_ms or s >= minute_end:
            continue
        full = [x for x in second_bars if s - win_ms < x["t"] <= s]
        if not full:
            continue
        recent = [x for x in full if x["t"] > s - recent_ms]
        window = recent if len(recent) > config.LIMIT_FILL_MIN_SAMPLES else full
        med = _median_close(window)
        sl_hit = med < sl_price if is_long else med > sl_price
        tp_hit = med > tp_price if is_long else med < tp_price
        if sl_hit:                    # SL first ⇒ conservative
            return "lost", s
        if tp_hit:
            return "won", s
    return None, None


@dataclass
class Grade:
    status: str                  # won | lost | washed | pending
    outcome_bps: float | None
    exit_reason: str | None      # tp_touch | sl_touch | horizon_expired | None
    exit_at_ms: int | None
    entry_price: float | None


def grade(sig: Signal, t0_ms: int, now_ms: int,
          entry_price: float | None = None,
          bars: list[dict] | None = None,
          second_bars_by_minute: dict[int, list[dict]] | None = None) -> Grade:
    """Grade one signal walk-forward from its commit time.

    `entry_price` / `bars` injectable for golden tests; fetched live otherwise.
    `second_bars_by_minute` maps a candidate minute's start_ms → its 1-second
    bars; injectable for tests. On the live path 1s bars are fetched per
    candidate minute. When neither is available the candle touch decides.
    """
    if entry_price is None:
        entry_price = polygon.entry_price_at(sig.trade_pair, sig.asset_class, t0_ms)
    if entry_price is None:
        return Grade(PENDING, None, None, None, None)   # no data yet — retry later

    sign = 1 if sig.direction == "LONG" else -1
    tp_price = entry_price * (1 + sign * sig.tp_bps / 10_000)
    sl_price = entry_price * (1 - sign * sig.sl_bps / 10_000)
    # wash window is class-fixed (crypto develops longer than fx/metals), §6.4
    horizon_h = config.class_horizon_h(sig.asset_class)
    horizon_ms = t0_ms + horizon_h * 3_600_000
    scan_to = min(now_ms, horizon_ms)

    fetched = bars is None
    if bars is None:
        bars = polygon.minute_aggs(sig.trade_pair, sig.asset_class, t0_ms, scan_to)
        bars = polygon.sanitize_minute_bars(sig.trade_pair, sig.asset_class, bars)

    for bar in bars:
        if bar["t"] < t0_ms or bar["t"] > horizon_ms:
            continue
        tp_touched = bar["h"] >= tp_price if sig.direction == "LONG" else bar["l"] <= tp_price
        sl_touched = bar["l"] <= sl_price if sig.direction == "LONG" else bar["h"] >= sl_price
        if not (tp_touched or sl_touched):
            continue

        # Candle touch is only a CANDIDATE — confirm against the median window
        # so an untradeable wick doesn't score. Skip confirmation only when
        # disabled or no 1s data is reachable.
        sec = None
        if config.LIMIT_FILL_MEDIAN_CONFIRM:
            if second_bars_by_minute is not None:
                sec = second_bars_by_minute.get(bar["t"])
            elif fetched:
                sec = polygon.second_aggs(
                    sig.trade_pair, sig.asset_class,
                    bar["t"] - config.LIMIT_FILL_WINDOW_S * 1000, bar["t"] + 60_000)
        if sec:
            outcome, ms = _confirm_touch_on_minute(
                sig.direction, sec, bar["t"], tp_price, sl_price)
            if outcome == "lost":
                return Grade(LOST, -sig.sl_bps, "sl_touch", ms, entry_price)
            if outcome == "won":
                return Grade(WON, sig.tp_bps, "tp_touch", ms, entry_price)
            continue  # candle pierced but median never sustained ⇒ keep scanning

        # No 1s data (or confirmation disabled): candle touch decides.
        if sl_touched:  # both-touched ⇒ SL first (conservative)
            return Grade(LOST, -sig.sl_bps, "sl_touch", bar["t"], entry_price)
        return Grade(WON, sig.tp_bps, "tp_touch", bar["t"], entry_price)

    if now_ms >= horizon_ms:
        # mark-to-horizon for the wash record (informational bps)
        mark = bars[-1]["c"] if bars else None
        bps = ((mark - entry_price) / entry_price) * 10_000 * sign if mark else None
        return Grade(WASHED, bps, "horizon_expired", horizon_ms, entry_price)

    return Grade(PENDING, None, None, None, entry_price)
