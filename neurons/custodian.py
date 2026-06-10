#!/usr/bin/env python3
"""SN89 collateral custodian — owner-side CLI (docs/collateral.md).

The custodian holds the two privileged keys (vault coldkey + EVM ledger owner
key) and executes every custody event. Validators never run this; they only
read the ledger.

Caller-side helpers (run by miners, no privileged keys):
  make-deposit            compose + coldkey-sign the transfer_stake to the
                          vault; prints extrinsic hex to send to the owner
  make-withdraw-request   coldkey-sign a withdraw request; prints JSON

Owner-side operations:
  deposit                 validate + submit a caller's extrinsic, credit ledger
  withdraw                verify a signed request + settlement lock, debit
                          ledger, transfer_stake the alpha back
  slash-eliminated        burn collateral of hotkeys the validator journal
                          marked eliminated (dry-run by default)
  balance                 read the public ledger

Env (owner): SN89_COLLATERAL_CONTRACT, SN89_VAULT_COLDKEY,
SN89_OWNER_EVM_ADDRESS, SN89_OWNER_EVM_KEY, SN89_VAULT_WALLET (wallet name).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bittensor as bt

from sn89_signals import collateral, config


def _st() -> "bt.Subtensor":
    return bt.Subtensor(network=config.NETWORK)


def _owner_evm() -> tuple[str, str]:
    addr, key = os.getenv("SN89_OWNER_EVM_ADDRESS", ""), os.getenv("SN89_OWNER_EVM_KEY", "")
    if not addr or not key:
        sys.exit("set SN89_OWNER_EVM_ADDRESS and SN89_OWNER_EVM_KEY")
    return addr, key


def _vault_wallet() -> "bt.Wallet":
    name = os.getenv("SN89_VAULT_WALLET", "")
    if not name:
        sys.exit("set SN89_VAULT_WALLET (vault wallet name)")
    return bt.Wallet(name=name)


# ── caller side ───────────────────────────────────────────────────────────────
def cmd_make_deposit(a):
    wallet = bt.Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey)
    ext = collateral.create_deposit_extrinsic(
        _st(), wallet, wallet.hotkey.ss58_address,
        int(a.amount * collateral.RAO_PER_ALPHA))
    print(collateral.encode_extrinsic(ext))


def cmd_make_withdraw_request(a):
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
    print(json.dumps(req, indent=2))


# ── owner side ────────────────────────────────────────────────────────────────
def cmd_balance(a):
    ledger = collateral.CollateralLedger()
    if a.hotkey:
        rao = ledger.balance_of(a.hotkey)
        print(f"{a.hotkey}: {rao} rao ({rao / collateral.RAO_PER_ALPHA:.4f} alpha)")
    else:
        print(f"total:   {ledger.total() / collateral.RAO_PER_ALPHA:.4f} alpha")
        print(f"slashed: {ledger.slashed() / collateral.RAO_PER_ALPHA:.4f} alpha")


def cmd_deposit(a):
    st = _st()
    hex_data = a.extrinsic_hex or open(a.file).read().strip()
    ext = collateral.decode_extrinsic(st, hex_data)
    hotkey, amount = collateral.validate_deposit_extrinsic(
        ext, config.VAULT_COLDKEY, config.NETUID)
    print(f"deposit {amount / collateral.RAO_PER_ALPHA:.4f} alpha from {hotkey}")
    receipt = st.substrate.submit_extrinsic(ext, wait_for_inclusion=True)
    if not receipt.is_success:
        sys.exit(f"transfer_stake failed: {receipt.error_message}")
    credited = collateral.stake_added_rao(
        [e.value if hasattr(e, "value") else e for e in receipt.triggered_events])
    addr, key = _owner_evm()
    tx = collateral.ledger_write("deposit", hotkey, credited, addr, key)
    print(f"credited {credited} rao · ledger tx {tx}")


def cmd_withdraw(a):
    st = _st()
    req = json.loads(open(a.request).read())
    err = collateral.verify_withdraw_request(req)
    if err:
        sys.exit(f"rejected: {err}")
    hotkey, coldkey, amount = req["hotkey"], req["coldkey"], int(req["amount_rao"])

    owner = st.substrate.query("SubtensorModule", "Owner", [hotkey])
    if str(getattr(owner, "value", owner)) != coldkey:
        sys.exit(f"rejected: {coldkey} does not own {hotkey}")

    # settlement lock against the validator journal: not eliminated, nothing
    # unsettled, and the cooldown elapsed since the last signal could settle
    db = sqlite3.connect(config.DB_PATH)
    elim = db.execute("SELECT eliminated_t0 FROM hotkey_meta WHERE hotkey=?",
                      (hotkey,)).fetchone()
    if elim and elim[0] is not None:
        sys.exit("rejected: hotkey is eliminated — collateral burns")
    open_n = db.execute(
        "SELECT COUNT(*) FROM signals WHERE hotkey=? AND status IN "
        "('sealed','revealed','pending')", (hotkey,)).fetchone()[0]
    if open_n:
        sys.exit(f"rejected: {open_n} unsettled signal(s)")
    last_t0 = db.execute("SELECT MAX(t0_unix) FROM signals WHERE hotkey=?",
                         (hotkey,)).fetchone()[0]
    if last_t0 and time.time() < last_t0 + config.WITHDRAW_COOLDOWN_S:
        sys.exit("rejected: withdraw cooldown not elapsed")

    ledger = collateral.CollateralLedger()
    if ledger.balance_of(hotkey) < amount:
        sys.exit("rejected: amount exceeds posted collateral")

    addr, key = _owner_evm()
    tx = collateral.ledger_write("withdraw", hotkey, amount, addr, key)
    vault = _vault_wallet()
    ext = collateral.create_stake_transfer_extrinsic(  # vault → caller
        st, vault, hotkey, amount, dest_coldkey=coldkey)
    receipt = st.substrate.submit_extrinsic(ext, wait_for_inclusion=True)
    if not receipt.is_success:
        # put the ledger back so books match the vault
        collateral.ledger_write("deposit", hotkey, amount, addr, key)
        sys.exit(f"transfer_stake back failed (ledger restored): {receipt.error_message}")
    print(f"withdrew {amount / collateral.RAO_PER_ALPHA:.4f} alpha to {coldkey} · ledger tx {tx}")


def cmd_slash_eliminated(a):
    ledger = collateral.CollateralLedger()
    db = sqlite3.connect(config.DB_PATH)
    rows = db.execute(
        "SELECT hotkey, eliminated_t0 FROM hotkey_meta "
        "WHERE eliminated_t0 IS NOT NULL AND slash_tx IS NULL").fetchall()
    if not rows:
        print("no pending eliminations")
        return
    st = _st() if a.execute else None
    for hotkey, t0 in rows:
        balance = ledger.balance_of(hotkey)
        amount = int(balance * config.ELIM_SLASH_PROPORTION)
        print(f"{hotkey} eliminated at t0={t0:.0f} · collateral {balance} rao "
              f"· slash {amount} rao{'' if a.execute else ' (dry-run)'}")
        if not a.execute or amount == 0:
            continue
        addr, key = _owner_evm()
        tx = collateral.ledger_write("slash", hotkey, amount, addr, key)
        burn_ext = collateral.create_burn_alpha_extrinsic(
            st, _vault_wallet(), hotkey, amount)
        receipt = st.substrate.submit_extrinsic(burn_ext, wait_for_inclusion=True)
        if not receipt.is_success:
            print(f"  ! burn_alpha failed AFTER ledger slash {tx} — "
                  f"funds remain in slashedCollateral pool, reconcile manually: "
                  f"{receipt.error_message}")
        db.execute("UPDATE hotkey_meta SET slash_rao=?, slash_tx=? WHERE hotkey=?",
                   (amount, tx, hotkey))
        db.commit()
        print(f"  burned · ledger tx {tx}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("make-deposit")
    s.add_argument("--amount", type=float, required=True, help="alpha")
    s.add_argument("--wallet.name", dest="wallet_name", required=True)
    s.add_argument("--wallet.hotkey", dest="wallet_hotkey", required=True)
    s.set_defaults(fn=cmd_make_deposit)

    s = sub.add_parser("make-withdraw-request")
    s.add_argument("--amount", type=float, required=True, help="alpha")
    s.add_argument("--wallet.name", dest="wallet_name", required=True)
    s.add_argument("--hotkey", required=True)
    s.set_defaults(fn=cmd_make_withdraw_request)

    s = sub.add_parser("balance")
    s.add_argument("--hotkey", default="")
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser("deposit")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--extrinsic-hex")
    g.add_argument("--file")
    s.set_defaults(fn=cmd_deposit)

    s = sub.add_parser("withdraw")
    s.add_argument("--request", required=True, help="signed request JSON file")
    s.set_defaults(fn=cmd_withdraw)

    s = sub.add_parser("slash-eliminated")
    s.add_argument("--execute", action="store_true",
                   help="actually slash+burn (default: dry-run)")
    s.set_defaults(fn=cmd_slash_eliminated)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
