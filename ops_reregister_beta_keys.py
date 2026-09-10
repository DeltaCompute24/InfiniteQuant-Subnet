"""Re-register beta keys that testnet 496 has pruned.

Found 2026-09-10: 17 of 118 roster keys were gone from the metagraph while the
roster said registered=true. 496 is full (MaxAllowedUids 128) and 111 UIDs hold
zero stake, so every registration by anyone evicts one of ours once immunity
(5000 blocks, ~16.7 h) ends. The signer-map timer reads the roster flag, never
the chain, so nothing noticed. Whit's call: re-register, pay with testnet TAO.

Same pacing, burn guard and free-balance precheck as ops_issue_beta_keys.py.
Differences: no key is CREATED here (the hotkey files exist), the roster row
is UPDATED in place, and `registered` is set FROM THE CHAIN before anything is
spent, so a run that stops short still leaves the roster honest.

    ssh iq-main 'cd /opt/sn89-signals && sudo .venv/bin/python ops_reregister_beta_keys.py --plan'
    ssh iq-main 'cd /opt/sn89-signals && sudo nohup .venv/bin/python ops_reregister_beta_keys.py --go \
                 >> /var/log/sn89-beta-rereg.log 2>&1 &'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import bittensor as bt

import ops_issue_beta_keys as issue

NETUID, ENDPOINT = issue.NETUID, issue.ENDPOINT


def on_chain(s) -> set:
    return set(s.metagraph(NETUID).hotkeys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args()
    if not (a.go or a.plan):
        ap.error("--plan or --go")

    s = bt.Subtensor(ENDPOINT)
    roster = issue.load_roster()
    chain = on_chain(s)

    pruned = [r for r in roster["issued"]
              if r.get("testnet_hotkey") and r["testnet_hotkey"] not in chain]
    # Roster truth from the chain, BEFORE spending anything.
    changed = 0
    for r in roster["issued"]:
        want = r.get("testnet_hotkey") in chain
        if bool(r.get("registered")) != want:
            r["registered"] = want
            r["registered_checked_ts"] = int(time.time())
            changed += 1
    if changed and a.go:
        issue.save_roster(roster)
    free = float(str(s.get_balance(issue.funding_coldkey_ss58())).lstrip("τ"))
    burn = float(str(s.recycle(NETUID)).lstrip("τ"))
    print("roster %d · on chain %d · pruned %d · roster flags corrected %d"
          % (len(roster["issued"]), len(roster["issued"]) - len(pruned), len(pruned), changed))
    print("free τ%.6f · burn now τ%.6f · setpoint τ%.6f · fee τ%.4f · ~τ%.4f for all"
          % (free, burn, issue.BURN_SETPOINT_TAO, issue.EXTRINSIC_FEE_TAO,
             len(pruned) * (issue.BURN_SETPOINT_TAO + issue.EXTRINSIC_FEE_TAO)))
    for r in pruned[:a.limit]:
        print("   seq %3d %-22s %s tier=%s" % (r["seq"], r["hotkey_name"],
                                               r["testnet_hotkey"][:12] + "..", r.get("tier")))
    if a.plan or not pruned:
        return 0

    todo = pruned[:a.limit]
    for i, r in enumerate(todo, 1):
        w = bt.Wallet(name=r["wallet"], hotkey=r["hotkey_name"])
        if w.hotkey.ss58_address != r["testnet_hotkey"]:
            print("ABORT: %s on disk is %s, roster says %s -- wallet/roster mismatch"
                  % (r["hotkey_name"], w.hotkey.ss58_address[:12], r["testnet_hotkey"][:12]))
            return 4
        waited = 0
        while True:
            burn = float(str(s.recycle(NETUID)).lstrip("τ"))
            if burn > issue.BURN_ABORT_TAO:
                print("ABORT: burn %.6f > %.6f" % (burn, issue.BURN_ABORT_TAO))
                return 3
            if burn <= issue.BURN_SETPOINT_TAO:
                break
            if waited == 0:
                print("   burn %.6f > setpoint -- waiting" % burn, flush=True)
            time.sleep(issue.POLL_S)
            waited += issue.POLL_S
        free = float(str(s.get_balance(issue.funding_coldkey_ss58())).lstrip("τ"))
        need = burn + issue.EXTRINSIC_FEE_TAO
        if free < need:
            print("\nSTOPPING: have τ%.6f need τ%.6f; %d keys left (~τ%.4f). "
                  "Top up sn89test from the testnet faucet and re-run."
                  % (free, need, len(todo) - i + 1, (len(todo) - i + 1) * need))
            return 5
        # Someone else may have registered it meanwhile (or immunity churn).
        if s.is_hotkey_registered_on_subnet(r["testnet_hotkey"], NETUID):
            ok = True
        else:
            ok = s.burned_register(wallet=w, netuid=NETUID, wait_for_inclusion=True)
            if not ok:
                time.sleep(issue.MIN_GAP_S)
                ok = s.burned_register(wallet=w, netuid=NETUID, wait_for_inclusion=True)
        r["registered"] = bool(ok)
        r["reregistered_ts"] = int(time.time())
        r["reregistrations"] = int(r.get("reregistrations", 0)) + (1 if ok else 0)
        issue.save_roster(roster)
        print("[%2d/%2d] seq %3d %s -> registered=%s burn=%.6f"
              % (i, len(todo), r["seq"], r["testnet_hotkey"][:12] + "..", ok, burn), flush=True)
        if i < len(todo):
            time.sleep(issue.MIN_GAP_S)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
