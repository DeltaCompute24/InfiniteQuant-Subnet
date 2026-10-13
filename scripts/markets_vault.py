#!/usr/bin/env python3
"""SN89 Markets V5 collateral vault, for an entity (sn89_signals/markets_vault.py has the rules).

  deposit  --amount A   move A alpha of the entity's own stake onto the vault hotkey (if it is not there
                        already), transfer it to the vault coldkey, and file the signed mk.deposit claim
  withdraw --amount A   file mk.withdraw; approved at the next 15-minute checkpoint for what is free
  exit                  file mk.exit; everything free is approved, the locked net loss goes to the reserve
  status                the entity's vault balance, locked and free alpha, from a local grade cache

The coldkey signs the transfers (it is the entity hotkey's owner); the hotkey signs the claims. Both come
from a bittensor wallet: --wallet.name / --wallet.hotkey (and --wallet.path).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sn89_signals import config, hf, markets, markets_vault as vault  # noqa: E402

RAO = 10**9


def _wallet(a):
    import bittensor as bt
    return bt.Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey, path=a.wallet_path)


def _submit_extrinsic(st, wallet, function: str, params: dict) -> tuple[int, int]:
    sub = st.substrate
    call = sub.compose_call(call_module="SubtensorModule", call_function=function, call_params=params)
    ext = sub.create_signed_extrinsic(call=call, keypair=wallet.coldkey)
    rec = sub.submit_extrinsic(ext, wait_for_inclusion=True, wait_for_finalization=True)
    if not rec.is_success:
        raise SystemExit(f"{function} failed: {rec.error_message}")
    return int(sub.get_block_number(rec.block_hash)), int(rec.extrinsic_idx)


def _claim(a, payload: dict) -> dict:
    import websockets
    w = _wallet(a)
    kp = w.hotkey
    seq_path = os.path.expanduser(f"~/.sn89/vault-seq-{kp.ss58_address[:12]}")
    os.makedirs(os.path.dirname(seq_path), exist_ok=True)
    seq, nonce, ts = hf.next_submit_seq(seq_path), os.urandom(8).hex(), int(time.time() * 1000)
    payload = {"trade_pair": config.MARKETS_RATE_PAIR, **payload}
    sb = hf.submit_signing_bytes(kp.ss58_address, seq, nonce, payload, ts)
    frame = {"kind": "hf.submit", "v": 1, "hk": kp.ss58_address, "seq": seq, "nonce": nonce, "payload": payload,
             "ts_miner": ts, "sig": kp.sign(sb).hex()}

    async def go():
        async with websockets.connect(a.ingest, open_timeout=15) as ws:
            await ws.send(json.dumps(frame, separators=(",", ":")))
            return json.loads(await asyncio.wait_for(ws.recv(), 15))
    reply = asyncio.run(go())
    print(json.dumps({"payload": payload, "reply": reply}, indent=1))
    if reply.get("kind") != "hf.receipt":
        raise SystemExit("claim refused")
    return reply


def cmd_deposit(a):
    import bittensor as bt
    if not (config.MARKETS_VAULT_COLDKEY and config.MARKETS_VAULT_HOTKEY):
        raise SystemExit("the vault is not configured for this network (SN89_MARKETS_VAULT_COLDKEY/HOTKEY)")
    amount = Decimal(a.amount)
    rao = int(amount * RAO)
    st = bt.Subtensor(network=a.network)
    w = _wallet(a)
    ent, vhk, net = w.hotkey.ss58_address, config.MARKETS_VAULT_HOTKEY, config.NETUID
    on_vault = Decimal(str(st.get_stake(coldkey_ss58=w.coldkeypub.ss58_address, hotkey_ss58=vhk, netuid=net).tao))
    if on_vault < amount:
        need = int((amount - on_vault) * RAO) + 1
        print(f"moving {Decimal(need) / RAO} alpha from {ent[:8]} to the vault hotkey {vhk[:8]}")
        _submit_extrinsic(st, w, "move_stake", {"origin_hotkey": ent, "destination_hotkey": vhk,
                                                 "origin_netuid": net, "destination_netuid": net, "alpha_amount": need})
    blk, idx = _submit_extrinsic(st, w, "transfer_stake", {
        "destination_coldkey": config.MARKETS_VAULT_COLDKEY, "hotkey": vhk, "origin_netuid": net,
        "destination_netuid": net, "alpha_amount": rao})
    print(f"transferred at block {blk} extrinsic {idx}; filing the claim")
    _claim(a, {"kind": vault.KIND_DEPOSIT, "amount_alpha": str(amount), "block": blk, "ext_index": idx})


def cmd_withdraw(a):
    _claim(a, {"kind": vault.KIND_WITHDRAW, "amount_alpha": str(Decimal(a.amount))})


def cmd_exit(a):
    _claim(a, {"kind": vault.KIND_EXIT})


def cmd_status(a):
    db = markets._db(os.path.expanduser(a.cache))
    ch = markets.CachedChain(db, markets.RpcChainView())
    ent = a.entity or _wallet(a).hotkey.ss58_address
    b = vault.balances(db, ch, int(time.time()) - 60).get(ent)
    reqs = db.execute("SELECT key, kind, requested, cp, approved, status FROM vault_requests WHERE entity=? ORDER BY cp",
                      (ent,)).fetchall()
    print(json.dumps({"entity": ent, "balance": b, "requests": reqs}, indent=1, default=str))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("deposit", "withdraw", "exit", "status"))
    p.add_argument("--amount")
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    p.add_argument("--wallet.path", dest="wallet_path", default="~/.bittensor/wallets")
    p.add_argument("--network", default=os.getenv("SN89_NETWORK", "finney"))
    p.add_argument("--ingest", default=hf.HF_INGEST_WSS)
    p.add_argument("--cache", default=os.getenv("SN89_MARKETS_GRADE_CACHE", "~/.sn89/markets-grade"))
    p.add_argument("--entity")
    a = p.parse_args()
    if a.command in ("deposit", "withdraw") and not a.amount:
        p.error("--amount is required")
    {"deposit": cmd_deposit, "withdraw": cmd_withdraw, "exit": cmd_exit, "status": cmd_status}[a.command](a)


if __name__ == "__main__":
    main()
