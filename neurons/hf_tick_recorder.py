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

# ── GRADED CRYPTO SERIES: Hyperliquid (cutover 2026-08-20, Whit) ─────────────
# The bus serves two crypto series. Polygon's XT is an AGGREGATE of every venue,
# published as best-bid-across-venues vs best-ask-across-venues, so it prints
# spreads that are not tradeable at any single book. Hyperliquid is ONE real
# book, and it is where PTN/Vanta actually scores crypto fills — so grading
# there removes the basis between the line we grade on and the line a miner's
# signal is executed against.
#
# The parameter reaches ONLY crypto: the bus falls through to Polygon for FX and
# metals, which are not on Hyperliquid at all. So this one flag gives
# HL-for-crypto and Polygon-for-everything-else with no per-asset branching here.
#
# ⚠ This was NOT safe until the same day. The HL leg subscribed to l2Book only —
# a ~5s book-snapshot cadence giving 18 ticks/90s for EVERY pair against
# Polygon's 315 on BTCUSD — and its `price` was the book MID, which a wick can
# trade through without ever moving to. Grading is touch_ticks and needs
# MIN_TOUCH_TICKS distinct ticks to TOUCH a level, so both would have pushed
# outcomes toward WASH. The bus now also subscribes to HL `trades`: BTCUSD
# 18 -> 152, ETHUSD 18 -> 127, HYPEUSD 18 -> 185 per 90s.
#
# Replay stays correct across the boundary WITHOUT an as-of constant, because
# the tick series itself is published and Merkle-anchored per window: a replayer
# grades off the ticks that were anchored at the time, whatever venue produced
# them. The `s` field below makes each window say which venue that was.
TICK_SOURCE = os.getenv("SN89_TICK_SOURCE", "hyperliquid")
TICK_SOURCE_Q = f"?source={TICK_SOURCE}" if TICK_SOURCE else ""
OUT_DIR = Path(os.getenv("SN89_HF_TICK_DIR", "/var/lib/sn89-hf/ticks"))
POLL_MS = int(os.getenv("SN89_HF_TICK_POLL_MS", "50"))

# ── live tail ────────────────────────────────────────────────────────────────
# Ticks reach disk only when their window seals, which is 5s after a 180s window
# closes -- so the series is 5-185s behind by construction. That is right for the
# ANCHORED artifact: the tick_root is taken over the whole window and cannot be
# written incrementally. It is not a property of the data, which we hold the
# instant the bus returns it.
#
# The same additive row goes here as it arrives, so anything that needs to reason
# about price NOW reads live-tail-then-sealed and gets the identical series a few
# minutes earlier. Measured 2026-08-25: re-deciding twelve calls the fast verdict
# had got backwards off signals-marks.db (a ~2.3s downsample) against THIS series
# corrected 12 of 12 -- the resolution is the whole difference, so a consumer that
# wants to agree with the validator has to read these ticks and not the marks.
#
# UNANCHORED, and a SUBDIR on purpose: every consumer of the sealed series globs
# <dir>/*.ticks.jsonl non-recursively, so this cannot leak into a grade, a
# tick_root or a replay. Flattening it into the window dir would do exactly that.
LIVE_DIR = Path(os.getenv("SN89_HF_TICK_LIVE_DIR", str(OUT_DIR / "live")))
LIVE_RETAIN_S = int(os.getenv("SN89_HF_TICK_LIVE_RETAIN_S", str(6 * 3600)))
# Cover BOTH boards. Once mechanism 0 moves to touch-on-ticks it grades off this
# same series, so a pair missing here is a pair that cannot be graded. The four
# crosses added by fxexpand17 (AUDNZD/GBPCAD/NZDCHF/NZDJPY) had NO tick coverage
# at all until 2026-07-23 — absent from the tick-bus seed list while being graded.
# Last-resort list, used ONLY if the board cannot be read. A recorder that starts with
# no assets is worse than one with a stale list. Do not maintain this by hand — the
# live set comes from the board below.
LF_BOARD_FALLBACK = ["AUDNZD", "AUDUSD", "BTCUSD", "ETHUSD", "EURUSD", "GBPCAD",
                     "GBPUSD", "NZDCHF", "NZDJPY", "NZDUSD", "SOLUSD", "TAOUSD",
                     "USDCAD", "USDCHF", "USDJPY", "XAGUSD", "XAUUSD", "XRPUSD"]


