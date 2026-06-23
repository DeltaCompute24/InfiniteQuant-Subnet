"""Massive (formerly Polygon.io) data access for validators/owner.

Endpoints used:
  * 1-minute aggregates for touch detection
  * 1-second aggregates for entry anchoring (minute open as fallback)
  * snapshot mid for still-open marks
"""
from __future__ import annotations

import time

import requests

from . import config


def _aggs(asset: str, asset_class: str, span: str,
          from_ms: int, to_ms: int) -> list[dict]:
    """[{t,o,h,l,c}] ascending; [] on failure (caller treats as 'no data yet')."""
    if not config.POLYGON_API_KEY:
        return []
    ticker = config.polygon_ticker(asset, asset_class)
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/{span}/"
           f"{from_ms}/{to_ms}?adjusted=true&sort=asc&limit=50000"
           f"&apiKey={config.POLYGON_API_KEY}")
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return []
        return [
            {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
            for b in (r.json().get("results") or [])
        ]
    except Exception:
        return []


def minute_aggs(asset: str, asset_class: str, from_ms: int, to_ms: int) -> list[dict]:
    return _aggs(asset, asset_class, "minute", from_ms, to_ms)


# ── Bad-tick sanitization (CONSENSUS — mirrors the IQ Signals board grader) ──
# Polygon's consolidated crypto feed carries occasional single-trade rogue
# prints from thin venues (2026-06-06: one ~$12 HYPE trade at 61.67 vs a 58.0x
# market put a +6.2% wick on a 1-min candle and falsely stopped three shorts).
# A lone print can never sustain through the median-fill confirmation, so paper
# grading must not be looser: an uncorroborated spike is clamped before it can
# register as a candidate touch.
#
# Rule: a candle extreme that overshoots the candle body AND both neighbour
# candles' extremes by >WICK_TOL is an uncorroborated spike. Crypto gets one
# appeal: if Hyperliquid's candle for the same minute reached the same level,
# the move was real and the wick stands. Otherwise the extreme is clamped to
# the corroborated reference level.

def fetch_hl_minute_aggs(asset: str, from_ms: int, to_ms: int) -> dict | None:
    """Hyperliquid 1-min candles (second feed for crypto corroboration).
    {minute_start_ms: {"h": float, "l": float}} or None on any failure.
    Historical HL candles are immutable, so validators replay identically."""
    import re
    coin = re.sub(r"USDT?$", "", str(asset))
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "candleSnapshot",
                  "req": {"coin": coin, "interval": "1m",
                          "startTime": from_ms, "endTime": to_ms}},
            timeout=8)
        if r.status_code != 200:
            return None
        j = r.json()
        if not isinstance(j, list):
            return None
        return {int(c["t"]): {"h": float(c["h"]), "l": float(c["l"])} for c in j}
    except Exception:
        return None


def _in_forex_rollover(t_ms: int) -> bool:
    """True if t_ms (UTC) falls in the daily forex-rollover junk window."""
    import datetime as _dt
    d = _dt.datetime.utcfromtimestamp(t_ms / 1000.0)
    (h0, m0), (h1, m1) = config.FOREX_ROLLOVER_UTC
    return (d.hour == h0 and d.minute >= m0) or (d.hour == h1 and d.minute < m1)


