#!/usr/bin/env python3
"""SN89 collateral CLI — miner side, fully self-serve (docs/collateral.md).

Your keys never leave this box. Deposits are a transfer_stake your coldkey
signs locally; withdrawals are a JSON request your coldkey signs locally. The
custodian API only ever receives signed artifacts it cannot forge or alter.

  info        show vault / minimum / contract (GET from the custodian API)
  balance     your posted collateral from the public EVM ledger
  deposit     stake alpha as collateral:   sign transfer_stake → POST
  withdraw    request your collateral back: sign request → POST
              (settles only when you have no open signals, the 72h cooldown
               has passed, and you are not eliminated — check with `preview`)
  preview     withdrawal eligibility for a hotkey

API base: SN89_COLLATERAL_API (default below). Independent verification: your
balance is world-readable on the ledger contract — `balance` reads the chain
directly when SN89_COLLATERAL_CONTRACT is set, the API otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from sn89_signals import collateral, config

API = os.getenv("SN89_COLLATERAL_API",
                "https://partner.infinitequant.app/api/sn89/collateral").rstrip("/")


def _get(path: str) -> dict:
    r = requests.get(f"{API}{path}", timeout=30)
    body = r.json()
    if r.status_code != 200:
        sys.exit(f"error: {body.get('error', r.status_code)}")
    return body


def _post(path: str, payload: dict, timeout: int = 180) -> dict:
    # deposits/withdrawals wait for chain inclusion server-side (~12-30s)
    r = requests.post(f"{API}{path}", json=payload, timeout=timeout)
    body = r.json()
    if r.status_code != 200:
        sys.exit(f"rejected: {body.get('error', r.status_code)}")
    return body


def _vault_coldkey() -> str:
    """The vault address deposits go to. Prefer the locally-configured value
    (ships in config.py at release); fall back to the API and make the miner
    confirm what they are sending to."""
    if config.VAULT_COLDKEY:
        return config.VAULT_COLDKEY
    info = _get("/info")
    vault = info.get("vault_coldkey", "")
    if not vault:
        sys.exit("custodian API did not return a vault coldkey")
    print(f"vault coldkey (from API): {vault}")
    if input("deposit to this vault? [y/N] ").strip().lower() != "y":
        sys.exit("aborted")
    return vault


def cmd_info(a):
    print(json.dumps(_get("/info"), indent=2))


def cmd_balance(a):
    if config.COLLATERAL_CONTRACT:
        rao = collateral.CollateralLedger().balance_of(a.hotkey)
    else:
        rao = _get(f"/balance/{a.hotkey}")["balance_rao"]
    print(f"{a.hotkey}: {rao / collateral.RAO_PER_ALPHA:.4f} alpha ({rao} rao)")


def cmd_preview(a):
    print(json.dumps(_post("/query-withdraw", {"hotkey": a.hotkey}, timeout=30),
                     indent=2))


def cmd_deposit(a):
    import bittensor as bt
    wallet = bt.Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey)
    hotkey = wallet.hotkey.ss58_address
    vault = _vault_coldkey()
    amount_rao = int(a.amount * collateral.RAO_PER_ALPHA)
    print(f"deposit {a.amount} alpha · hotkey {hotkey} → vault {vault}")
    st = bt.Subtensor(network=config.NETWORK)
    ext = collateral.create_deposit_extrinsic(
        st, wallet, hotkey, amount_rao, vault_coldkey=vault)
    out = _post("/deposit", {"extrinsic_hex": collateral.encode_extrinsic(ext)})
    print(f"credited {out['credited_rao'] / collateral.RAO_PER_ALPHA:.4f} alpha "
          f"· ledger tx {out['ledger_tx']}")


def cmd_withdraw(a):
    import bittensor as bt
    wallet = bt.Wallet(name=a.wallet_name)
    req = {
        "amount_rao": int(a.amount * collateral.RAO_PER_ALPHA),
        "coldkey": wallet.coldkeypub.ss58_address,
        "hotkey": a.hotkey,
        "nonce": secrets.token_hex(8),
        "timestamp_ms": int(time.time() * 1000),
    }
    msg = collateral.withdraw_request_message(
        req["amount_rao"], req["coldkey"], req["hotkey"],
        req["nonce"], req["timestamp_ms"])
    req["signature"] = wallet.coldkey.sign(msg).hex()
    out = _post("/withdraw", req)
    print(f"withdrew {out['amount_rao'] / collateral.RAO_PER_ALPHA:.4f} alpha "
          f"to {req['coldkey']} · ledger tx {out['ledger_tx']}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("balance")
    s.add_argument("--hotkey", required=True)
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser("preview")
    s.add_argument("--hotkey", required=True)
    s.set_defaults(fn=cmd_preview)

    s = sub.add_parser("deposit")
    s.add_argument("--amount", type=float, required=True, help="alpha")
    s.add_argument("--wallet.name", dest="wallet_name", required=True)
    s.add_argument("--wallet.hotkey", dest="wallet_hotkey", required=True)
    s.set_defaults(fn=cmd_deposit)

    s = sub.add_parser("withdraw")
    s.add_argument("--amount", type=float, required=True, help="alpha")
    s.add_argument("--wallet.name", dest="wallet_name", required=True)
    s.add_argument("--hotkey", required=True)
    s.set_defaults(fn=cmd_withdraw)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
