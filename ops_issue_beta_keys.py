"""Issue the SN89 custom-sizing beta keys on testnet 496.

RUN (from /opt/sn89-signals, never /tmp -- a script's own dir lands on sys.path[0]
and a stray /tmp/inspect.py has broken `import bittensor` before):

    ssh iq-main 'cd /opt/sn89-signals && .venv/bin/python ops_issue_beta_keys.py --plan'
    ssh iq-main 'cd /opt/sn89-signals && nohup .venv/bin/python ops_issue_beta_keys.py --go \
                 >> /var/log/sn89-beta-issue.log 2>&1 &'

WHY IT IS SLOW ON PURPOSE
  TargetRegistrationsPerInterval on 496 is 2 per 100 blocks and AdjustmentAlpha is
  0 (no smoothing). Root owns that knob -- our owner call got BadOrigin -- so we
  cannot raise it. Registering in a burst overshoots the target and reprices the
  burn against a ~0.85 TAO balance, which is the self-DoS the mainnet onboarding
  doc records. One registration per PACE_S keeps us at the target, and the burn
  guard aborts if it climbs anyway.

WHY IT NEVER EVICTS
  128 UID cap (MaxAllowedUids x mechanism_count <= 256, and 496 runs 2 mechanisms),
  26 in use, so 102 are free. 94 traders + 8 reserved fills them exactly. Nothing
  is pruned. That matters because ImmunityPeriod is 5000 blocks ~ 16.7 h and a
  paced run takes about that long: if the run needed evictions, the keys
  registered first would leave immunity just as the last ones registered and
  could be evicted by them.

KEY HYGIENE
  * Wallet dirs are sn89beta-<seq>-<rand4>. NEVER the trader's handle: the mainnet
    self-fund onboarding named dirs from the display name, everyone who typed the
    placeholder landed in one shared dir, and it cross-wired coldkeys and destroyed
    hotkey secrets. The handle lives in the roster, not the path.
  * create_new_hotkey PRINTS THE MNEMONIC TO STDOUT. It is captured to a mode-600
    file and never reaches the terminal, because the terminal is what gets pasted.
  * Resumable: an already-registered hotkey is skipped, so an interrupted run is
    restarted with the same command.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import string
import sys
import time

import bittensor as bt

NETUID = 496
ENDPOINT = "wss://test.chain.opentensor.ai:443"
FUNDING_WALLET = "sn89test"          # holds the coldkey that pays the burn

COHORT_PATH = "/opt/iq-platform/data/live/sn89-beta-cohort.json"
ROSTER_PATH = "/opt/iq-platform/data/live/sn89-beta-testnet-hotkeys.json"
SEED_PATH = "/root/.sn89/sn89-beta-seeds.jsonl"       # mode 600

# Hotkey names this script must never create, overwrite or register. `owner` is
# the testnet validator's signing key -- UID 0, 358k stake, the weight setter.
RESERVED_HOTKEYS = {"owner", "vali", "vali2", "miner", "default"}

# ADAPTIVE PACING. Do not set a fixed rate here -- it was wrong twice.
#
# MEASURED on 496, not modelled:
#   a registration multiplies the burn by ~1.25
#   an idle block decays it by ~0.998
# so one registration takes ~111 blocks (~22 min) to decay off, and the
# equilibrium pace is SLOWER than the 2-per-100-block target implies. The first
# guess (600 s, from the target) was close but optimistic; the second ("fire the
# batch inside one interval, the burn reprices at the boundary") was simply
# false -- the burn moves continuously and would have aborted around key 20.
#
# So: no rate. Register whenever the burn is at or under the setpoint, wait when
# it is not. Self-tunes to whatever the chain actually does, which is the only
# thing that has been reliable here.
#
# The setpoint is the speed/cost dial. Raising it buys throughput with TAO:
#   0.001  ~2x MinBurn   cheapest, slowest
#   0.010  ~20x          much faster, ~1.1 TAO for 93 keys -- needs a faucet top-up
BURN_SETPOINT_TAO = float(os.getenv("SN89_BETA_BURN_SETPOINT", "0.001"))
EXTRINSIC_FEE_TAO = 0.0022   # measured on 496: burned_register's own fee,
                             # paid in free TAO on TOP of the recycle burn
POLL_S = 30                  # how often to re-check the burn while waiting
MIN_GAP_S = 13               # MaxRegistrationsPerBlock is 1; never go under a block
BURN_ABORT_TAO = 0.05        # 100x MinBurn -- survives one reprice, catches a runaway.
MAX_KEYS = 94


def rand4() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))


def load_cohort() -> list[dict]:
    with open(COHORT_PATH) as fh:
        c = json.load(fh)
    out = []
    for hk in c.get("qualified", []):
        out.append({"mainnet_hotkey": hk, "tier": "qualified"})
    for hk in c.get("invited_unqualified", []):
        out.append({"mainnet_hotkey": hk, "tier": "active"})
    return out[:MAX_KEYS]


def load_roster() -> dict:
    if os.path.exists(ROSTER_PATH):
        with open(ROSTER_PATH) as fh:
            return json.load(fh)
    return {"netuid": NETUID, "network": "testnet", "issued": []}


def save_roster(r: dict) -> None:
    tmp = ROSTER_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(r, fh, indent=1)
    os.replace(tmp, ROSTER_PATH)


def record_seed(wallet_name: str, hotkey_name: str, ss58: str, captured: str) -> None:
    """Mnemonic to a mode-600 file. Never stdout."""
    fd = os.open(SEED_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps({"ts": int(time.time()), "wallet": wallet_name,
                             "hotkey": hotkey_name, "ss58": ss58,
                             "creation_output": captured}) + "\n")
    os.chmod(SEED_PATH, 0o600)


def funding_coldkey_ss58() -> str:
    """The coldkey that pays the burn, read straight off coldkeypub.txt.

    Deliberately does NOT construct a bt.Wallet. A wallet object here points at
    whatever hotkey name it was opened with, and the first version of this script
    called create_new_hotkey on one -- which targeted `owner`, the validator's own
    signing key at UID 0 with 358k stake. Reading a public key needs no wallet, so
    it gets no wallet.
    """
    p = os.path.join(os.path.expanduser("~/.bittensor/wallets"),
                     FUNDING_WALLET, "coldkeypub.txt")
    with open(p) as fh:
        return json.load(fh)["ss58Address"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually create and register")
    ap.add_argument("--plan", action="store_true", help="show what would happen")
    ap.add_argument("--limit", type=int, default=MAX_KEYS)
    args = ap.parse_args()
    if not (args.go or args.plan):
        ap.error("pass --plan or --go")

    cohort = load_cohort()[: args.limit]
    roster = load_roster()
    done = {e["mainnet_hotkey"] for e in roster["issued"]}
    todo = [c for c in cohort if c["mainnet_hotkey"] not in done]

    s = bt.Subtensor(network=ENDPOINT)
    mg = s.metagraph(NETUID)
    free = 128 - mg.n
    burn = s.recycle(NETUID)

    print("cohort %d · already issued %d · to issue %d" % (len(cohort), len(done), len(todo)))
    print("496: %d UIDs in use, %d free · burn %s" % (mg.n, free, burn))
    print("adaptive: register while burn <= %.6f, else wait (poll %ds)"
          % (BURN_SETPOINT_TAO, POLL_S))
    if len(todo) > free:
        print("REFUSING: %d keys needed but only %d free slots -- registering past "
              "the free slots evicts neurons, and during a paced run the evicted "
              "ones can be keys issued earlier in this same run." % (len(todo), free))
        return 2
    if args.plan:
        for c in todo[:5]:
            print("   would issue for %s (%s)" % (c["mainnet_hotkey"][:12] + "..", c["tier"]))
        print("   ... and %d more" % max(0, len(todo) - 5))
        return 0

    for i, c in enumerate(todo, 1):
        # Wait for the burn to come back under the setpoint. This is the pacing.
        waited = 0
        while True:
            burn = float(str(s.recycle(NETUID)).lstrip("τ"))
            if burn > BURN_ABORT_TAO:
                print("ABORT: burn %.6f > %.6f -- decay is not keeping up. Stop, "
                      "let it fall, restart." % (burn, BURN_ABORT_TAO))
                return 3
            if burn <= BURN_SETPOINT_TAO:
                break
            if waited == 0:
                print("   burn %.6f > setpoint %.6f -- waiting"
                      % (burn, BURN_SETPOINT_TAO), flush=True)
            time.sleep(POLL_S)
            waited += POLL_S
        if waited:
            print("   waited %ds for burn to fall to %.6f" % (waited, burn), flush=True)

        # ── can this registration actually succeed? ──────────────────────────
        # Read for free BEFORE creating a key and paying an extrinsic fee. Without
        # this the run does not stop when the coldkey empties: burned_register
        # simply fails, retries once, records registered=false, and moves to the
        # next candidate -- so an empty wallet produces a HOTKEY AND A ROSTER ROW
        # FOR EVERY REMAINING CANDIDATE, none of them registered, all needing
        # reconciliation afterwards.
        #
        # Same rule as the alpha sweep that burned 7.63 alpha retrying a
        # transfer_stake whose fee could never be paid: a precondition that can be
        # read for free must never be discovered by spending money on an extrinsic
        # that cannot succeed.
        free = float(str(s.get_balance(funding_coldkey_ss58())).lstrip("τ"))
        need = burn + EXTRINSIC_FEE_TAO
        if free < need:
            left = len(todo) - i + 1
            print("\nSTOPPING: insufficient free balance.\n"
                  "  have      τ%.6f\n"
                  "  need      τ%.6f  (burn %.6f + fee %.6f)\n"
                  "  remaining %d keys, about τ%.4f to finish\n"
                  "  Top up the coldkey from the testnet faucet, then re-run --\n"
                  "  the script is resumable and skips every key already issued."
                  % (free, need, burn, EXTRINSIC_FEE_TAO, left, left * need))
            return 5

        seq = len(roster["issued"]) + 1
        hk_name = "sn89beta-%03d-%s" % (seq, rand4())

        # Build the wallet AT THE NEW HOTKEY NAME and create on that object.
        # Calling create_new_hotkey on `funder` targets whatever hotkey that
        # wallet was opened with -- which is `owner`, the validator's own signing
        # key at UID 0 with 358k stake. The first run of this script did exactly
        # that and was saved only by overwrite=False and an unwritable keyfile.
        w = bt.Wallet(name=FUNDING_WALLET, hotkey=hk_name)

        # Belt as well as braces: never touch a name that already exists, and
        # never a reserved one, whatever the wallet object thinks it points at.
        kf = os.path.join(os.path.expanduser("~/.bittensor/wallets"),
                          FUNDING_WALLET, "hotkeys", hk_name)
        if hk_name in RESERVED_HOTKEYS or os.path.exists(kf):
            print("REFUSING to create %r -- reserved or already present" % hk_name)
            return 4

        # create_new_hotkey prints the mnemonic. Capture it; never let it out.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            w.create_new_hotkey(use_password=False, overwrite=False, suppress=True)
        captured = buf.getvalue()

        w = bt.Wallet(name=FUNDING_WALLET, hotkey=hk_name)
        ss58 = w.hotkey.ss58_address
        record_seed(FUNDING_WALLET, hk_name, ss58, captured)

        ok = s.burned_register(wallet=w, netuid=NETUID, wait_for_inclusion=True)
        if not ok:
            # MaxRegistrationsPerBlock is 1, so a collision simply drops one.
            # Retry once on the next block before recording a failure.
            time.sleep(MIN_GAP_S)
            ok = s.burned_register(wallet=w, netuid=NETUID, wait_for_inclusion=True)
        entry = {"seq": seq, "wallet": FUNDING_WALLET, "hotkey_name": hk_name,
                 "testnet_hotkey": ss58, "mainnet_hotkey": c["mainnet_hotkey"],
                 "tier": c["tier"], "registered": bool(ok), "ts": int(time.time())}
        roster["issued"].append(entry)
        save_roster(roster)
        print("[%3d/%3d] %s -> %s registered=%s burn=%.6f"
              % (i, len(todo), hk_name, ss58[:12] + "..", ok, burn), flush=True)

        if i < len(todo):
            time.sleep(MIN_GAP_S)

    print("done. roster at %s · seeds at %s (mode 600 -- vault them)"
          % (ROSTER_PATH, SEED_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