def _lf_board() -> list:
    """The LIVE board, not a copy of it.

    A hardcoded list here is a second source of truth for "what is on the board", and it
    has drifted twice: the fxexpand17 crosses went untracked 2026-07-21 → 07-23, and
    TAOUSD was listed on 2026-08-11 with no tick coverage at all. Both are invisible —
    the pair is fully live everywhere a human looks and simply cannot be graded.

    Union with the fallback on purpose: a pair dropped from the board keeps recording, so
    in-flight calls that still resolve to it through bands_as_of can finish grading.
    """
    try:
        from sn89_signals import config
        live = list((config.load_bands() or {}).get("bands", {}).keys())
        if live:
            return sorted(set(live) | set(LF_BOARD_FALLBACK))
        print("[hf-ticks] WARNING board read returned no pairs — using fallback list",
              flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[hf-ticks] WARNING board unreadable ({e}) — using fallback list",
              flush=True)
    return sorted(LF_BOARD_FALLBACK)


LF_BOARD = _lf_board()
ASSETS = [a.strip().upper() for a in os.getenv(
    "SN89_HF_TICK_ASSETS",
    ",".join(sorted(set(hf.HF_BOARD_V1) | set(LF_BOARD)))).split(",") if a.strip()]
# ADDITIVE, unlike SN89_HF_TICK_ASSETS which replaces the list above. Use this to record a
# CANDIDATE pair before it is listed: the HF gate needs its typical spread measured on this
# very corpus, so a pair that is not recording cannot be measured and cannot be listed --
# and post-2026-08-11 the list derives from the board, which makes that circular.
# Recording an unlisted pair grades nothing and gates nothing; it only produces evidence.
ASSETS = sorted(set(ASSETS) | {a.strip().upper() for a in
                               os.getenv("SN89_HF_TICK_EXTRA", "").split(",")
                               if a.strip()})
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
        self.live = LIVE_DIR
        self._live_pruned = 0.0

    def poll_once(self) -> int:
        try:
            with urllib.request.urlopen(f"{BUS}/ticks{TICK_SOURCE_Q}", timeout=2) as r:
                ticks = json.loads(r.read()).get("ticks", {})
        except Exception:
            return 0
        self.polls += 1
        n = 0
        fresh: list = []
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
            # `s` records WHICH venue produced this print. hf.tick_bytes hashes
            # only ("a","t","p"), so this cannot move tick_root or any anchor —
            # it is pure provenance. Without it the published history gives a
            # replayer no way to tell a Polygon window from a Hyperliquid one
            # across the 2026-08-20 cutover, which is exactly the question they
            # would need to answer to reproduce a grade near the boundary.
            rec = {"a": a, "t": src, "p": float(price)}
            if d.get("source"):
                rec["s"] = str(d["source"])
            if d.get("bid") is not None:
                rec["b"] = float(d["bid"])
            if d.get("ask") is not None:
                rec["k"] = float(d["ask"])
            self.windows.setdefault(w, []).append(rec)
            fresh.append((w, rec))
            n += 1
            self.kept += 1
        self._write_live(fresh)
        return n

    def _write_live(self, fresh) -> None:
        """Append this poll's new ticks to the live tail. Never raises.

        One buffered write per POLL, not per tick: at 50 ms and ~39 assets that is
        20 writes a second instead of hundreds, and the ordering inside a poll is
        already the ordering the sealed file will carry.

        A failure here must never cost a tick from the anchored window -- that is
        the record, this is a convenience -- so it is caught and logged, exactly
        as hf_ingest.record_live does on the submission side.
        """
        if not fresh:
            return
        try:
            self.live.mkdir(parents=True, exist_ok=True)
            by_w: dict = {}
            for w, rec in fresh:
                by_w.setdefault(w, []).append(rec)
            for w, recs in by_w.items():
                with open(self.live / f"{w}.ticks.jsonl", "a") as f:
                    for rec in recs:
                        # sort_keys mirrors seal() exactly, so a sealed window and
                        # its live tail are BYTE-IDENTICAL line for line and the
                        # parity check is a diff rather than a judgement call.
                        f.write(json.dumps(rec, sort_keys=True,
                                           separators=(",", ":")) + "\n")
            now = time.time()
            if now - self._live_pruned > 600:
                self._live_pruned = now
                cut = int((now - LIVE_RETAIN_S) * 1000)
                for p in self.live.glob("*.ticks.jsonl"):
                    if p.stem.split(".")[0].isdigit() and int(p.stem.split(".")[0]) < cut:
                        p.unlink()
        except Exception as e:                                   # noqa: BLE001
            _log(f"live tick tail write FAILED: {e}")

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
