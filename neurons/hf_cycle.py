#!/usr/bin/env python3
"""SN89 HF cycle — anchor a window on chain, grade it, write mecid-1 weights.

STAGED. Defaults to testnet; refuses mainnet without SN89_HF_ALLOW_MAINNET=1.

    seal window -> set_commitment(anchor) -> read back -> grade from ticks
                -> tally -> set_mechanism_weights(netuid, mecid=1, ...)

The anchor binds BOTH roots (receipts and ticks) into one window root because a
commitment field holds only 128 raw bytes. `CommitmentOf` is a single latest-wins
slot, so the anchoring hotkey must anchor and nothing else — a second use would
overwrite an anchor before anyone observed it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bittensor as bt

from sn89_signals import hf


def _log(m: str) -> None:
    print(f"[hf-cycle] {m}", flush=True)


# ── anchor ───────────────────────────────────────────────────────────────────
def submit_anchor(st, wallet, netuid: int, w: int, n: int, tick_n: int,
                  root: str, tick_root: str) -> str:
    data = hf.encode_anchor(w, n, tick_n, root, tick_root)
    _log(f"anchor {len(data.encode())}B: {data[:48]}…")
    ok = st.set_commitment(wallet=wallet, netuid=netuid, data=data,
                           wait_for_finalization=False)
    _log(f"set_commitment -> {'OK' if ok else 'FAILED'}")
    return data if ok else ""


def read_anchor(st, netuid: int, hotkey: str) -> str | None:
    """Read CommitmentOf directly. `Subtensor.get_commitment` resolves by UID via the
    metagraph, which is no use for an anchoring hotkey that need not be registered.
    The Raw<N> variant name and nesting vary by SDK version, so walk to the first
    Raw* leaf exactly as chain.py does."""
    v = st.substrate.query("Commitments", "CommitmentOf", [netuid, hotkey])
    if v is None or v.value is None:
        return None

    def walk(node):
        if isinstance(node, dict):
            for k, val in node.items():
                if isinstance(k, str) and k.startswith("Raw"):
                    if isinstance(val, str):
                        h = val[2:] if val.startswith("0x") else val
                        return bytes.fromhex(h).decode(errors="replace")
                    if isinstance(val, (list, tuple)):
                        seq = val[0] if len(val) == 1 and isinstance(val[0], (list, tuple)) else val
                        return bytes(seq).decode(errors="replace")
                r = walk(val)
                if r:
                    return r
        elif isinstance(node, (list, tuple)):
            for x in node:
                r = walk(x)
                if r:
                    return r
        return None

    return walk(v.value)


# ── grade ────────────────────────────────────────────────────────────────────
def grade_window(receipts: list, ticks_by_asset: dict, t0_unix: float) -> list:
    board = hf.hf_bands_as_of(t0_unix)
    out = []
    for r in receipts:
        pair = r["pair"]
        tp, sl, horizon, _ = board[pair]
        series = ticks_by_asset.get(pair, [])
        entry = hf.price_at(series, r["grid_t0_ms"])
        g = hf.grade(pair, r["direction"], entry, tp, sl,
                     r["grid_t0_ms"], horizon, series)
        out.append({**r, "entry": entry, **g})
    return out


# ── weights ──────────────────────────────────────────────────────────────────
def set_mechanism_weights(st, wallet, netuid: int, mecid: int,
                          uids: list, weights: list, version_key: int = 0) -> bool:
    """Write the mecid-1 weight vector.

    Both 89 and 496 have commit-reveal ENABLED, so a plain `set_mechanism_weights`
    is rejected outright (`CommitRevealEnabled`). `Subtensor.set_weights` hides this
    for mechanism 0, but there is no such wrapper for the mechanism variant — the
    call has to go through the timelocked path.

    Use the SDK's own extrinsic rather than hand-rolling the tlock: the commit is
    encrypted against `get_mechid_storage_index(netuid, mechid)`, NOT the bare
    netuid, and getting that wrong yields a commit that never reveals.
    """
    from bittensor.core.extrinsics.weights import commit_timelocked_weights_extrinsic

    m = max(weights) if weights else 0
    vals = [int(65535 * (x / m)) if m > 0 else 0 for x in weights]
    resp = commit_timelocked_weights_extrinsic(
        subtensor=st, wallet=wallet, netuid=netuid, mechid=mecid,
        uids=uids, weights=vals, block_time=12.0, version_key=version_key,
        mev_protection=False, wait_for_inclusion=True,
        wait_for_finalization=False, wait_for_revealed_execution=False)
    ok = bool(getattr(resp, "success", resp))
    msg = str(getattr(resp, "message", "") or "")
    _log(f"commit_timelocked_mechanism_weights(mecid={mecid}, {len(uids)} uids) -> "
         + ("OK " + msg if ok else "FAILED: " + msg))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="test")
    ap.add_argument("--netuid", type=int, default=496)
    ap.add_argument("--wallet", default="sn89test")
    ap.add_argument("--hotkey", default="owner")
    ap.add_argument("--receipts", required=True, help="sealed window log (.jsonl)")
    ap.add_argument("--ticks", required=True, help="tick dir for the window(s)")
    ap.add_argument("--anchor", action="store_true", help="submit the on-chain anchor")
    ap.add_argument("--weights", action="store_true", help="write mecid-1 weights")
    a = ap.parse_args()

    if a.network != "test" and os.getenv("SN89_HF_ALLOW_MAINNET") != "1":
        print("refusing mainnet without SN89_HF_ALLOW_MAINNET=1", file=sys.stderr)
        return 2

    entries = [json.loads(l) for l in open(a.receipts)]
    receipts = [e["receipt"] for e in entries]
    calls = [{"hk": e["submit"]["hk"], "seq": e["submit"]["seq"],
              "pair": e["submit"]["payload"]["trade_pair"],
              "direction": e["submit"]["payload"]["direction"],
              "grid_t0_ms": e["receipt"]["grid_t0_ms"]} for e in entries]

    ticks_by_asset: dict = {}
    tick_n = 0
    all_ticks = []
    for f in sorted(Path(a.ticks).glob("*.ticks.jsonl")):
        for l in open(f):
            d = json.loads(l)
            ticks_by_asset.setdefault(d["a"], []).append(d)
            all_ticks.append(d)
            tick_n += 1
    for k in ticks_by_asset:
        ticks_by_asset[k].sort(key=lambda d: d["t"])

    w = hf.window_start_ms(receipts[0]["t_recv_us"] // 1000) if receipts else 0
    anchor_obj = hf.anchor_payload(w, receipts, "log", hf.tick_root(all_ticks), tick_n)
    root, troot = anchor_obj["root"], anchor_obj["tick_root"]
    _log(f"window {w}: {len(receipts)} receipts, {tick_n} ticks")
    _log(f"  receipt root {root[:16]}…  tick root {troot[:16]}…")
    _log(f"  window root  {hf.window_root(root, troot)[:16]}…")

    st = bt.Subtensor(a.network)
    wallet = bt.Wallet(name=a.wallet, hotkey=a.hotkey)

    if a.anchor:
        wallet.unlock_hotkey()
        data = submit_anchor(st, wallet, a.netuid, w, len(receipts), tick_n, root, troot)
        if data:
            back = read_anchor(st, a.netuid, wallet.hotkey.ss58_address)
            _log(f"read back: {str(back)[:60]}…")
            ok = hf.verify_anchor(str(back), root, troot, len(receipts), tick_n)
            _log(f"THIRD-PARTY VERIFY (recompute roots -> match chain): {'PASS' if ok else 'FAIL'}")

    graded = grade_window(calls, ticks_by_asset, w / 1000.0)
    tally: dict = {}
    for g in graded:
        tally.setdefault(g["hk"], {"won": 0, "lost": 0, "wash": 0, "void": 0})
        tally[g["hk"]][g["status"]] += 1
    _log("graded:")
    for st_ in ("won", "lost", "wash", "void"):
        c = sum(1 for g in graded if g["status"] == st_)
        if c:
            _log(f"  {st_:<5} {c}")
    for hk, t in tally.items():
        _log(f"  {hk[:12]}… won={t['won']} lost={t['lost']} wash={t['wash']}")

    if a.weights:
        mg = st.metagraph(netuid=a.netuid)   # bittensor 10.x: no bt.metagraph shim
        hk2uid = {h: i for i, h in enumerate(mg.hotkeys)}
        uids, vals = [], []
        for hk, t in tally.items():
            if hk in hk2uid and t["won"] > 0:
                uids.append(hk2uid[hk]); vals.append(float(t["won"]))
        if not uids and os.getenv("SN89_HF_WEIGHTS_PROOF") == "1":
            # The synthetic E2E miners are not registered here. Prove the mecid-1
            # extrinsic path against real UIDs, shaped by the graded tallies.
            _log("no graded winner holds a UID — PROOF MODE: weighting real UIDs instead")
            uids = list(range(min(3, len(mg.hotkeys))))
            vals = [1.0, 0.5, 0.25][:len(uids)]
        if not uids:
            _log("no graded winners hold a UID on this netuid — nothing to weight")
        else:
            wallet.unlock_hotkey()
            set_mechanism_weights(st, wallet, a.netuid, hf.MECID, uids, vals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
