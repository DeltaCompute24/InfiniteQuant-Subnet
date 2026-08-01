#!/usr/bin/env python3
"""Closers positions feed publisher (+ test-stack index.json maintenance).

Derives the network's OPEN positions from the PTN websocket event journal
(append-only open/close events for our SN8 hotkeys), filters them to pairs the
HF tick feed can actually grade, and writes the feed the Closers ingest
validates against and the dashboard renders:

    {"generated_at_ms": ..., "positions": [
        {"id": <position_uuid>, "trade_pair": "BTCUSD", "direction": "LONG",
         "opened_ms": ..., "miner": "<hotkey prefix>"}, ...]}

Pairs NOT on the tick feed are excluded rather than published-but-ungradeable:
an ungradeable position would accept submissions that can never score, which is
the void-farming surface the design review flagged. (Today that excludes the
whole *USDC Hyperliquid book — the marks/ticks gap measured 2026-07-31.)

SN89_CLOSERS_SYNTH_POSITIONS=1 appends two clearly-labelled synthetic positions
(BTCUSD LONG / EURUSD SHORT) so a testnet demo always has something to click
even when the live book holds no tick-covered pair. Synthetic rows carry
"synthetic": true and must NEVER be enabled on the mainnet feed.

When SN89_HF_PUBLIC_DIR_INDEX is set, also rebuilds <dir>/index.json from the
window subdirectories each cycle — the test stack serves the public dir with a
bare python http.server, which (unlike the mainnet webhook) has no dynamic
index endpoint.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import config, hf  # noqa: E402

EVENTS = os.getenv("SN89_CLOSERS_EVENTS_JSONL",
                   "/opt/iq-platform/data/live/ptn-ws-position-events.jsonl")

# USDC-quoted Hyperliquid perp positions grade against their USD spot twin's
# tick series (the tick corpus carries no USDC pairs at all — the marks gap
# measured 2026-07-31 that hid HALF the book, 735/1472 positions). The basis
# between an HL perp and its spot twin over a 1h horizon is a fraction of the
# move being graded, and the mapping is committed here so it is consensus, not
# convention: the feed publishes trade_pair = the ALIAS (what grading uses)
# and keeps the venue pair in `venue_pair` for display.
PAIR_ALIAS = {
    "GOLDUSDC": "XAUUSD", "SILVERUSDC": "XAGUSD",
    "BTCUSDC": "BTCUSD", "ETHUSDC": "ETHUSD", "SOLUSDC": "SOLUSD",
    "XRPUSDC": "XRPUSD", "HYPEUSDC": "HYPEUSD", "TAOUSDC": "TAOUSD",
}
OUT = os.getenv("SN89_CLOSERS_POSITIONS_OUT",
                "/opt/sn89-blobs/hf-test/positions.json")
INDEX_DIR = os.getenv("SN89_HF_PUBLIC_DIR_INDEX", "")
SYNTH = os.getenv("SN89_CLOSERS_SYNTH_POSITIONS", "0") == "1"
EVERY_S = float(os.getenv("SN89_CLOSERS_PUBLISH_EVERY_S", "5"))


def gradeable_pairs() -> set[str]:
    """Pairs the HF tick recorder covers: HF board ∪ LF board (mirrors the
    recorder's own default pair list)."""
    pairs = set(hf.HF_BOARD_V1)
    try:
        pairs |= set(config.load_bands().keys())
    except Exception:  # noqa: BLE001 — board unreadable → HF pairs still work
        pass
    return {p.upper() for p in pairs}


def open_positions() -> list[dict]:
    ok_pairs = gradeable_pairs()
    opens: dict[str, dict] = {}
    try:
        fh = open(EVENTS, encoding="utf-8")
    except OSError:
        return []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            u = e.get("position_uuid")
            if not u:
                continue
            if e.get("is_close"):
                opens.pop(u, None)
            elif u not in opens and e.get("direction") in ("LONG", "SHORT"):
                venue_pair = str(e.get("asset", "")).upper()
                pair = PAIR_ALIAS.get(venue_pair, venue_pair)
                opens[u] = {
                    "id": u,
                    "trade_pair": pair,
                    "venue_pair": venue_pair,
                    "direction": e.get("direction"),
                    "opened_ms": int(e.get("processed_ms") or 0),
                    "miner": str(e.get("miner_hotkey", ""))[:8],
                }
    return [p for p in opens.values() if p["trade_pair"] in ok_pairs]


def synth_positions() -> list[dict]:
    day = time.strftime("%Y%m%d", time.gmtime())
    return [
        {"id": f"synth-btc-{day}", "trade_pair": "BTCUSD", "direction": "LONG",
         "opened_ms": int(time.time() * 1000) - 3_600_000, "miner": "synthetic",
         "synthetic": True},
        {"id": f"synth-eur-{day}", "trade_pair": "EURUSD", "direction": "SHORT",
         "opened_ms": int(time.time() * 1000) - 3_600_000, "miner": "synthetic",
         "synthetic": True},
    ]


def rebuild_index(public_dir: str) -> int:
    ws = sorted(int(d) for d in os.listdir(public_dir)
                if d.isdigit() and os.path.isdir(os.path.join(public_dir, d)))
    tmp = os.path.join(public_dir, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"windows": ws}, fh)
    os.replace(tmp, os.path.join(public_dir, "index.json"))
    return len(ws)


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(f"closers-positions: events={EVENTS} out={OUT} synth={SYNTH} "
          f"index_dir={INDEX_DIR or '-'}", flush=True)
    while True:
        pos = open_positions()
        if SYNTH:
            pos += synth_positions()
        doc = {"generated_at_ms": int(time.time() * 1000), "positions": pos}
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp, OUT)
        n_w = rebuild_index(INDEX_DIR) if INDEX_DIR and os.path.isdir(INDEX_DIR) else -1
        print(f"  published {len(pos)} positions"
              + (f" · index {n_w} windows" if n_w >= 0 else ""), flush=True)
        time.sleep(EVERY_S)


if __name__ == "__main__":
    main()
