#!/usr/bin/env python3
"""SN89 Markets oracle recorder: the published Hyperliquid oraclePx series Markets V4 settles on.

Separate from the HF tick recorder by design: it reads only Hyperliquid's public API and writes only
its own directory. Format and integrity rules live in sn89_signals/oracle.py.

Sources
    WebSocket `activeAssetCtx` per mapped coin (main dex and `xyz:` coins on one socket). When no
    message has arrived for REST_AFTER_S, REST `metaAndAssetCtxs` per dex is polled instead (one call
    returns every asset on a dex) until the socket recovers.

Rows
    One row when an asset's oraclePx changes, and a heartbeat row with the unchanged value every
    oracle.HEARTBEAT_S, but ONLY while that asset has been observed within FRESH_S. A recorder that
    stops hearing from Hyperliquid therefore stops writing, and the gap is visible to grading.

Layout (DATA_DIR)
    live/<w>.jsonl          the open window, appended as rows arrive (the Markets service tails it)
    <w>/oracle.jsonl        sealed rows, ordered (t, a, o)
    <w>/oracle.json         {"w", "n", "root"}
A window seals SEAL_LAG_S after it ends; a restart seals whatever live files it finds.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import config, oracle  # noqa: E402

DATA_DIR = Path(os.getenv("SN89_MARKETS_ORACLE_DIR", "/var/lib/sn89-markets-oracle"))
WS_URL = os.getenv("SN89_HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
REST_URL = os.getenv("SN89_HL_INFO_URL", "https://api.hyperliquid.xyz/info")
REST_AFTER_S = 8.0
REST_EVERY_S = 2.0
STALE_POLL_S = 6.0
FRESH_S = 10.0
SEAL_LAG_S = 5.0


def _log(m: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {m}", flush=True)


class Recorder:
    def __init__(self):
        self.by_coin = {c: a for a, c in config.markets_oracle_assets(time.time()).items()}
        self.last_o: dict[str, str] = {}
        self.last_row_ms: dict[str, int] = {}
        self.seen_ms: dict[str, int] = {}
        self.last_ws_msg = 0.0
        self.rows_written = 0
        (DATA_DIR / "live").mkdir(parents=True, exist_ok=True)

    # ── rows ──
    def _write(self, asset: str, o: str, t_ms: int) -> None:
        w = oracle.window_of(t_ms)
        line = json.dumps({"t": t_ms, "a": asset, "o": o}, separators=(",", ":")) + "\n"
        with open(DATA_DIR / "live" / f"{w}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line)
        self.last_o[asset] = o
        self.last_row_ms[asset] = t_ms
        self.rows_written += 1

    def observe(self, coin: str, o) -> None:
        asset = self.by_coin.get(coin)
        if asset is None or o in (None, ""):
            return
        o = str(o)
        now = int(time.time() * 1000)
        self.seen_ms[asset] = now
        if self.last_o.get(asset) != o:
            self._write(asset, o, now)

    def heartbeat(self) -> None:
        now = int(time.time() * 1000)
        for asset, o in list(self.last_o.items()):
            if now - self.seen_ms.get(asset, 0) > FRESH_S * 1000:
                continue                                   # not hearing it: write nothing
            if now - self.last_row_ms.get(asset, 0) >= oracle.HEARTBEAT_S * 1000:
                self._write(asset, o, now)

    # ── sealing ──
    def seal_due(self) -> None:
        now_ms = int(time.time() * 1000)
        for f in sorted((DATA_DIR / "live").glob("*.jsonl")):
            try:
                w = int(f.stem)
            except ValueError:
                continue
            if now_ms < w + oracle.WINDOW_MS + SEAL_LAG_S * 1000:
                continue
            rows = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                               # a torn last line from a crash
                if oracle.window_of(int(r["t"])) == w:
                    rows.append(r)
            rows.sort(key=oracle.row_order)
            out = DATA_DIR / str(w)
            out.mkdir(exist_ok=True)
            tmp = out / "oracle.jsonl.tmp"
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, out / "oracle.jsonl")
            meta = {"w": w, "n": len(rows), "root": oracle.window_root(rows)}
            tmp = out / "oracle.json.tmp"
            tmp.write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, out / "oracle.json")
            for p in (out, out / "oracle.jsonl", out / "oracle.json"):
                try:
                    p.chmod(0o755 if p.is_dir() else 0o644)
                except OSError:
                    pass
            f.unlink()
            _log(f"sealed {w}: {len(rows)} rows, {len({r['a'] for r in rows})} assets")

    # ── sources ──
    async def ws_loop(self) -> None:
        import websockets
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, max_size=2 ** 22) as ws:
                    for coin in self.by_coin:
                        await ws.send(json.dumps({"method": "subscribe",
                                                  "subscription": {"type": "activeAssetCtx", "coin": coin}}))
                    _log(f"ws subscribed {len(self.by_coin)} coins")
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if msg.get("channel") != "activeAssetCtx":
                            continue
                        d = msg.get("data") or {}
                        self.last_ws_msg = time.time()
                        self.observe(d.get("coin"), (d.get("ctx") or {}).get("oraclePx"))
            except Exception as e:                         # noqa: BLE001 - reconnect on anything
                _log(f"ws error {type(e).__name__}: {e}; reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def _rest_once(self) -> None:
        for dex in sorted({c.split(":")[0] if ":" in c else "" for c in self.by_coin}):
            body = {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}
            req = urllib.request.Request(REST_URL, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            meta, ctxs = json.load(urllib.request.urlopen(req, timeout=5))
            for a, c in zip(meta["universe"], ctxs):
                self.observe(a["name"], c.get("oraclePx"))

    async def rest_loop(self) -> None:
        """Poll every REST_EVERY_S while the socket is quiet; every STALE_POLL_S while it is up but
        some asset has not been heard from (activeAssetCtx only pushes on a change, so a quiet
        stock oracle would otherwise stop getting heartbeats)."""
        last_poll = 0.0
        while True:
            await asyncio.sleep(REST_EVERY_S)
            now = time.time()
            ws_quiet = now - self.last_ws_msg >= REST_AFTER_S
            stale = any(now * 1000 - self.seen_ms.get(a, 0) > REST_AFTER_S * 1000 for a in self.by_coin.values())
            if not ws_quiet and not (stale and now - last_poll >= STALE_POLL_S):
                continue
            last_poll = now
            try:
                await asyncio.to_thread(self._rest_once)
            except Exception as e:                         # noqa: BLE001
                _log(f"rest fallback failed: {type(e).__name__}: {e}")

    async def tick_loop(self) -> None:
        last_status = 0.0
        while True:
            await asyncio.sleep(1.0)
            self.heartbeat()
            try:
                self.seal_due()
            except Exception as e:                         # noqa: BLE001
                _log(f"seal failed: {type(e).__name__}: {e}")
            if time.time() - last_status > 300:
                last_status = time.time()
                fresh = sum(1 for t in self.seen_ms.values() if time.time() * 1000 - t < FRESH_S * 1000)
                _log(f"status: {fresh}/{len(self.by_coin)} assets fresh, {self.rows_written} rows written, "
                     f"ws {'up' if time.time() - self.last_ws_msg < REST_AFTER_S else 'QUIET (rest fallback)'}")

    async def run(self) -> None:
        self.seal_due()
        await asyncio.gather(self.ws_loop(), self.rest_loop(), self.tick_loop())


if __name__ == "__main__":
    _log(f"markets oracle recorder -> {DATA_DIR}")
    asyncio.run(Recorder().run())
