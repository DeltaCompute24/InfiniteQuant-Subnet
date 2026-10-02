#!/usr/bin/env python3
"""Attest a referrer succession (§ referrer succession): old recruiter hotkey →
the same recruiter's new hotkey, signed by config.SUCCESSION_ATTESTOR_HK (chef).

Two modes:

  --from OLD --to NEW [--wait 180]
      One attestation now. Refuses unless NEW holds a UID on the subnet.

  --queue PATH
      One step of a queue drain, safe to call every minute (the selffund
      watcher does). PATH is JSONL of {"from","to"}; progress lives in
      PATH + ".state.json". CommitmentOf is ONE latest-wins slot per hotkey, so
      at most one attestation is in flight: it is committed, then marked done
      only once the validator journal shows it, and re-committed if it is not
      journaled within RECOMMIT_S. The next item waits until then.

Runs as root on iq-main (chef's wallet and the validator journal are root's).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import chain, config  # noqa: E402

RECOMMIT_S = 600


def journaled_block(db_path: str, frm: str, to: str) -> int | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT commit_block FROM referrer_successions "
                            "WHERE from_hk=? AND to_hk=?", (frm, to)).fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None                      # validator not on the succession build yet
    return int(r[0]) if r else None


def _wallet(name: str, hotkey: str):
    import bittensor as bt
    w = bt.Wallet(name=name, hotkey=hotkey)
    if w.hotkey.ss58_address != config.SUCCESSION_ATTESTOR_HK:
        raise SystemExit(f"wallet {name}/{hotkey} is {w.hotkey.ss58_address}, "
                         f"not the attestor {config.SUCCESSION_ATTESTOR_HK}")
    return w


def commit(ch, w, frm: str, to: str) -> bool:
    if frm == to:
        print(f"  ✗ {frm[:8]}…: self-succession refused")
        return False
    if to not in set(ch.metagraph().hotkeys):
        print(f"  ✗ {to[:8]}… holds no UID on {config.NETUID} — not attesting")
        return False
    resp = ch.commit_referrer_succession(w, frm, to)
    # SDK 10 returns an ExtrinsicResponse (truthy object); older SDKs a bool.
    ok = bool(getattr(resp, "success", resp))
    msg = getattr(resp, "message", "") or ""
    print(f"  {'⇢' if ok else '✗'} sn89refs {frm[:8]}… → {to[:8]}… submitted={ok}"
          + (f" ({str(msg)[:120]})" if msg and not ok else ""))
    return ok


def drain(args, ch, w) -> int:
    items = []
    try:
        with open(args.queue) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    if d.get("from") and d.get("to"):
                        items.append((d["from"], d["to"]))
    except FileNotFoundError:
        return 0
    state_path = args.queue + ".state.json"
    try:
        state = json.load(open(state_path))
    except (FileNotFoundError, ValueError):
        state = {}

    def save():
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, state_path)

    for frm, to in items:
        key = f"{frm}:{to}"
        st = state.get(key) or {}
        if st.get("status") in ("done", "refused"):
            continue
        jb = journaled_block(args.db, frm, to)
        if st.get("status") == "committed":
            if jb is not None and jb >= int(st.get("not_before_block", 0)):
                state[key] = {**st, "status": "done", "journaled_block": jb}
                save()
                print(f"  ✓ journaled {frm[:8]}… → {to[:8]}… block={jb}")
                continue
            if time.time() - float(st.get("committed_at", 0)) < RECOMMIT_S:
                return 0                 # in flight — the slot is busy, wait
        if w is None:
            print(f"  · pending {frm[:8]}… → {to[:8]}… (no wallet; dry)")
            return 0
        block = ch.current_block()
        if not commit(ch, w, frm, to):
            if to not in set(ch.metagraph().hotkeys):
                # successor not registered YET (re-roll still funding): leave it
                return 0
            state[key] = {"status": "refused", "at": time.time()}
            save()
            continue
        state[key] = {"status": "committed", "committed_at": time.time(),
                      "not_before_block": block}
        save()
        return 0                         # one in flight at a time
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--queue")
    ap.add_argument("--wait", type=int, default=0,
                    help="single mode: seconds to wait for the validator to journal it")
    ap.add_argument("--wallet", default=os.getenv("SN89_SUCCESSION_WALLET", "chef"))
    ap.add_argument("--hotkey", default=os.getenv("SN89_SUCCESSION_HOTKEY", "default"))
    ap.add_argument("--db", default=os.getenv("SN89_DB_PATH", "/root/.sn89/validator-main.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ch = chain.Chain()
    w = None if args.dry_run else _wallet(args.wallet, args.hotkey)
    if args.queue:
        return drain(args, ch, w)
    if not (args.frm and args.to):
        ap.error("--from and --to, or --queue")
    if w is None:
        print(f"dry: would attest {args.frm} → {args.to}")
        return 0
    if not commit(ch, w, args.frm, args.to):
        return 1
    deadline = time.time() + args.wait
    while time.time() < deadline:
        jb = journaled_block(args.db, args.frm, args.to)
        if jb is not None:
            print(f"  ✓ journaled block={jb}")
            return 0
        time.sleep(10)
    return 0 if not args.wait else 2


if __name__ == "__main__":
    sys.exit(main())
