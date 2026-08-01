#!/usr/bin/env python3
"""Rebuild the unified multi-competition weight vector from public inputs —
the combined-era analogue of replay.weights_from_journal, for auditors.

    LF      — the validator journal (on-chain commitments + drand + price),
              exactly replay.weights_from_journal
    HF      — the published, Merkle-anchored HF windows (hf_grade.mecid1_weights)
    Closers — the same published windows' kind=closers receipts + tick feed
              (closers.closers_weights)
    blend   — competitions.combine at config.comp_weights_as_of(now)

Prints each competition's earner set, the blended vector, and (with --compare)
the delta against the weights currently on chain for the signer UID. Any
nonzero delta beyond float noise means the validator is not computing what the
public record says it should — which is the entire point of publishing it.

Usage (run against a live validator's journal, or a rebuilt one):
  .venv/bin/python tools/replay_combined.py                  # env-configured net
  .venv/bin/python tools/replay_combined.py --compare        # + on-chain diff
  SN89_NETUID=496 SN89_NETWORK=test ... --db /root/.sn89/validator.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import (closers, competitions, config, hf,  # noqa: E402
                          hf_grade, replay)


def lf_vector(db_path: str, uid_by_hotkey: dict, now: float) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    sig_rows = [
        {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st,
         "is_copy": int(cp or 0), "plaintext": pt}
        for ch, hk, t0, st, cp, pt in db.execute(
            "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext "
            "FROM signals")]
    meta = {hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
            for hk, fs, sk in db.execute(
                "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")}
    referral_rows = [
        {"recruiter_hk": r, "recruit_hk": c, "commit_block": cb,
         "recruit_reg_block": rb}
        for r, c, cb, rb in db.execute(
            "SELECT recruiter_hk, recruit_hk, commit_block, recruit_reg_block "
            "FROM referrals")]
    db.close()
    return replay.weights_from_journal(sig_rows, meta, uid_by_hotkey, now,
                                       referrals=referral_rows)


def fmt(vec: dict, burn: int) -> str:
    earners = {u: w for u, w in sorted(vec.items()) if u != burn and w > 0}
    return (f"burn={vec.get(burn, 0.0):.4f} · "
            + " ".join(f"uid{u}={w:.4f}" for u, w in earners.items()) or "∅")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH,
                    help="validator journal (LF competition input)")
    ap.add_argument("--now", type=float, default=None)
    ap.add_argument("--compare", action="store_true",
                    help="diff the blend against on-chain weights")
    a = ap.parse_args()
    now = a.now or time.time()

    import bittensor as bt
    st = bt.Subtensor(config.NETWORK)
    mg = st.metagraph(config.NETUID)
    uid_by_hotkey = {h: i for i, h in enumerate(mg.hotkeys)}
    print(f"netuid {config.NETUID} · {config.NETWORK} · {len(mg.hotkeys)} uids "
          f"· now={now:.0f}")

    shares = config.comp_weights_as_of(now)
    print(f"shares as-of now: {shares}")

    vectors: dict = {}
    try:
        vectors["lf"] = lf_vector(os.path.expanduser(a.db), uid_by_hotkey, now)
    except Exception as e:  # noqa: BLE001
        print(f"lf: FAILED ({e}) — share burns")
        vectors["lf"] = None
    try:
        vectors["hf"] = hf_grade.mecid1_weights(uid_by_hotkey, now)
    except Exception as e:  # noqa: BLE001
        print(f"hf: FAILED ({e}) — share burns")
        vectors["hf"] = None
    try:
        hk_by_uid = {u: h for h, u in uid_by_hotkey.items()}
        qual = {hk_by_uid[u]
                for vec in (vectors.get("lf") or {}, vectors.get("hf") or {})
                for u, wt in vec.items()
                if wt > 0 and u != config.BURN_UID and u in hk_by_uid}
        vectors["closers"] = closers.closers_weights(uid_by_hotkey, now,
                                                     qualified_hks=qual)
    except Exception as e:  # noqa: BLE001
        print(f"closers: FAILED ({e}) — share burns")
        vectors["closers"] = None

    for key in shares:
        v = vectors.get(key)
        print(f"  {key:8} {'(unavailable)' if v is None else fmt(v, config.BURN_UID)}")

    blend = competitions.combine(vectors, shares)
    print(f"\nBLEND    {fmt(blend, config.BURN_UID)}")

    if a.compare:
        # On-chain weights for the highest-stake validator uid (the signer),
        # straight from Weights storage — the metagraph's W is empty on a lite
        # sync, and under commit-reveal this storage holds the last REVEALED
        # vector (so a just-committed epoch legitimately lags one reveal).
        vuid = max(range(len(mg.hotkeys)), key=lambda i: float(mg.S[i]))
        try:
            raw = st.substrate.query("SubtensorModule", "Weights",
                                     [config.NETUID, vuid]).value or []
            onchain = {int(u): float(w) for u, w in raw if w > 0}
        except Exception as e:  # noqa: BLE001
            print(f"on-chain read failed: {e}")
            return
        total = sum(onchain.values()) or 1.0
        onchain = {u: w / total for u, w in onchain.items()}
        print(f"ONCHAIN  (uid {vuid}) {fmt(onchain, config.BURN_UID)}")
        keys = set(blend) | set(onchain)
        worst = max((abs(blend.get(u, 0) - onchain.get(u, 0)) for u in keys),
                    default=0.0)
        print(f"max per-uid delta: {worst:.5f}"
              + ("  ✔ replay parity" if worst < 0.01 else
                 "  ⚠ divergent — EITHER the last on-chain reveal predates "
                 "recent grades (re-run after the next reveal; earners only "
                 "ever appearing in the rebuild is the tell) OR the validator "
                 "is not computing the public record. Persistent divergence "
                 "across two reveals is the second one — investigate."))


if __name__ == "__main__":
    main()
