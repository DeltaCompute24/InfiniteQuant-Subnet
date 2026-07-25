#!/usr/bin/env python3
"""SN89 HF tick recorder — the published, anchored price series for mechanism 1.

STAGED. Read-only against the tick bus; writes only its own window directory.

Why this exists: `iq-signals-mark-recorder` samples the bus at IQ_MARKS_SAMPLE_MS=2000,
so `signals-marks.db` has a 2005 ms median gap and 0 % of gaps under 250 ms — far too
coarse to grade a 30 min / 12 bps call. The FEED is not the limit: `iq-polygon-tick-bus`
is WebSocket-primary (crypto XT.* trades, forex C.* quotes). This recorder simply stops
discarding the resolution, by polling fast and deduping on the feed's OWN src_ts.

Dedupe-on-src_ts is the load-bearing detail. It means the published series carries
exactly the resolution the feed actually provides and never invents sub-second
structure: Polygon's forex/metals quotes timestamp to the second, so gold lands on a
1 s grid honestly rather than being smeared onto a 250 ms one by our receive clock.

Each window is `hf.ANCHOR_WINDOW_S` long — 180 s, not the 60 s this docstring
claimed until 2026-07-25. Read the constant, never this line. Each window produces:
    <dir>/<w>.ticks.jsonl   one line per (asset, src_ts), ordered by (t, asset)
    <dir>/<w>.ticks.json    {w, n, tick_root, sig} — signed, and the tick_root goes
                            into the window's on-chain anchor alongside the receipt
                            root, so the prices we grade against are as immutable as
                            the calls themselves.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import hf

BUS = os.getenv("IQ_TICKBUS_URL", "http://127.0.0.1:18774")
OUT_DIR = Path(os.getenv("SN89_HF_TICK_DIR", "/var/lib/sn89-hf/ticks"))
POLL_MS = int(os.getenv("SN89_HF_TICK_POLL_MS", "50"))
# Cover BOTH boards. Once mechanism 0 moves to touch-on-ticks it grades off this
# same series, so a pair missing here is a pair that cannot be graded. The four
# crosses added by fxexpand17 (AUDNZD/GBPCAD/NZDCHF/NZDJPY) had NO tick coverage
# at all until 2026-07-23 — absent from the tick-bus seed list while being graded.
LF_BOARD = ["AUDNZD", "AUDUSD", "BTCUSD", "ETHUSD", "EURUSD", "GBPCAD", "GBPUSD",
            "NZDCHF", "NZDJPY", "NZDUSD", "SOLUSD", "USDCAD", "USDCHF", "USDJPY",
            "XAGUSD", "XAUUSD", "XRPUSD"]
ASSETS = [a.strip().upper() for a in os.getenv(
    "SN89_HF_TICK_ASSETS",
    ",".join(sorted(set(hf.HF_BOARD_V1) | set(LF_BOARD)))).split(",") if a.strip()]
STALE_MS = int(os.getenv("SN89_HF_TICK_STALE_MS", "30000"))
# A tick is filed under ITS OWN src_ts, so one whose timestamp predates the current
# window can still arrive after that window ended (feed lag). Sealing on the boundary
# therefore sealed a window, took a late tick, and sealed it AGAIN with a different
# tick_root — two roots for one window, which would make the series unreplayable.
# Wait this long past a window's end before sealing, then refuse to reopen it.
SEAL_GRACE_MS = int(os.getenv("SN89_HF_TICK_SEAL_GRACE_MS", "5000"))


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-ticks] {m}", flush=True)


class TickRecorder:
    def __init__(self, sign_kp=None, out_dir: Path = OUT_DIR):
        self.kp = sign_kp
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.last_src: dict[str, int] = {}       # asset -> last src_ts kept
        self.windows: dict[int, list] = {}       # window start ms -> ticks
        self.sealed: set = set()                 # windows already committed - never reopen
        self.polls = 0
        self.kept = 0
        self.late = 0

    def poll_once(self) -> int:
        try:
            with urllib.request.urlopen(f"{BUS}/ticks", timeout=2) as r:
                ticks = json.loads(r.read()).get("ticks", {})
        except Exception:
            return 0
        self.polls += 1
        n = 0
        for a in ASSETS:
            d = ticks.get(a)
            if not d:
                continue
            src = int(d.get("ts") or 0)
            price = d.get("price")
            if not src or price is None:
                continue
            if int(d.get("age_ms") or 0) > STALE_MS or d.get("stale"):
                continue
            if src <= self.last_src.get(a, 0):      # same source tick — not new data
                continue
            w = hf.window_start_ms(src)
            if w in self.sealed:                    # arrived after its window closed
                self.late += 1
                continue
            self.last_src[a] = src
            rec = {"a": a, "t": src, "p": float(price)}
            if d.get("bid") is not None:
                rec["b"] = float(d["bid"])
            if d.get("ask") is not None:
                rec["k"] = float(d["ask"])
            self.windows.setdefault(w, []).append(rec)
            n += 1
            self.kept += 1
        return n

    def seal(self, w: int) -> dict:
        if w in self.sealed:
            raise RuntimeError(f"window {w} already sealed — refusing to re-root it")
        self.sealed.add(w)
        ticks = sorted(self.windows.pop(w, []), key=hf.tick_order_key)
        with open(self.out / f"{w}.ticks.jsonl", "w") as f:
            for t in ticks:
                f.write(json.dumps(t, sort_keys=True, separators=(",", ":")) + "\n")
        root = hf.tick_root(ticks)
        meta = {"v": 1, "t": "hf-ticks", "w": int(w), "n": len(ticks), "tick_root": root}
        if self.kp is not None:
            payload = hf.canonical_json(meta).encode()
            meta["sig"] = self.kp.sign(payload).hex()
            meta["signer"] = self.kp.ss58_address
        (self.out / f"{w}.ticks.json").write_text(json.dumps(meta))
        per_asset = {}
        for t in ticks:
            per_asset[t["a"]] = per_asset.get(t["a"], 0) + 1
        _log(f"window {w} · {len(ticks)} ticks · root {root[:16]}… · "
             + " ".join(f"{a}:{c}" for a, c in sorted(per_asset.items())))
        return meta

    def run(self, seconds: int | None = None):
        _log(f"bus={BUS} poll={POLL_MS}ms assets={','.join(ASSETS)} out={self.out}")
        t_end = None if seconds is None else time.time() + seconds
        while t_end is None or time.time() < t_end:
            self.poll_once()
            now_ms = int(time.time() * 1000)
            ready = now_ms - SEAL_GRACE_MS
            for w in sorted([x for x in self.windows
                             if x + hf.ANCHOR_WINDOW_S * 1000 <= ready]):
                self.seal(w)
            time.sleep(POLL_MS / 1000.0)
        for w in sorted(self.windows):            # flush on a bounded probe run
            self.seal(w)
        _log(f"polls={self.polls} ticks_kept={self.kept} late_dropped={self.late}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=None,
                    help="bounded probe run instead of a daemon")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    kp = None
    sk = os.getenv("SN89_HF_TICK_SK", "").strip()
    if sk:
        from bittensor_wallet import Keypair
        kp = Keypair.create_from_uri(sk) if sk.startswith("//") else Keypair.create_from_seed(sk)
        _log(f"signing key {kp.ss58_address}")
    else:
        _log("no SN89_HF_TICK_SK — windows will be unsigned (probe mode)")
    TickRecorder(kp, Path(a.out) if a.out else OUT_DIR).run(a.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
