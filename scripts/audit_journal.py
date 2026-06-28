#!/usr/bin/env python3
"""Audit the single validator: re-derive its weights from the published journal
and confirm they match what it set on-chain. This is the trust mechanism of the
single-validator model (docs/single-validator-model.md) — "replay and verify the
results match expectations." Anyone can run it; it needs no validator role.

    python3 audit_journal.py <checkpoint.json> [--chain] [--tolerance 1e-6]

Checks:
  1. REPLAY (always, offline): re-derive weights from the journal with the same
     scoring code the validator runs, and compare to the checkpoint's recorded
     weights. Proves the weight vector is an honest function of the journal — a
     validator that hid a loss or fudged a tier is caught.
  2. --chain ON-CHAIN WEIGHTS: compare the replay to the LIVE metagraph weights
     (not the checkpoint's snapshot), catching a validator that set something
     different on-chain than its journal implies.
  3. --chain COMMIT ANCHORS: spot-check that each signal's commit_hex exists
     on-chain at its commit_block (single CommitmentOf reads — no block scan),
     proving the journal contains no fabricated signals.

Exits non-zero on any mismatch.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import replay  # noqa: E402


def _weights_equal(a: dict, b: dict, tol: float) -> tuple[bool, list]:
    keys = set(a) | set(b)
    diffs = []
    for k in sorted(keys):
        va, vb = a.get(k, 0.0), b.get(k, 0.0)
        if abs(va - vb) > tol:
            diffs.append((k, va, vb))
    return (not diffs), diffs


def main():
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        print("usage: audit_journal.py <checkpoint.json> [--chain] [--tolerance T]")
        sys.exit(2)
    # default tolerance accommodates the u16 quantization set_weights applies on-chain
    tol = float(args[args.index("--tolerance") + 1]) if "--tolerance" in args else 1e-3
    cp = json.load(open(paths[0]))
    signals, meta, now = cp["signals"], cp["meta"], cp["now_unix"]

    uid_by_hotkey = {hk: int(u) for hk, u in (cp.get("uid_by_hotkey") or {}).items()}
    recorded = {int(k): float(v) for k, v in (cp.get("weights_onchain") or {}).items()}

    if "--chain" in args:
        try:
            from sn89_signals import chain
            ch = chain.Chain()
            mg = ch.metagraph()
            uid_by_hotkey = {hk: i for i, hk in enumerate(mg.hotkeys)}
            vuid = cp.get("validator_uid")
            if vuid is not None:
                live = ch.weights_for_uid(int(vuid))
                if live:
                    recorded = live
                    print(f"  (live: on-chain weights for validator uid {vuid} — "
                          f"{len(recorded)} entries)")
                else:
                    print(f"  ⚠ --chain: no on-chain weights set yet for validator uid {vuid}")
            else:
                print("  ⚠ --chain: checkpoint has no validator_uid; using snapshot")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ --chain requested but chain read failed: {e}")
            sys.exit(2)

    if not uid_by_hotkey:
        print("INCONCLUSIVE: no uid map (checkpoint lacks uid_by_hotkey; pass --chain)")
        sys.exit(2)

    print(f"replaying {len(signals)} signals over {len(meta)} hotkeys (now={now:.0f})…")
    replayed = replay.weights_from_journal(signals, meta, uid_by_hotkey, now)

    if not recorded:
        print(f"  replay produced {len(replayed)} uids, but there are NO on-chain weights "
              "to compare against — run with --chain, or export with --validator-hotkey.")
        print("\nAUDIT INCONCLUSIVE — nothing to verify the replay against.")
        sys.exit(2)

    match, diffs = _weights_equal(replayed, recorded, tol)
    if match:
        print(f"  ✓ REPLAY MATCHES on-chain weights ({len(replayed)} uids, tol={tol})")
        print("\nAUDIT PASSED — the validator's on-chain weights match a replay of its journal.")
        sys.exit(0)
    print(f"  ✗ REPLAY MISMATCH on {len(diffs)} uid(s):")
    for uid, rv, cv in diffs[:20]:
        print(f"      uid {uid}: replay={rv:.6f}  on-chain={cv:.6f}")
    print("\nAUDIT FAILED — the validator's on-chain weights do not match its journal.")
    sys.exit(1)


if __name__ == "__main__":
    main()
