"""Caller collateral — vault custody, EVM ledger, burn-on-elimination.

Architecture (modeled on the SN8 Taoshi/Vanta collateral system):

  deposit   The caller's coldkey signs a SubtensorModule.transfer_stake moving
            SN89 alpha from their hotkey onto the subnet vault coldkey. The
            custodian (subnet owner) validates and submits that extrinsic, then
            credits the EVM ledger (contracts/Collateral.sol) under the
            caller's hotkey. Anyone can audit balances via balanceOf.

  slash     On elimination (scoring.elimination_t0) the custodian debits the
            ledger and burns the same amount of alpha from the vault stake via
            SubtensorModule.burn_alpha — destruction is verifiable on-chain.

  withdraw  The caller's coldkey signs a withdraw request (canonical JSON, see
            withdraw_request_message). The custodian verifies the signature,
            that the coldkey owns the hotkey, and the settlement lock
            (no open signals, cooldown elapsed, not eliminated), then debits
            the ledger and transfer_stakes the alpha back.

Validators only ever READ the ledger (CollateralLedger) — they hold no EVM or
vault keys. All key-bearing operations live in the custodian
(neurons/custodian.py) and take keys as arguments; nothing here persists them.

Ledger account = first 20 bytes of the hotkey's 32-byte AccountId (standard
subtensor ss58 → H160 truncation). Amounts are rao of SN89 alpha.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import config

RAO_PER_ALPHA = 10**9

# Minimal ABI for contracts/Collateral.sol — reads + owner writes.
LEDGER_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "getTotalCollateral", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "getSlashedCollateral", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "deposit", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "account", "type": "address"},
                {"name": "amount", "type": "uint256"}], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "account", "type": "address"},
                {"name": "amount", "type": "uint256"}], "outputs": []},
    {"name": "slash", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "account", "type": "address"},
                {"name": "amount", "type": "uint256"}], "outputs": []},
]


def ss58_to_h160(ss58: str) -> str:
    """EVM ledger key for a hotkey: first 20 bytes of the 32-byte AccountId."""
    from scalecodec.utils.ss58 import ss58_decode
    from web3 import Web3
    account_id_hex = ss58_decode(ss58)
    return Web3.to_checksum_address("0x" + account_id_hex[:40])


# ── ledger (read-only — validators) ──────────────────────────────────────────
class CollateralLedger:
    """Read-only view of the on-chain ledger. Safe to construct everywhere;
    `enabled` is False until SN89_COLLATERAL_CONTRACT is configured, and every
    read raises RuntimeError when disabled."""

    def __init__(self, contract_address: str | None = None,
                 endpoint: str | None = None):
        self.address = (contract_address if contract_address is not None
                        else config.COLLATERAL_CONTRACT)
        self.endpoint = endpoint or config.EVM_ENDPOINT
        self._contract = None

    @property
    def enabled(self) -> bool:
        return bool(self.address)

    def _c(self):
        if not self.enabled:
            raise RuntimeError("collateral ledger not configured "
                               "(set SN89_COLLATERAL_CONTRACT)")
        if self._contract is None:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self.endpoint))
            self._contract = w3.eth.contract(
                Web3.to_checksum_address(self.address), abi=LEDGER_ABI)
        return self._contract

    def balance_of(self, hotkey_ss58: str) -> int:
        """Posted collateral for a hotkey, in rao."""
        return int(self._c().functions.balanceOf(ss58_to_h160(hotkey_ss58)).call())

    def total(self) -> int:
        return int(self._c().functions.getTotalCollateral().call())

    def slashed(self) -> int:
        return int(self._c().functions.getSlashedCollateral().call())


# ── extrinsic helpers ────────────────────────────────────────────────────────
def create_stake_transfer_extrinsic(st, wallet, hotkey_ss58: str,
                                    amount_rao: int, dest_coldkey: str,
                                    netuid: int | None = None,
                                    wallet_password: str | None = None):
    """Compose + coldkey-sign a transfer_stake of `amount_rao` alpha staked on
    hotkey_ss58 to `dest_coldkey` (same hotkey, same netuid). Used for both
    the caller deposit (dest = vault) and the custodian's withdrawal return
    (dest = caller coldkey). Nothing is submitted here.
    """
    if not dest_coldkey:
        raise ValueError("destination coldkey required")
    if amount_rao <= 0:
        raise ValueError(f"amount must be positive: {amount_rao}")
    nu = netuid if netuid is not None else config.NETUID
    call = st.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="transfer_stake",
        call_params={
            "destination_coldkey": dest_coldkey,
            "hotkey": hotkey_ss58,
            "origin_netuid": nu,
            "destination_netuid": nu,
            "alpha_amount": amount_rao,
        },
    )
    keypair = (wallet.get_coldkey(wallet_password) if wallet_password
               else wallet.coldkey)
    return st.substrate.create_signed_extrinsic(call=call, keypair=keypair)


def create_deposit_extrinsic(st, wallet, hotkey_ss58: str, amount_rao: int,
                             vault_coldkey: str | None = None,
                             netuid: int | None = None,
                             wallet_password: str | None = None):
    """Caller side: the deposit transfer_stake (destination = vault). Ship its
    hex (encode_extrinsic) to the custodian."""
    vault = vault_coldkey or config.VAULT_COLDKEY
    if not vault:
        raise ValueError("vault coldkey not configured (set SN89_VAULT_COLDKEY)")
    return create_stake_transfer_extrinsic(
        st, wallet, hotkey_ss58, amount_rao, vault, netuid, wallet_password)


def encode_extrinsic(extrinsic) -> str:
    return bytes(extrinsic.data.data).hex()


def decode_extrinsic(st, data_hex: str):
    from scalecodec import ScaleBytes
    from scalecodec.types import GenericExtrinsic
    ext = GenericExtrinsic(
        data=ScaleBytes(bytearray.fromhex(data_hex.removeprefix("0x"))),
        metadata=st.substrate.metadata,
        runtime_config=st.substrate.runtime_config,
    )
    ext.decode()
    return ext


def validate_deposit_extrinsic(extrinsic, vault_coldkey: str,
                               netuid: int) -> tuple[str, int]:
    """Custodian side: assert the decoded extrinsic is exactly a
    transfer_stake of SN89 alpha to OUR vault, and return
    (origin_hotkey, alpha_amount_rao). Raises ValueError on any mismatch."""
    call = extrinsic.value["call"]
    if (call["call_module"] != "SubtensorModule"
            or call["call_function"] != "transfer_stake"):
        raise ValueError(
            f"expected SubtensorModule.transfer_stake, got "
            f"{call['call_module']}.{call['call_function']}")
    args = {a["name"]: a["value"] for a in call["call_args"]}
    if args["destination_coldkey"] != vault_coldkey:
        raise ValueError("destination is not the vault coldkey")
    if int(args["origin_netuid"]) != netuid or int(args["destination_netuid"]) != netuid:
        raise ValueError(f"netuid mismatch (want {netuid})")
    amount = int(args["alpha_amount"])
    if amount <= 0:
        raise ValueError("non-positive amount")
    return str(args["hotkey"]), amount


def stake_added_rao(events: list[Any]) -> int:
    """Amount actually credited by the chain, from the StakeAdded event of a
    submitted transfer_stake receipt."""
    for ev in events:
        e = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "value", {}).get("event", {})
        if e.get("event_id") == "StakeAdded":
            attrs = e.get("attributes")
            # attributes shape: (coldkey, hotkey, amount_rao, ...) — varies by
            # SDK version; the first integer attribute is the rao amount.
            seq = attrs.values() if isinstance(attrs, dict) else attrs
            for a in seq:
                if isinstance(a, int):
                    return a
    raise ValueError("no StakeAdded event in receipt")


def create_burn_alpha_extrinsic(st, vault_wallet, hotkey_ss58: str,
                                amount_rao: int, netuid: int | None = None,
                                wallet_password: str | None = None):
    """Custodian side: compose + vault-coldkey-sign the burn that destroys
    `amount_rao` of alpha staked on `hotkey_ss58` (the vault stake hotkey)."""
    if amount_rao <= 0:
        raise ValueError(f"amount must be positive: {amount_rao}")
    call = st.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="burn_alpha",
        call_params={
            "netuid": netuid if netuid is not None else config.NETUID,
            "hotkey": hotkey_ss58,
            "amount": amount_rao,
        },
    )
    keypair = (vault_wallet.get_coldkey(wallet_password) if wallet_password
               else vault_wallet.coldkey)
    return st.substrate.create_signed_extrinsic(call=call, keypair=keypair)


# ── withdraw requests (caller-signed) ────────────────────────────────────────
def withdraw_request_message(amount_rao: int, coldkey: str, hotkey: str,
                             nonce: str, timestamp_ms: int) -> bytes:
    """Canonical bytes the caller's coldkey signs: sorted-key JSON, no spaces.
    Same scheme SN8 uses for its /collateral/withdraw endpoint."""
    return json.dumps(
        {"amount_rao": int(amount_rao), "coldkey": coldkey, "hotkey": hotkey,
         "nonce": str(nonce), "timestamp_ms": int(timestamp_ms)},
        sort_keys=True, separators=(",", ":")).encode()


def verify_withdraw_request(req: dict, max_age_s: int = 600) -> Optional[str]:
    """Returns None if the request is valid, else a rejection reason.

    req: {amount_rao, coldkey, hotkey, nonce, timestamp_ms, signature}
    Checks sr25519 signature by the coldkey and request freshness. Replay
    protection beyond the window (nonce journal) and coldkey-owns-hotkey are
    the custodian's responsibility — they need chain/DB access.
    """
    try:
        msg = withdraw_request_message(req["amount_rao"], req["coldkey"],
                                       req["hotkey"], req["nonce"],
                                       req["timestamp_ms"])
        sig = bytes.fromhex(str(req["signature"]).removeprefix("0x"))
    except (KeyError, ValueError) as e:
        return f"malformed request: {e}"
    if abs(time.time() * 1000 - int(req["timestamp_ms"])) > max_age_s * 1000:
        return "stale timestamp"
    from bittensor import Keypair
    try:
        if not Keypair(ss58_address=req["coldkey"]).verify(msg, sig):
            return "bad signature"
    except Exception as e:  # noqa: BLE001 — invalid ss58 etc.
        return f"verify failed: {e}"
    return None


# ── EVM owner writes (custodian only) ────────────────────────────────────────
def ledger_write(fn_name: str, hotkey_ss58: str, amount_rao: int,
                 owner_address: str, owner_private_key: str,
                 contract_address: str | None = None,
                 endpoint: str | None = None) -> str:
    """Sign + send an owner-only ledger mutation (deposit/withdraw/slash).
    Returns the tx hash hex. Raises on revert."""
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(endpoint or config.EVM_ENDPOINT))
    contract = w3.eth.contract(
        Web3.to_checksum_address(contract_address or config.COLLATERAL_CONTRACT),
        abi=LEDGER_ABI)
    fn = contract.functions[fn_name](ss58_to_h160(hotkey_ss58), int(amount_rao))
    tx = fn.build_transaction({
        "from": Web3.to_checksum_address(owner_address),
        "nonce": w3.eth.get_transaction_count(
            Web3.to_checksum_address(owner_address), "pending"),
        "gas": 200_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=owner_private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"{fn_name} reverted: {tx_hash.hex()}")
    return tx_hash.hex()
