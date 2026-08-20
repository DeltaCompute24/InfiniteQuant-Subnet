#!/usr/bin/env python3
"""Publish the single validator's journal as a verifiable checkpoint.

The authoritative validator runs this (e.g. each weight cycle) and serves the
output at a public URL. Anyone can then run audit_journal.py to re-derive the
weight vector from it and confirm it was computed honestly — see
docs/single-validator-model.md. READ-ONLY on the validator DB.

    python3 export_checkpoint.py [out.json] [--db PATH] [--chain]

--chain adds the metagraph uid map + the on-chain weights snapshot (so the audit
can run fully offline); without it the auditor reads those from chain itself.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config  # noqa: E402

DEFAULT_OUT = "/tmp/sn89_checkpoint.json"


def main():
    args = sys.argv[1:]
    out = next((a for a in args if not a.startswith("-")), DEFAULT_OUT)
    db_path = args[args.index("--db") + 1] if "--db" in args else config.DB_PATH
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    now = time.time()

    signals = [
        # exit_at_ms is WHEN the status became final, and without it the journal
        # cannot be replayed as of any past instant: statuses MUTATE as calls
        # resolve, so replaying an old `now` against today's rows counts wins
        # that had not happened yet. Six such rows were enough to shift the
        # normalisation of a whole commit by 11% (2026-08-19).
        {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st,
         "is_copy": int(cp or 0), "plaintext": pt, "commit_block": cb,
         "round": rnd, "exit_at_ms": ex, "t0_ms": tms, "void_reason": vr}
        # t0_ms is the ms-precise T0 from the commit block's Timestamp pallet and
        # is what board-resolution and entry pricing key on; t0_unix alone loses
        # that precision. void_reason travels with a void so an importer can heal
        # one row without inventing a reason for another.
        for ch, hk, t0, st, cp, pt, cb, rnd, ex, tms, vr in con.execute(
            "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext, "
            "commit_block, round, exit_at_ms, t0_ms, void_reason FROM signals")
    ]
    meta = {
        hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
        for hk, fs, sk in con.execute(
            "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")
    }
    # Referral claims (§ referral) — raw journaled facts; the auditor re-derives
    # validity. Old auditors ignore unknown keys, so exporting these is fully
    # backward-compatible while REFERRAL_ENABLED=0 (the dark-ship stage).
    try:
        referrals = [
            {"recruiter_hk": r, "recruit_hk": c, "commit_block": cb,
             "recruit_reg_block": rb}
            for r, c, cb, rb in con.execute(
                "SELECT recruiter_hk, recruit_hk, commit_block, recruit_reg_block "
                "FROM referrals")
        ]
    except sqlite3.OperationalError:   # DB predates the referrals table
        referrals = []
    # The (now, vector) the validator ACTUALLY committed from, newest first.
    #
    # `now_unix` above is EXPORT time, not the instant any committed vector was
    # computed, so replaying at it reproduces the committed weights only by
    # luck. replay is bit-for-bit deterministic in (journal, now), so handing
    # the auditor the real `now` turns the comparison from a race against the
    # 72-minute tempo into an exact, repeatable check. Missing on a validator
    # that has not yet written the table -- an auditor must treat absence as
    # "cannot pin the instant", never as an empty list of commits.
    try:
        commits = [
            {"block": b, "now_unix": nu, "combined": bool(cmb),
             "weights": json.loads(vec)}
            for b, nu, vec, cmb in con.execute(
                "SELECT block, now_unix, vector, combined FROM weight_commits "
                "ORDER BY block DESC LIMIT 20")]
    except sqlite3.OperationalError:  # validator predates weight_commits
        commits = None
    con.close()


    cp_out = {
        "schema": 1, "netuid": config.NETUID, "network": config.NETWORK,
        "now_unix": now, "generated_at_ms": int(now * 1000),
        "signals": signals, "meta": meta, "referrals": referrals,
        "weight_commits": commits,
    }

    if "--chain" in args:
        try:
            from sn89_signals import chain
            ch = chain.Chain()
            mg = ch.metagraph()
            cp_out["uid_by_hotkey"] = {hk: i for i, hk in enumerate(mg.hotkeys)}
            # snapshot THIS validator's actual on-chain weight vector (read from the
            # Weights storage — the metagraph doesn't carry weights by default).
            vhk = (args[args.index("--validator-hotkey") + 1]
                   if "--validator-hotkey" in args else os.getenv("SN89_VALIDATOR_HOTKEY"))
            if vhk:
                vuid = ch.uid_of(vhk, mg=mg)
                cp_out["validator_uid"] = vuid
                w = ch.weights_for_uid(vuid) if vuid is not None else None
                if w:
                    cp_out["weights_onchain"] = {str(u): wt for u, wt in w.items()}
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ chain snapshot skipped: {e}", file=sys.stderr)

    with open(out, "w") as fh:
        json.dump(cp_out, fh)
    dec = sum(1 for s in signals if s["status"] in ("won", "lost"))
    print(f"wrote {out}: {len(signals)} signals ({dec} decisive), {len(meta)} hotkeys, "
          f"{len(referrals)} referrals (now_unix={now:.0f})")


if __name__ == "__main__":
    main()
