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
import sqlite3
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

# A position enters the feed only after its POSITION-LEVEL leveraged return
# (direction × price-move × net leverage — the ±% the operator bot shows) has
# reached ±MIN_MOVE_BPS (10 = 0.10%, either direction — a drawdown is exactly
# when a CLOSE vote is informative). Fresh positions sitting near flat P&L
# would invite votes on chop that grade as coin flips and pressure us to exit
# trades that never went anywhere. Leverage matters: a 0.10× book needs a 1%
# price move to reach 0.10% position P&L. Once a position crosses, it is
# LATCHED into the feed for life even if P&L chops back, so the votable set
# never flickers. 0 disables the gate.
MIN_MOVE_BPS = float(os.getenv("SN89_CLOSERS_MIN_MOVE_BPS", "10"))
MARKS_DB = os.getenv("IQ_MARKS_DB", "/opt/iq-platform/data/live/signals-marks.db")
LATCH_PATH = OUT + ".latch.json"

# Which hotkeys' positions are votable. Comma-separated ss58 (or unique
# prefixes); EMPTY publishes every key on the event journal (testnet default).
# The set lives in the service unit's env, never in this public file — the
# feed itself publishes no key identity either (see MINER_LABEL): votable
# positions are "the network's book", not attributable to a wallet.
ALLOW_HOTKEYS = tuple(h.strip() for h in
                      os.getenv("SN89_CLOSERS_HOTKEY_ALLOW", "").split(",")
                      if h.strip())
MINER_LABEL = os.getenv("SN89_CLOSERS_MINER_LABEL", "iq")
# Per-key display overrides: "<hotkey-or-prefix>:<label>,..." — e.g. the demo
# key that seeds the votable set reads "Demo" so nobody grades it as an SN89
# book. Everything else publishes the flat anonymous MINER_LABEL.
MINER_LABELS = {}
for part in os.getenv("SN89_CLOSERS_MINER_LABELS", "").split(","):
    if ":" in part:
        k, _, v = part.strip().partition(":")
        if k and v:
            MINER_LABELS[k] = v


def miner_label(hotkey: str) -> str:
    for k, v in MINER_LABELS.items():
        if hotkey.startswith(k):
            return v
    return MINER_LABEL


def gradeable_pairs() -> set[str]:
    """Pairs the HF tick recorder covers: HF board ∪ LF board (mirrors the
    recorder's own default pair list).

    ⚠ THE LF HALF MUST INDEX ["bands"]. `load_bands()` returns the whole
    document — {"version", "bands", "note", "refreshed_at", "vol_window_days"} —
    so `set(load_bands().keys())` unions in FIVE METADATA STRINGS and ZERO
    pairs. The votable set silently collapsed to HF_BOARD_V1, which is exactly
    what this docstring promises it is not, and no test or log line said so:
    the junk keys uppercase to plausible-looking pair names and every position
    on an LF-only pair simply never appeared in the feed.

    Measured on the position bus over the 30 days to 2026-08-25: AUDUSD,
    HYPEUSD, TAOUSD, USDCAD and XAGUSD were unvotable, dropping 275 of 3,122
    fleet opens (8.8%) and 99 of 906 allowlisted opens (10.9%). TAOUSD had been
    a graded LF pair since 2026-08-11 and was never once votable.

    `config.allowed_assets()` exists for precisely this and is what the tick
    recorder uses (neurons/hf_tick_recorder.py). Use it, not load_bands().
    """
    pairs = set(hf.HF_BOARD_V1)
    try:
        pairs |= set(config.allowed_assets().keys())
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
            elif e.get("direction") in ("LONG", "SHORT"):
                hk = str(e.get("miner_hotkey", ""))
                if ALLOW_HOTKEYS and not hk.startswith(ALLOW_HOTKEYS):
                    continue
                if u in opens:
                    # later fill on the same position (add / partial reduce) —
                    # refresh the avg entry and net leverage the gate uses
                    opens[u]["direction"] = e.get("direction")
                    opens[u]["entry_price"] = float(
                        e.get("entry_price") or opens[u]["entry_price"])
                    opens[u]["net_leverage"] = abs(float(
                        e.get("net_leverage") or opens[u]["net_leverage"]))
                    continue
                venue_pair = str(e.get("asset", "")).upper()
                pair = PAIR_ALIAS.get(venue_pair, venue_pair)
                opens[u] = {
                    "id": u,
                    "trade_pair": pair,
                    "venue_pair": venue_pair,
                    "direction": e.get("direction"),
                    "opened_ms": int(e.get("processed_ms") or 0),
                    "miner": miner_label(hk),
                    "entry_price": float(e.get("entry_price") or 0),
                    "net_leverage": abs(float(e.get("net_leverage") or 0)),
                }
    return [p for p in opens.values() if p["trade_pair"] in ok_pairs]


def gate_moved(pos: list[dict]) -> list[dict]:
    """Apply the ±MIN_MOVE_BPS entry gate (see constant above). Fail-open on
    missing entry price or mark: a marks-store gap must degrade to the old
    behavior (publish everything), never blank the votable set."""
    if MIN_MOVE_BPS <= 0:
        return pos
    try:
        latched = set(json.load(open(LATCH_PATH)))
    except Exception:  # noqa: BLE001 — first run / unreadable → empty latch
        latched = set()
    con = None
    try:
        con = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        pass
    out = []
    for p in pos:
        pid = str(p["id"])
        if p.get("synthetic") or pid in latched:
            out.append(p)
            continue
        entry = p.get("entry_price") or 0.0
        lev = p.get("net_leverage") or 0.0
        mark = None
        if con is not None and entry:
            try:
                row = con.execute(
                    "SELECT mark FROM marks WHERE asset=? "
                    "ORDER BY ts DESC LIMIT 1", (p["trade_pair"],)).fetchone()
                mark = row[0] if row else None
            except sqlite3.Error:
                mark = None
        if not entry or not mark or not lev:
            out.append(p)
            continue
        sign = 1.0 if p.get("direction") == "LONG" else -1.0
        ret_bps = sign * (mark / entry - 1.0) * lev * 1e4
        if abs(ret_bps) >= MIN_MOVE_BPS:
            latched.add(pid)
            out.append(p)
    if con is not None:
        con.close()
    # prune closed positions so the latch never grows unbounded
    latched &= {str(p["id"]) for p in pos}
    tmp = LATCH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(latched), fh)
    os.replace(tmp, LATCH_PATH)
    return out


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
        pos = gate_moved(open_positions())
        if SYNTH:
            pos += synth_positions()
        # entry_price/net_leverage are gate inputs only — published, they would
        # fingerprint the key against PTN's public per-hotkey positions API,
        # defeating the anonymous MINER_LABEL.
        pub = [{k: v for k, v in p.items()
                if k not in ("entry_price", "net_leverage")} for p in pos]
        doc = {"generated_at_ms": int(time.time() * 1000), "positions": pub}
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
