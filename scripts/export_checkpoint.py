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
        {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st,
         "is_copy": int(cp or 0), "plaintext": pt, "commit_block": cb, "round": rnd}
        for ch, hk, t0, st, cp, pt, cb, rnd in con.execute(
            "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext, "
            "commit_block, round FROM signals")
    ]
    meta = {
        hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
        for hk, fs, sk in con.execute(
            "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")
    }
    con.close()

    cp_out = {
        "schema": 1, "netuid": config.NETUID, "network": config.NETWORK,
        "now_unix": now, "generated_at_ms": int(now * 1000),
        "signals": signals, "meta": meta,
    }

    if "--chain" in args:
        try:
            from sn89_signals import chain
            mg = chain.Chain().metagraph()
            cp_out["uid_by_hotkey"] = {hk: i for i, hk in enumerate(mg.hotkeys)}
            cp_out["weights_onchain"] = {
                str(i): float(w) for i, w in enumerate(getattr(mg, "weights_normalized", []))
            } or None
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ chain snapshot skipped: {e}", file=sys.stderr)

    with open(out, "w") as fh:
        json.dump(cp_out, fh)
    dec = sum(1 for s in signals if s["status"] in ("won", "lost"))
    print(f"wrote {out}: {len(signals)} signals ({dec} decisive), {len(meta)} hotkeys "
          f"(now_unix={now:.0f})")


if __name__ == "__main__":
    main()
