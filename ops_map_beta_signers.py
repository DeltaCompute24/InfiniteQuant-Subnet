"""Wire issued beta keys into the limit-watcher signer map.

IDEMPOTENT AND RE-RUNNABLE. Registration takes ~22 min/key, so the beta opens on
a partial cohort and this is run again as more land. It only ever ADDS; it never
removes an entry it did not create, because the file also holds the pre-existing
managed-miner signers.

The watcher calls load_signers() every loop, so a new entry is picked up without
a restart -- which is the whole reason the beta can open before issuance finishes.

RUN
    ssh iq-main 'cd /opt/sn89-signals && .venv/bin/python ops_map_beta_signers.py'
"""
from __future__ import annotations

import json
import os
import shutil
import time

ROSTER = "/opt/iq-platform/data/live/sn89-beta-testnet-hotkeys.json"
SIGNERS = "/etc/iq/sn89-limit-signers.json"
WALLET_ROOT = os.path.expanduser("~/.bittensor/wallets")


def main() -> int:
    with open(ROSTER) as fh:
        roster = json.load(fh)
    issued = [e for e in roster.get("issued", []) if e.get("registered")]

    signers = {}
    if os.path.exists(SIGNERS):
        with open(SIGNERS) as fh:
            signers = json.load(fh)
    before = len(signers)

    added, skipped, missing = 0, 0, []
    for e in issued:
        hk = e["testnet_hotkey"]
        if hk in signers:
            skipped += 1
            continue
        path = os.path.join(WALLET_ROOT, e["wallet"], "hotkeys", e["hotkey_name"])
        if not os.path.exists(path):
            # A roster row whose keyfile is gone is a row we must not sign for.
            # Record it loudly rather than writing a signer that resolves to
            # nothing -- keypair_for() would return None and the submission
            # would fail as `no_signer` with no clue why.
            missing.append((e["hotkey_name"], path))
            continue
        signers[hk] = {"hotkey_file": path,
                       "wallet": e["wallet"],
                       "wallet_hotkey": e["hotkey_name"],
                       "beta": True,
                       "mainnet_hotkey": e["mainnet_hotkey"]}
        added += 1

    if added:
        if os.path.exists(SIGNERS):
            shutil.copy2(SIGNERS, "%s.bak.beta.%s"
                         % (SIGNERS, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
        tmp = SIGNERS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(signers, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SIGNERS)

    print("roster registered=%d · signers %d -> %d (added %d, already present %d)"
          % (len(issued), before, len(signers), added, skipped))
    if missing:
        print("MISSING KEYFILES -- not signed for:")
        for name, p in missing:
            print("   %s -> %s" % (name, p))
    print("watcher re-reads the signer map each loop; no restart needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
