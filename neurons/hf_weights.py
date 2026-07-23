#!/usr/bin/env python3
"""SN89 HF weight setter (mecid 1). Grades resolved HF calls, applies the gated
HF scoring, and commits the mecid-1 timelocked weight vector. VALIDATOR ONLY.

Pipeline each cycle:
  1. read every accepted call from the published receipt logs
  2. grade each call whose horizon has elapsed, against the anchored tick series
     (first touch wins; unresolved at the horizon = wash), cache the outcome
  3. build HF-only (hotkey -> decisive) history + first-seen (HF warmup clock)
  4. hf.hf_compute_weights -> {uid: weight}  (same gate as mecid 0, HF constants)
  5. commit_timelocked_mechanism_weights(mecid=1) from the validator hotkey

Until a miner clears the 8-decisive gate, step 4 returns all-burn, which the first
commit writes over the stale mecid-1 rows left by the subnet's previous second
mechanism. So running this even at 0% emission split cleans up and stays correct.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bittensor as bt

from sn89_signals import hf

LOG_DIR = Path(os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf"))
TICK_DIR = Path(os.getenv("SN89_HF_TICK_DIR", str(LOG_DIR / "ticks")))
GRADE_DB = os.getenv("SN89_HF_GRADE_DB", str(LOG_DIR / "hf_grades.db"))
NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
WALLET = os.getenv("SN89_HF_WEIGHT_WALLET", "chef")
HOTKEY = os.getenv("SN89_HF_WEIGHT_HOTKEY", "default")
CYCLE_S = int(os.getenv("SN89_HF_WEIGHT_CYCLE_S", "600"))
COMMIT_EVERY_S = int(os.getenv("SN89_HF_WEIGHT_COMMIT_EVERY_S", "4200"))  # ~tempo
ENABLED = os.getenv("SN89_HF_WEIGHTS_ENABLED") == "1"
WINDOW_MS = 180_000                                   # tick seal window


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-weights] {m}", flush=True)


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(GRADE_DB, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS grades ("
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, "
              "status TEXT, graded_at REAL)")
    return c


# ── tick loading ─────────────────────────────────────────────────────────────
_tick_cache: dict = {}


def _load_ticks(pair: str, t0_ms: int, end_ms: int) -> list:
    """All ticks for `pair` in [t0_ms, end_ms], from the sealed tick windows."""
    out = []
    w = (t0_ms // WINDOW_MS) * WINDOW_MS
    while w <= end_ms:
        f = TICK_DIR / f"{w}.ticks.jsonl"
        if w not in _tick_cache:
            rows = []
            if f.exists():
                for line in f.open():
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    rows.append(d)
            _tick_cache[w] = rows
        for d in _tick_cache[w]:
            if d.get("a") == pair and t0_ms <= int(d["t"]) <= end_ms:
                out.append(d)
        w += WINDOW_MS
    out.sort(key=lambda d: int(d["t"]))
    return out


# ── grading ──────────────────────────────────────────────────────────────────
def grade_new_calls(now_ms: int) -> int:
    db = _db()
    done = {r[0] for r in db.execute("SELECT key FROM grades")}
    graded = 0
    for logf in sorted(LOG_DIR.glob("*.jsonl")):
        if not logf.stem.isdigit():
            continue
        for line in logf.open():
            try:
                e = json.loads(line)
            except Exception:
                continue
            sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
            hk, seq = sub.get("hk"), sub.get("seq")
            key = f"{hk}:{seq}"
            if not hk or key in done:
                continue
            p = sub.get("payload") or {}
            pair, direction = p.get("trade_pair"), p.get("direction")
            t0_ms = rcpt.get("grid_t0_ms")
            board = hf.hf_bands_as_of(t0_ms / 1000.0) if t0_ms else None
            if not board or pair not in board:
                continue
            tp, sl, horizon_s, _ = board[pair]
            end_ms = int(t0_ms) + horizon_s * 1000
            if now_ms < end_ms:
                continue                                  # not resolved yet
            ticks = _load_ticks(pair, int(t0_ms), end_ms)
            entry = hf.price_at(ticks, int(t0_ms))
            g = hf.grade(pair, direction, entry, tp, sl, int(t0_ms), horizon_s, ticks)
            db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?)",
                       (key, hk, int(t0_ms), pair, g["status"], time.time()))
            done.add(key)
            graded += 1
    db.commit()
    db.close()
    return graded


# ── scoring inputs from the grade cache ──────────────────────────────────────
def build_history() -> tuple[dict, dict]:
    db = _db()
    decisive_by_hk: dict = {}
    first_seen: dict = {}
    for hk, t0_ms, status in db.execute("SELECT hk, t0_ms, status FROM grades"):
        t0 = t0_ms / 1000.0
        first_seen[hk] = min(first_seen.get(hk, t0), t0)
        if status in ("won", "lost"):
            decisive_by_hk.setdefault(hk, []).append((t0, status == "won", False))
    db.close()
    for v in decisive_by_hk.values():
        v.sort(key=lambda x: x[0])
    return decisive_by_hk, first_seen


# ── weight commit ────────────────────────────────────────────────────────────
def commit_weights(sub, wallet, weights: dict) -> bool:
    from bittensor.core.extrinsics.weights import commit_timelocked_weights_extrinsic
    if not weights:
        return False
    m = max(weights.values()) or 1.0
    uids = list(weights.keys())
    vals = [int(65535 * (weights[u] / m)) for u in uids]
    resp = commit_timelocked_weights_extrinsic(
        subtensor=sub, wallet=wallet, netuid=NETUID, mechid=hf.MECID_1,
        uids=uids, weights=vals, block_time=12.0, mev_protection=False,
        wait_for_inclusion=True, wait_for_finalization=False,
        wait_for_revealed_execution=False)
    ok = bool(getattr(resp, "success", resp))
    _log(f"commit mecid-1 ({len(uids)} uids) -> "
         + ("OK" if ok else f"FAIL {getattr(resp, 'message', '')}"))
    return ok


def main() -> int:
    if not ENABLED:
        print("SN89_HF_WEIGHTS_ENABLED != 1 — staged, refusing to run", file=sys.stderr)
        return 2
    sub = bt.Subtensor(NETWORK)
    wallet = bt.Wallet(name=WALLET, hotkey=HOTKEY)
    wallet.unlock_hotkey()
    _log(f"weight hotkey {wallet.hotkey.ss58_address} · netuid {NETUID} mecid {hf.MECID_1}")
    last_commit = 0.0
    while True:
        try:
            now_ms = int(time.time() * 1000)
            n = grade_new_calls(now_ms)
            dec, fs = build_history()
            mg = sub.metagraph(netuid=NETUID)
            uid_by_hk = {h: i for i, h in enumerate(mg.hotkeys)}
            weights = hf.hf_compute_weights(dec, fs, uid_by_hk, time.time())
            earners = {u: w for u, w in weights.items() if u != 0 and w > 0}
            _log(f"graded +{n} · {len(dec)} miners w/ decisive · "
                 f"{len(earners)} earning · burn={weights.get(0, 0.0):.3f}")
            if time.time() - last_commit >= COMMIT_EVERY_S:
                if commit_weights(sub, wallet, weights):
                    last_commit = time.time()
        except Exception as e:                            # noqa: BLE001
            _log(f"cycle error: {e}")
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    raise SystemExit(main())
