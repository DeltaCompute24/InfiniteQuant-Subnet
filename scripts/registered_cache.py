#!/usr/bin/env python3
"""Dump the set of hotkeys registered on the SN89 subnet to a JSON cache the
partner-webhook reads for its onboarding registration gate. Runs on a timer so
the webhook never needs the heavy bittensor dependency.
"""
import json, os, tempfile
from datetime import datetime, timezone
import bittensor as bt

NETUID = int(os.getenv("SN89_AUTH_NETUID", "89"))
NETWORK = os.getenv("SN89_AUTH_NETWORK", "finney")
OUT = os.getenv("SN89_AUTH_REG_CACHE",
                "/opt/iq-platform/data/live/sn89-registered-hotkeys.json")

st = bt.Subtensor(network=NETWORK)
mg = st.metagraph(NETUID)
hotkeys = list(mg.hotkeys)
payload = {
    "netuid": NETUID,
    "network": NETWORK,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    "n": len(hotkeys),
    "hotkeys": hotkeys,
}
d = os.path.dirname(OUT)
fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
os.replace(tmp, OUT)
os.chmod(OUT, 0o644)
print(f"sn89-registered-cache: netuid={NETUID} network={NETWORK} n={len(hotkeys)} -> {OUT}")

# Daily miner rewards pool (taostats 'Miner / Day' parity): alpha price in TAO
# x 7200 blocks/day x 41% miner split. Best-effort — the hotkey cache above is
# the primary job and must not fail because of this.
POOL_OUT = os.getenv("SN89_POOL_CACHE",
                     "/opt/iq-platform/data/live/sn89-pool.json")
MINER_SPLIT = 0.41
BLOCKS_PER_DAY = 7200
try:
    si = st.subnet(NETUID)
    price = float(si.price)
    total_miner_pool = price * BLOCKS_PER_DAY * MINER_SPLIT
    # The miner pool above is the WHOLE subnet miner emission. It is then divided
    # between the mechanisms (mecid-0 LF / mecid-1 HF) by MechanismEmissionSplit,
    # so an LF miner only ever receives mecid-0's fraction of it. Compute that
    # fraction live (it ramps as HF's share grows) so the dashboard credits LF
    # miners the pool they actually split, not the combined pool (a 2x overstate
    # while the split is 50/50). HF earnings are attributed separately once any HF
    # miner clears the eligibility gate.
    lf_frac = 1.0
    try:
        q = st.substrate.query("SubtensorModule", "MechanismEmissionSplit", [NETUID])
        sp = list(q.value) if hasattr(q, "value") else list(q)
        tot = sum(sp)
        if tot > 0 and len(sp) >= 1:
            lf_frac = sp[0] / tot
            mecid_split = sp
        else:
            mecid_split = None
    except Exception:
        mecid_split = None
    # ⚑ POST-MERGE (combined_weights_active): the chain split stops meaning
    # LF-vs-HF — mecid-0 carries BOTH, divided by config.COMP_WEIGHTS. Reading
    # the raw split here would credit every LF miner ~2x the moment the split
    # moves to [65535,0] (the exact class of error miners caught at 2x before:
    # harold $120-dash/$74-wallet). Source the fractions from the committed
    # shares instead: lf_frac = mecid0_share x share_lf.
    try:
        import sys as _sys, time as _time
        _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from sn89_signals import config as _cfg
        if _cfg.combined_weights_active(_time.time()):
            shares = _cfg.comp_weights_as_of(_time.time())
            hf_like = shares.get("hf", 0.0) + shares.get("closers", 0.0)
            base = lf_frac                      # mecid-0's chain share (→1.0 post-split-move)
            lf_frac = base * shares.get("lf", 0.0)
            hf_frac_override = base * hf_like
        else:
            hf_frac_override = None
    except Exception:
        hf_frac_override = None
    pool = {
        "netuid": NETUID,
        "network": NETWORK,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "alpha_price_tao": price,
        "miner_pool_tao_day": round(total_miner_pool, 2),
        "mecid_emission_split": mecid_split,      # [mecid0, mecid1] u16, or None
        "lf_emission_frac": round(lf_frac, 6),    # mecid-0's share of the miner pool
        "lf_pool_tao_day": round(total_miner_pool * lf_frac, 4),  # what LF miners split
        "hf_emission_frac": round(hf_frac_override if hf_frac_override is not None
                                  else (1.0 - lf_frac), 6),
        "hf_pool_tao_day": round(total_miner_pool * (hf_frac_override if hf_frac_override is not None
                                  else (1.0 - lf_frac)), 4),
    }
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(POOL_OUT), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(pool, fh)
    os.replace(tmp, POOL_OUT)
    os.chmod(POOL_OUT, 0o644)
    print(f"sn89-pool: miner_pool_tao_day={pool['miner_pool_tao_day']} -> {POOL_OUT}")
except Exception as e:
    print(f"sn89-pool: skipped ({e})")
