"""Raise the two netuid-496 hyperparameters that block the beta cohort.

WHY
  MaxAllowedUids on 496 is 128 with 26 in use, so only 102 slots are free and the
  117-key beta cohort does not fit.

  TargetRegistrationsPerInterval is 2 per 100 blocks with AdjustmentAlpha=0 (no
  smoothing), so registering 117 keys in a burst overshoots the target ~60x and
  reprices the burn hard. MinBurn is 0.0005 TAO and MaxBurn is 100 TAO, against a
  sn89test balance of ~0.85 TAO -- the same self-DoS the mainnet onboarding doc
  records, on a tighter budget.

SAFETY
  Owner-signed, and it refuses to run unless 496's SubnetOwner really is our
  sn89test coldkey. Raising MaxAllowedUids is safe: it is LOWERING it that prunes
  neurons. Both are testnet-only knobs on a subnet we own.

RUN
  ssh iq-main 'cd /opt/sn89-signals && .venv/bin/python ops_set_496_hyper.py'

  Run it from /opt/sn89-signals, never from /tmp: a script's own directory lands
  on sys.path[0], and a stray /tmp/inspect.py there has shadowed the stdlib and
  broken `import bittensor` before.
"""
import sys

import bittensor as bt

NET = 496
TARGET_UIDS = 192          # 26 in use + 117 cohort + headroom for seed miners
TARGET_REGS = 64           # so a 117-key batch does not ratchet the burn

s = bt.Subtensor(network="wss://test.chain.opentensor.ai:443")
w = bt.Wallet(name="sn89test", hotkey="owner")

owner = s.query_subtensor("SubnetOwner", params=[NET])
owner = getattr(owner, "value", owner)
me = w.coldkeypub.ss58_address
print("subnet owner : %s" % owner)
print("our coldkey  : %s" % me)
if owner != me:
    sys.exit("REFUSING: we are not the subnet owner of netuid %d" % NET)


def show(label):
    def q(n):
        v = s.query_subtensor(n, params=[NET])
        return getattr(v, "value", v)
    print("%s MaxAllowedUids=%s  TargetRegs/Interval=%s  recycle=%s  uids_in_use=%s"
          % (label, q("MaxAllowedUids"), q("TargetRegistrationsPerInterval"),
             s.recycle(NET), s.metagraph(NET).n))


def send(call_fn, params, label):
    call = s.substrate.compose_call(call_module="AdminUtils",
                                    call_function=call_fn, call_params=params)
    ext = s.substrate.create_signed_extrinsic(call=call, keypair=w.coldkey)
    r = s.substrate.submit_extrinsic(ext, wait_for_inclusion=True)
    ok = getattr(r, "is_success", None)
    print("  %-46s -> ok=%s %s"
          % (label, ok, "" if ok else getattr(r, "error_message", "")))
    return ok


show("\nbefore:")
print("\nsubmitting:")
send("sudo_set_max_allowed_uids",
     {"netuid": NET, "max_allowed_uids": TARGET_UIDS},
     "MaxAllowedUids -> %d" % TARGET_UIDS)
send("sudo_set_target_registrations_per_interval",
     {"netuid": NET, "target_registrations_per_interval": TARGET_REGS},
     "TargetRegistrationsPerInterval -> %d" % TARGET_REGS)
show("\nafter: ")
