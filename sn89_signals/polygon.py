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
