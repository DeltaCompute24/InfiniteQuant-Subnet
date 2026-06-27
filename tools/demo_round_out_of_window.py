"""Live demo (audit #6): a miner-supplied drand round is judged against the
commitment's own inclusion-block time, not round_time(rnd). Commits a round ~3 h
past the expected commit+24h window from miner2; the validator must journal it
VOID (round_out_of_window) at ingest, never sealed — so it can't sit sealed
forever re-pulling its blob, and a far-future round can't wedge the loop.

Read-only except the single set_commitment it issues. Run from /opt/sn89-signals:
    set -a && . ./.env.test && set +a
    .venv/bin/python tools/demo_round_out_of_window.py --wallet.name sn89test --wallet.hotkey miner2
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bittensor as bt  # noqa: E402

from sn89_signals import chain as chainmod  # noqa: E402
from sn89_signals import config, crypto  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wallet.name", dest="wallet_name", default="sn89test")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="miner2")
    p.add_argument("--hours-late", type=float, default=3.0,
                   help="how far past the expected commit+24h the round sits "
                        "(must exceed ROUND_TOLERANCE_S to be out-of-window)")
    args = p.parse_args()

    ch = chainmod.Chain(network=os.getenv("SN89_NETWORK", "test"),
                        netuid=int(os.getenv("SN89_NETUID", "496")))
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    hk = wallet.hotkey.ss58_address

    import time
    now = time.time()
    # A round that opens ~hours_late AFTER commit+24h — decodes fine (well under
    # 2**63), but is outside the ±ROUND_TOLERANCE_S window, so it must be voided.
    bad_round = crypto.target_round(now + config.REVEAL_DELAY_S + args.hours_late * 3600)
    good_round = crypto.target_round(now + config.REVEAL_DELAY_S)
    commit_hex = "c" * 64
    url = "soak://round-demo/out-of-window"

    print(f"hotkey={hk[:10]}…  tolerance=±{config.ROUND_TOLERANCE_S}s")
    print(f"good round (commit+24h)   = {good_round}")
    print(f"BAD  round (+{args.hours_late}h past)   = {bad_round}  "
          f"(Δ {(bad_round - good_round) * config.__dict__.get('DRAND_PERIOD_S', 3)}s of drand time)")
    print(f"committing {commit_hex[:12]}… with the BAD round at block ~{ch.current_block()}")
    ok = ch.commit(wallet, commit_hex, bad_round, url)
    print(f"set_commitment ok={ok}")
    print("→ watch: journalctl -u sn89-validator.service -f  for "
          f"'+ void {commit_hex[:12]}… … (round_out_of_window)'")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
