"""On-chain commitment helpers.

Commitment payload (hex string via set_commitment, ≤ 128 raw bytes):

    "sn89:1:" + commit_hex(64) + ":" + round(decimal) + ":" + url_tag(16)

* commit_hex — SHA256 of the signal's canonical bytes (schema.commitment)
* round     — drand round the tlock envelope opens at
* url_tag   — first 8 bytes (hex) of SHA256(blob_url); lets validators match a
              commitment to a fetched blob without trusting the URL contents

The block in which the commitment lands is T0 — the canonical signal time
(§4.1). Entry price derives from T0, never from the miner.
"""
from __future__ import annotations

import hashlib
import re
import time

import bittensor as bt

from . import config

_PREFIX = "sn89"
_V = 1
_RE = re.compile(rf"^{_PREFIX}:(\d+):([0-9a-f]{{64}}):(\d+):([0-9a-f]{{16}})$")


def url_tag(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def encode_commitment(commit_hex: str, rnd: int, blob_url: str) -> str:
    return f"{_PREFIX}:{_V}:{commit_hex}:{rnd}:{url_tag(blob_url)}"


def decode_commitment(data: str) -> dict | None:
    m = _RE.match(data.strip())
    if not m:
        return None
    v, commit_hex, rnd, tag = m.groups()
    if int(v) != _V:
        return None
    return {"commit": commit_hex, "round": int(rnd), "url_tag": tag}


class Chain:
    def __init__(self, network: str | None = None, netuid: int | None = None):
        self.netuid = netuid if netuid is not None else config.NETUID
        self.st = bt.Subtensor(network=network or config.NETWORK)

    # ── miner side ───────────────────────────────────────────────────────────
    def commit(self, wallet: "bt.Wallet", commit_hex: str, rnd: int, blob_url: str) -> bool:
        data = encode_commitment(commit_hex, rnd, blob_url)
        return self.st.set_commitment(wallet=wallet, netuid=self.netuid, data=data)

    # ── validator side ───────────────────────────────────────────────────────
    def read_all_commitments(self, block: int | None = None) -> dict[str, dict]:
        """{hotkey: decoded_commitment} for every hotkey with a valid sn89 commitment.

        NOTE: subtensor stores ONE commitment per hotkey (latest wins), so the
        validator must poll every block-ish and journal what it sees — the
        journal of (hotkey, commit, first_seen_block) is the canonical T0
        record, exactly like MANTIS arrival logging. get_all_commitments
        returns the current map; CommitmentOf storage can be queried at a
        specific block hash for backfill.
        """
        out: dict[str, dict] = {}
        try:
            raw = self.st.get_all_commitments(netuid=self.netuid, block=block)
        except TypeError:  # older SDK signature
            raw = self.st.get_all_commitments(self.netuid)
        for hotkey, data in (raw or {}).items():
            dec = decode_commitment(data) if isinstance(data, str) else None
            if dec:
                dec["hotkey"] = hotkey
                out[hotkey] = dec
        return out

    def current_block(self) -> int:
        return self.st.get_current_block()

    def block_time_unix(self, block: int) -> float:
        """Timestamp of a block via the chain's Timestamp pallet."""
        bh = self.st.get_block_hash(block)
        ts_ms = self.st.substrate.query("Timestamp", "Now", block_hash=bh).value
        return ts_ms / 1000.0

    # ── weights ──────────────────────────────────────────────────────────────
    def set_weights(self, wallet: "bt.Wallet", uids: list[int], weights: list[float]) -> bool:
        return self.st.set_weights(
            wallet=wallet, netuid=self.netuid, uids=uids, weights=weights,
            wait_for_inclusion=False)

    def metagraph(self):
        return self.st.metagraph(netuid=self.netuid)


def now_unix() -> float:
    return time.time()
