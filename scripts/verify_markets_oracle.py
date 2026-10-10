#!/usr/bin/env python3
"""Verify the published Markets oracle record against the chain. Anyone can run this.

    python scripts/verify_markets_oracle.py [--base URL] [--rows] [--network finney]

1. Fetch the published batch chain (<base>/batches.jsonl) and every window's root
   (<base>/<w>/oracle.json); with --rows, also re-derive each root from the window's rows.
2. Walk the chain from genesis: every `prev` and `link` must recompute, and every batch root must be
   the root of its windows.
3. Read the Markets entity hotkey's on-chain commitment (UID 199 on netuid 89). Its link must be one
   of the published links. Because each link commits to every batch before it, a match proves that
   none of the published history up to that batch has been rewritten since it was committed.
Exit 0 when everything holds, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import config, oracle  # noqa: E402

ENTITY_HOTKEY = "5Hdvy1Msc9Su6U7kcJQRFnc39NNVumAcPnqFLoHqZ4Uix6g3"


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "sn89-oracle-verify/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=config.MARKETS_ORACLE_PUBLIC_BASE)
    ap.add_argument("--rows", action="store_true", help="also recompute every window root from its rows")
    ap.add_argument("--network", default="finney")
    ap.add_argument("--netuid", type=int, default=89)
    ap.add_argument("--hotkey", default=ENTITY_HOTKEY)
    a = ap.parse_args()
    base = a.base.rstrip("/")

    batches = [json.loads(l) for l in get(f"{base}/batches.jsonl").splitlines() if l.strip()]
    if not batches:
        print("no batches published yet")
        return 1
    roots: dict[int, str] = {}
    bad = 0
    for b in batches:
        for i in range(int(b["n"])):
            w = int(b["w0"]) + i * oracle.WINDOW_MS
            meta = json.loads(get(f"{base}/{w}/oracle.json"))
            roots[w] = meta["root"]
            if a.rows:
                rows = [json.loads(l) for l in get(f"{base}/{w}/oracle.jsonl").splitlines() if l.strip()]
                if oracle.window_root(rows) != meta["root"] or len(rows) != int(meta["n"]):
                    print(f"window {w}: rows do not reproduce the published root")
                    bad += 1
    ok, why = oracle.verify_chain(batches, roots)
    print(f"chain: {len(batches)} batches, {len(roots)} windows -> {'OK' if ok else 'BROKEN: ' + why}")

    import bittensor as bt
    st = bt.Subtensor(a.network)
    raw = None
    try:
        raw = st.substrate.query("Commitments", "CommitmentOf", [a.netuid, a.hotkey]).value
    except Exception as e:                                               # noqa: BLE001
        print(f"chain read failed: {e}")
    onchain = None
    if raw:
        for f in (raw.get("info") or {}).get("fields") or []:
            for item in (f if isinstance(f, list) else [f]):
                for k, v in (item or {}).items():
                    if k.startswith("Raw"):
                        s = bytes.fromhex(v[2:]).decode() if isinstance(v, str) and v.startswith("0x") else (
                            bytes(v).decode() if isinstance(v, (list, bytes)) else str(v))
                        onchain = oracle.decode_anchor(s) or onchain
    if onchain is None:
        print("on-chain: no oracle anchor found for the entity hotkey")
        return 1
    links = {b["link"]: b for b in batches}
    hit = links.get(onchain["link"])
    print(f"on-chain head: batch {onchain['w0']} link {onchain['link'][:16]}… -> "
          f"{'IN the published chain' if hit and int(hit['w0']) == onchain['w0'] else 'NOT in the published chain'}")
    return 0 if ok and hit and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
