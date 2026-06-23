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


def _extract_raw_str(info) -> str | None:
    """Pull the raw commitment string out of a CommitmentOf `info` struct.

    Storage shape: Registration{ deposit, block, info: { fields: [[{"Raw70":
    "0x736e38..."}]] } } — the Raw<N> variant name and nesting depth vary by
    SDK version, so walk the structure and hex-decode the first Raw* leaf.
    """
    if isinstance(info, str):
        return info
    if isinstance(info, dict):
        for k, v in info.items():
            if isinstance(k, str) and k.startswith("Raw"):
                if isinstance(v, str):
                    h = v[2:] if v.startswith("0x") else v
                    try:
                        return bytes.fromhex(h).decode()
                    except (ValueError, UnicodeDecodeError):
                        return None
                if isinstance(v, (bytes, bytearray)):
                    return bytes(v).decode(errors="replace")
                if isinstance(v, (list, tuple)):  # ints, or nested [[ints]]
                    # Some SDK builds wrap the byte list one or more levels deep
                    # (e.g. RawN: [[115, 110, ...]]); unwrap single-element
                    # nesting down to the flat int sequence before decoding.
                    seq = v
                    while len(seq) == 1 and isinstance(seq[0], (list, tuple)):
                        seq = seq[0]
                    try:
                        return bytes(seq).decode(errors="replace")
                    except (ValueError, TypeError):
                        return None
            else:
                r = _extract_raw_str(v)
                if r is not None:
                    return r
    if isinstance(info, (list, tuple)):
        for item in info:
            r = _extract_raw_str(item)
            if r is not None:
                return r
    return None


def _to_ss58(key) -> str:
    """Normalize a query_map AccountId key to an ss58 string. Raw substrate keys
    arrive as a (possibly nested) tuple of 32 bytes; encode to ss58 so the hotkey
    matches metagraph.hotkeys. SDK-decoded keys already carry .value / are str."""
    if hasattr(key, "value"):
        key = key.value
    if isinstance(key, str):
        return key
    if isinstance(key, (list, tuple)):
        seq = key
        while len(seq) == 1 and isinstance(seq[0], (list, tuple)):
            seq = seq[0]
        try:
            from scalecodec.utils.ss58 import ss58_encode
            return ss58_encode(bytes(seq), 42)
        except Exception:  # noqa: BLE001
            return str(key)
    return str(key)


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
        validator must poll every block-ish and journal what it sees. T0 is the
        commitment's INCLUSION block — use read_all_commitments_with_block()
        (which carries `commit_block` from raw storage) for ingestion; this
        SDK-decoded variant remains for tools that only need the payload.
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

    def read_all_commitments_with_block(self) -> dict[str, dict]:
        """Like read_all_commitments, but sourced from raw CommitmentOf storage
        so each entry carries `commit_block` — the exact inclusion block of the
        latest set_commitment (docs/entry-timing.md §2.1).

        commit_block is consensus-exact: every validator reads the identical
        value regardless of poll phase, so T0 (and the entry price derived from
        it) is identical across validators. The bittensor SDK's
        get_all_commitments() strips this field, hence the raw query_map.
        """
        out: dict[str, dict] = {}
        qm = self.st.substrate.query_map("Commitments", "CommitmentOf", [self.netuid])
        for hotkey, reg in qm:
            v = reg.value if hasattr(reg, "value") else reg
            if not isinstance(v, dict):
                continue
            data = _extract_raw_str(v.get("info"))
            dec = decode_commitment(data) if data else None
            if not dec:
                continue
            hk = _to_ss58(hotkey)
            dec["hotkey"] = hk
            dec["commit_block"] = int(v.get("block") or 0)
            out[hk] = dec
        return out

    def current_block(self) -> int:
        return self.st.get_current_block()

    def block_time_ms(self, block: int) -> int:
        """Millisecond timestamp of a block via the chain's Timestamp pallet."""
        bh = self.st.get_block_hash(block)
        return int(self.st.substrate.query("Timestamp", "Now", block_hash=bh).value)

    def block_time_unix(self, block: int) -> float:
        """Timestamp of a block via the chain's Timestamp pallet."""
        return self.block_time_ms(block) / 1000.0

    # ── weights ──────────────────────────────────────────────────────────────
    def set_weights(
        self, wallet: "bt.Wallet", uids: list[int], weights: list[float]
    ) -> tuple[bool, str]:
        """Commit weights; return (success, message).

        bittensor ≥ 9 returns an `ExtrinsicResponse` (success + message, no
        `__bool__` → always truthy if treated as a flag), older SDKs return a
        `(success, message)` tuple, and very old ones a bare bool. Normalize all
        three so the caller can branch on real success and SURFACE the chain's
        rejection reason — the actual reason a commit fails (no permit,
        TooManyUnrevealedCommits, rate limit) lives in that message.
        """
        resp = self.st.set_weights(
            wallet=wallet, netuid=self.netuid, uids=uids, weights=weights,
            wait_for_inclusion=False)
        if hasattr(resp, "success"):              # ExtrinsicResponse
            return bool(resp.success), str(getattr(resp, "message", "") or "")
        if isinstance(resp, tuple):               # (success, message)
            ok = bool(resp[0])
            return ok, str(resp[1]) if len(resp) > 1 else ""
        return bool(resp), ""                      # bare bool

    def metagraph(self):
        return self.st.metagraph(netuid=self.netuid)

    def has_validator_permit(self, hotkey_ss58: str, mg=None) -> bool | None:
        """Whether `hotkey_ss58` holds a validator permit on this subnet.

        Returns None if the hotkey is not registered here. A hotkey WITHOUT a
        permit can still submit weight commits, but the chain will never reveal
        them — they accumulate until `TooManyUnrevealedCommits` and the validator
        earns nothing. Pass an already-fetched `mg` to avoid a second query.
        """
        mg = mg if mg is not None else self.metagraph()
        if hotkey_ss58 not in mg.hotkeys:
            return None
        return bool(mg.validator_permit[mg.hotkeys.index(hotkey_ss58)])


def now_unix() -> float:
    return time.time()
