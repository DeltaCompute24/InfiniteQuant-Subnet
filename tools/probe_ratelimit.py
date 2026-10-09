"""Definitively read the Commitments rate limit on the testnet chain + check
miner2 registration on netuid 496. Read-only."""
import os
import sys

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import chain as chainmod
from sn89_signals import config

ch = chainmod.Chain(network=os.getenv("SN89_NETWORK", "test"),
                    netuid=int(os.getenv("SN89_NETUID", "496")))
sub = ch.st.substrate

print("=== Commitments pallet storage functions ===")
try:
    md = sub.get_metadata_storage_functions("Commitments")
    for f in md:
        name = f.get("storage_name") if isinstance(f, dict) else getattr(f, "name", f)
        print("  -", name)
except Exception as e:
    print("  (metadata list failed:", e, ")")

print("\n=== rate-limit candidates ===")
for args in ([], [config.NETUID]):
    for name in ("RateLimit", "MaxSpace", "DefaultRateLimit"):
        try:
            q = sub.query("Commitments", name, args)
            print(f"  storage Commitments.{name}({args}) = {getattr(q,'value',q)}")
        except Exception as e:
            print(f"  storage Commitments.{name}({args}) ERR {type(e).__name__}: {e}")
for name in ("RateLimit", "DefaultRateLimit", "MaxSpace"):
    try:
        c = sub.get_constant("Commitments", name)
        print(f"  const   Commitments.{name} = {getattr(c,'value',c)}")
    except Exception as e:
        print(f"  const   Commitments.{name} ERR {type(e).__name__}: {e}")

print("\n=== miner2 registration on netuid 496 ===")
try:
    import bittensor as bt
    w = bt.Wallet(name="sn89test", hotkey="miner2")
    hk = w.hotkey.ss58_address
    mg = ch.st.metagraph(netuid=config.NETUID)
    reg = hk in list(mg.hotkeys)
    uid = list(mg.hotkeys).index(hk) if reg else None
    print(f"  miner2 hotkey={hk} registered_on_496={reg} uid={uid}")
except Exception as e:
    print("  (registration check failed:", e, ")")
