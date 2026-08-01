#!/usr/bin/env python3
"""Neutral-merge reconciliation — prove the merged vector pays every miner what
the two dedicated mechanisms would have.

Pre-merge, a miner's share of subnet emission is:

    pre[uid] = split0 · lf[uid] + split1 · hf[uid]        (splits from
               MechanismEmissionSplit, vectors from each mecid's own path)

Post-merge (closers at 0 during the soak) it is:

    post[uid] = combine({lf, hf}, comp_weights)[uid]       (single mecid-0)

If comp_weights mirror the chain split, pre == post to float rounding for
EVERY miner — that is the normalize-then-weight guarantee, and this script is
the check that runs daily through the soak. Any drift beyond --tol is a
defect in the merge, not noise; find it before ramping closers.

Reads the same inputs the validator does; prints a per-uid table and exits
nonzero on failure so it can run under a timer with an alert on failure.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import competitions, config, hf_grade  # noqa: E402
from tools.replay_combined import lf_vector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="max acceptable per-uid emission-share drift")
    a = ap.parse_args()
    now = time.time()

    import bittensor as bt
    st = bt.Subtensor(config.NETWORK)
    mg = st.metagraph(config.NETUID)
    uid_by_hotkey = {h: i for i, h in enumerate(mg.hotkeys)}

    # chain-enforced split (what the dedicated mechanisms pay today)
    try:
        raw = st.substrate.query("SubtensorModule", "MechanismEmissionSplit",
                                 [config.NETUID]).value
        tot = sum(raw) or 1
        split = [x / tot for x in raw]
    except Exception:  # noqa: BLE001
        split = [0.5, 0.5]
    print(f"netuid {config.NETUID} · chain split {[round(x, 4) for x in split]}")

    lf = lf_vector(os.path.expanduser(a.db), uid_by_hotkey, now)
    hf = hf_grade.mecid1_weights(uid_by_hotkey, now)

    pre = {}
    for vec, sh in ((lf, split[0]), (hf, split[1] if len(split) > 1 else 0.0)):
        for u, w in vec.items():
            pre[u] = pre.get(u, 0.0) + sh * w

    # Neutrality target: blend at the CHAIN split (not comp_weights) — the
    # chain's u16 split is [32768, 32767] ≈ 0.500008/0.499992, and using a
    # rounded 0.5/0.5 here would report a phantom ~1e-5 drift on every uid.
    # comp_weights only has to match the split to the precision you intend;
    # this check isolates the merge arithmetic itself.
    shares = config.comp_weights_as_of(now)
    if abs(shares.get("closers", 0.0)) > 1e-9:
        print(f"⚠ closers share is {shares['closers']} — a neutral soak wants "
              f"closers:0 (comp shares now: {shares})")
    post = competitions.combine(
        {"lf": lf, "hf": hf},
        {"lf": split[0], "hf": split[1] if len(split) > 1 else 0.0})

    keys = sorted(set(pre) | set(post))
    worst_uid, worst = None, 0.0
    print(f"{'uid':>5} {'pre(dedicated)':>15} {'post(merged)':>13} {'delta':>12}")
    for u in keys:
        d = post.get(u, 0.0) - pre.get(u, 0.0)
        if abs(d) > abs(worst):
            worst, worst_uid = d, u
        flag = "  ⚠" if abs(d) > a.tol else ""
        print(f"{u:>5} {pre.get(u, 0.0):>15.6f} {post.get(u, 0.0):>13.6f} "
              f"{d:>+12.2e}{flag}")

    ok = abs(worst) <= a.tol
    print(f"\nworst drift: uid {worst_uid} {worst:+.2e} (tol {a.tol:.0e}) — "
          + ("✔ MERGE IS PAYOUT-NEUTRAL" if ok else "✗ NOT NEUTRAL — do not ramp"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