def sanitize_minute_bars(asset: str, asset_class: str, bars: list[dict],
                         hl_fetch=fetch_hl_minute_aggs) -> list[dict]:
    """Sanitize bars before touch grading: drop the daily forex-rollover window
    for non-crypto assets, then clamp uncorroborated spike wicks (class-aware
    tolerance). `hl_fetch` injectable for tests."""
    # drop the rollover junk window entirely (non-crypto only; crypto is 24/7)
    if asset_class != "crypto":
        bars = [b for b in bars if not _in_forex_rollover(b["t"])]
    wick_tol = config.WICK_TOL if asset_class == "crypto" else config.WICK_TOL_NONCRYPTO
    if len(bars) < 3:
        return bars
    # pass 1: suspects = extreme > wick_tol beyond body + both neighbours
    suspects, refs = [], []
    for i, b in enumerate(bars):
        prev = bars[i - 1] if i > 0 else None
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        ref_hi = max(b["o"], b["c"],
                     prev["h"] if prev else float("-inf"),
                     nxt["h"] if nxt else float("-inf"))
        ref_lo = min(b["o"], b["c"],
                     prev["l"] if prev else float("inf"),
                     nxt["l"] if nxt else float("inf"))
        if b["h"] > ref_hi * (1 + wick_tol) or b["l"] < ref_lo * (1 - wick_tol):
            suspects.append(i)
        refs.append((ref_hi, ref_lo))
    if not suspects:
        return bars
    # pass 2: crypto gets a second-feed appeal before the wick is clamped
    hl = (hl_fetch(asset, bars[0]["t"], bars[-1]["t"] + 60_000)
          if asset_class == "crypto" else None)
    out = list(bars)
    for i in suspects:
        b, (ref_hi, ref_lo) = bars[i], refs[i]
        hl_bar = (hl or {}).get(b["t"])
        h, l = b["h"], b["l"]
        if b["h"] > ref_hi * (1 + wick_tol):
            if hl_bar and hl_bar["h"] >= b["h"] * (1 - 0.001):
                pass  # corroborated by HL — real move, keep
            else:
                h = max(ref_hi, hl_bar["h"] if hl_bar else float("-inf"))
                print(f"  [BAD TICK] {asset} t={b['t']} high {b['h']} "
                      f"uncorroborated (ref {ref_hi:.6f}) → clamped to {h}")
        if b["l"] < ref_lo * (1 - wick_tol):
            if hl_bar and hl_bar["l"] <= b["l"] * (1 + 0.001):
                pass  # corroborated by HL — real move, keep
            else:
                l = min(ref_lo, hl_bar["l"] if hl_bar else float("inf"))
                print(f"  [BAD TICK] {asset} t={b['t']} low {b['l']} "
                      f"uncorroborated (ref {ref_lo:.6f}) → clamped to {l}")
        if h != b["h"] or l != b["l"]:
            out[i] = {**b, "h": h, "l": l}
    return out


def second_aggs(asset: str, asset_class: str, from_ms: int, to_ms: int) -> list[dict]:
    return _aggs(asset, asset_class, "second", from_ms, to_ms)


_price_cache: dict[str, tuple[float, float]] = {}  # asset -> (ts, price)


def snapshot_mid(asset: str, asset_class: str) -> float | None:
    if not config.POLYGON_API_KEY:
        return None
    cached = _price_cache.get(asset)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    if asset_class == "equities":
        t = config.polygon_ticker(asset, asset_class)
        url = (f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/"
               f"tickers/{t}?apiKey={config.POLYGON_API_KEY}")
    else:
        market = "crypto" if asset_class == "crypto" else "forex"
        t = config.polygon_ticker(asset, asset_class)
        url = (f"https://api.polygon.io/v2/snapshot/locale/global/markets/{market}/"
               f"tickers/{t}?apiKey={config.POLYGON_API_KEY}")
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return None
        tk = (r.json() or {}).get("ticker") or {}
        price = None
        if tk.get("lastTrade", {}).get("p"):
            price = tk["lastTrade"]["p"]
        elif tk.get("lastQuote", {}).get("a") and tk.get("lastQuote", {}).get("b"):
            price = (tk["lastQuote"]["a"] + tk["lastQuote"]["b"]) / 2
        elif tk.get("day", {}).get("c"):
            price = tk["day"]["c"]
        if price is not None:
            _price_cache[asset] = (time.time(), price)
        return price
    except Exception:
        return None


def entry_price_at(asset: str, asset_class: str, t0_ms: int) -> float | None:
    """Entry anchor (§6.2, docs/entry-timing.md §2.2): open of the first
    1-SECOND bar at/after t0 + LATENCY_BUFFER; falls back to the first
    1-minute bar only when the second feed has no bar in the scan window
    (sparse FX/metals off-hours). Deterministic and replayable — every
    validator that asks for the same window gets the same bar.
    """
    anchor_ms = t0_ms + config.LATENCY_BUFFER_S * 1000
    bars = second_aggs(asset, asset_class, anchor_ms,
                       anchor_ms + config.ENTRY_SECOND_SCAN_S * 1000)
    for b in bars:
        if b["t"] >= anchor_ms:
            return b["o"]
    # minute fallback — pre-cutover behavior
    bars = minute_aggs(asset, asset_class, anchor_ms, anchor_ms + 30 * 60 * 1000)
    for b in bars:
        if b["t"] >= anchor_ms:
            return b["o"]
    return None
