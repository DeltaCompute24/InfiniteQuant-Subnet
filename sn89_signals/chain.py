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

# Referral commitment (§ referral incentive): the RECRUITER's hotkey commits
# "sn89ref:1:<recruit_ss58>" BEFORE the recruit registers. ~60 raw bytes —
# comfortably inside the 128-byte commitment budget. The inclusion block is the
# chain-anchored proof time; scoring.valid_referral_pairs enforces
# commit_block + REFERRAL_MIN_LEAD_BLOCKS <= recruit registration block.
_REF_PREFIX = "sn89ref"
_REF_V = 1
_REF_RE = re.compile(rf"^{_REF_PREFIX}:(\d+):(5[1-9A-HJ-NP-Za-km-z]{{46,50}})$")


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
    rnd = int(rnd)
    # The round regex is `(\d+)` — unbounded. A drand round must be a positive
    # signed-64-bit integer: anything larger overflows the `round INTEGER`
    # column at ingest (an `OverflowError` that crashes the whole validator
    # loop), and even a merely far-future round would sit sealed forever,
    # re-pulling its blob every cycle. Reject out-of-range here; the exact
    # commit+REVEAL_DELAY window is enforced at ingest against the consensus-
    # exact commit-block time.
    if not 0 < rnd < 2**63:
        return None
    return {"commit": commit_hex, "round": rnd, "url_tag": tag}


def encode_referral(recruit_ss58: str) -> str:
    return f"{_REF_PREFIX}:{_REF_V}:{recruit_ss58}"


def decode_referral(data: str) -> dict | None:
    """Decode an sn89ref referral commitment, or None. The recruit address must
    be a checksum-valid ss58 AccountId (deterministic — every validator accepts
    or rejects the identical payload); a typo'd address is dropped here rather
    than journaled as a permanently-invalid referral."""
    m = _REF_RE.match((data or "").strip())
    if not m:
        return None
    v, recruit = m.groups()
    if int(v) != _REF_V:
        return None
    try:
        from scalecodec.utils.ss58 import ss58_decode
        ss58_decode(recruit)
    except Exception:  # noqa: BLE001 — bad checksum / not an AccountId
        return None
    return {"recruit": recruit}


def _decode_any(data: str | None) -> dict | None:
    """Decode a commitment payload of either kind. Signal entries keep their
    exact legacy shape plus kind="signal" (existing consumers ignore unknown
    keys); referral entries carry {kind:"referral", recruit}."""
    if not data:
        return None
    dec = decode_commitment(data)
    if dec:
        dec["kind"] = "signal"
        return dec
    ref = decode_referral(data)
    if ref:
        ref["kind"] = "referral"
        return ref
    return None


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


def _account_from_event(attrs) -> object | None:
    """Pull the committer AccountId out of a Commitments event's attributes.

    Shape varies by SDK: a dict ({'netuid':…, 'who':…}), a list/tuple of values
    ([netuid, who]), or a single value. Return the first thing that looks like an
    account (an ss58 string, or a 32-byte sequence) — _to_ss58 normalizes it.
    Defensive: returns None if nothing account-shaped is found.
    """
    def _looks_like_account(x) -> bool:
        if isinstance(x, str):
            return x.startswith("5") and len(x) >= 47
        if isinstance(x, (list, tuple)):
            seq = x
            while len(seq) == 1 and isinstance(seq[0], (list, tuple)):
                seq = seq[0]
            return len(seq) == 32 and all(isinstance(b, int) for b in seq)
        return hasattr(x, "value")

    if isinstance(attrs, dict):
        for key in ("who", "account", "account_id", "hotkey", "AccountId"):
            if key in attrs and _looks_like_account(attrs[key]):
                return attrs[key]
        for v in attrs.values():
            if _looks_like_account(v):
                return v
    elif isinstance(attrs, (list, tuple)):
        for v in attrs:
            if _looks_like_account(v):
                return v
    elif _looks_like_account(attrs):
        return attrs
    return None


class Chain:
    def __init__(self, network: str | None = None, netuid: int | None = None):
        self.netuid = netuid if netuid is not None else config.NETUID
        self.st = bt.Subtensor(network=network or config.NETWORK)
        self._archive_st = None

    def _archive(self):
        """Lazy finney-ARCHIVE subtensor for reads of state the public node has
        pruned (historic block timestamps). Created on first use; reused."""
        if self._archive_st is None:
            self._archive_st = bt.Subtensor(network="archive")
        return self._archive_st

    # ── miner side ───────────────────────────────────────────────────────────
    def commit(self, wallet: "bt.Wallet", commit_hex: str, rnd: int, blob_url: str) -> bool:
        data = encode_commitment(commit_hex, rnd, blob_url)
        # inclusion is sufficient — entry T0 = the inclusion block (docs/entry-timing.md
        # §2.1). Returning at inclusion (~1 block) instead of finalization (~2 blocks)
        # unblocks the per-tenant commit loop, cutting cross-tenant serialization latency.
        return self.st.set_commitment(wallet=wallet, netuid=self.netuid, data=data,
                                      wait_for_finalization=False)

    def commit_referral(self, wallet: "bt.Wallet", recruit_ss58: str) -> bool:
        """Commit a referral claim for `recruit_ss58`, signed by the RECRUITER's
        hotkey (this wallet). MUST be placed before the recruit registers —
        scoring requires commit_block + REFERRAL_MIN_LEAD_BLOCKS <= reg block.

        ⚠ CommitmentOf is one latest-wins slot per hotkey and the validator
        polls every POLL_INTERVAL_S: don't call this within ~90s of this
        hotkey's last signal commit (either commitment could go unobserved),
        and hold the next signal commit ~90s. See neurons/miner.py `refer`.
        """
        data = encode_referral(recruit_ss58)
        return self.st.set_commitment(wallet=wallet, netuid=self.netuid, data=data,
                                      wait_for_finalization=False)

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
            dec = _decode_any(data)
            if not dec:
                continue
            hk = _to_ss58(hotkey)
            dec["hotkey"] = hk
            dec["commit_block"] = int(v.get("block") or 0)
            out[hk] = dec
        return out

    def read_commitments_in_block_range(self, from_block: int, to_block: int) -> list[dict]:
        """EXPERIMENTAL (audit #5) — soak on testnet before mainnet use.

        Scan blocks (from_block, to_block] for commitment-setting activity so a
        commitment a miner OVERWROTE between snapshot polls isn't lost (the
        cross-validator split-brain: validators that polled at different phases
        otherwise latch different commitments for the same hotkey and grade
        different signals). For each block we read the chain events, find events
        on the Commitments pallet, and re-read the committer's CommitmentOf AT
        THAT BLOCK — capturing each distinct commitment with its consensus-exact
        inclusion block. Returns decoded dicts shaped like
        read_all_commitments_with_block() values.

        Best-effort and defensive: the per-runtime event identifier for
        set_commitment is matched loosely (Commitments pallet), and any block /
        event / query that can't be parsed is skipped rather than aborting the
        scan. The scan is bounded to the most recent SCAN_MAX_BLOCKS_PER_POLL
        blocks so a long gap can't trigger an unbounded backfill (the latest
        snapshot still covers the gap's final state). MUST be validated against a
        live chain before SN89_SCAN_COMMITMENT_HISTORY is enabled on mainnet.
        """
        out: list[dict] = []
        start = max(from_block + 1, to_block - config.SCAN_MAX_BLOCKS_PER_POLL + 1)
        for block in range(start, to_block + 1):
            try:
                bh = self.st.get_block_hash(block)
                events = self.st.substrate.get_events(bh)
            except Exception:  # noqa: BLE001 — skip unreadable block, never abort
                continue
            committers: set = set()
            for ev in events or []:
                try:
                    e = ev.value if hasattr(ev, "value") else ev
                    inner = e.get("event", e) if isinstance(e, dict) else {}
                    module = str(inner.get("module_id") or inner.get("module") or "")
                    if "commitment" not in module.lower():
                        continue
                    acct = _account_from_event(inner.get("attributes"))
                    if acct is not None:
                        committers.add(acct)
                except Exception:  # noqa: BLE001
                    continue
            for acct in committers:
                try:
                    reg = self.st.substrate.query(
                        "Commitments", "CommitmentOf", [self.netuid, acct], block_hash=bh)
                    v = reg.value if hasattr(reg, "value") else reg
                    if not isinstance(v, dict):
                        continue
                    data = _extract_raw_str(v.get("info"))
                    dec = _decode_any(data)
                    if not dec:
                        continue
                    dec["hotkey"] = _to_ss58(acct)
                    dec["commit_block"] = int(v.get("block") or block)
                    out.append(dec)
                except Exception:  # noqa: BLE001
                    continue
        return out

    def current_block(self) -> int:
        return self.st.get_current_block()

    def block_time_ms(self, block: int) -> int:
        """Millisecond timestamp of a block via the chain's Timestamp pallet.

        The public finney node prunes historical state, so a commitment whose
        inclusion block predates the pruning window raises "State discarded".
        Fall back to a finney ARCHIVE node for those reads - it returns the
        same consensus Timestamp.Now the canonical chain recorded at that
        block. (Testnet uses a retaining relay node, so the fallback is
        finney-only.)"""
        try:
            bh = self.st.get_block_hash(block)
            return int(self.st.substrate.query("Timestamp", "Now", block_hash=bh).value)
        except Exception:
            if "test" in str(config.NETWORK):
                raise
            st = self._archive()
            bh = st.get_block_hash(block)
            return int(st.substrate.query("Timestamp", "Now", block_hash=bh).value)

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

    def set_mechanism_weights(
        self, wallet: "bt.Wallet", mecid: int, uids: list[int], weights: list[float]
    ) -> tuple[bool, str]:
        """Commit a sub-mechanism (mecid) weight vector via the timelocked
        commit-reveal path. Commit-reveal is enabled on SN89, so a plain
        set_mechanism_weights is rejected (CommitRevealEnabled) -- and the tlock
        commit MUST bind to get_mechid_storage_index(netuid, mecid), not the bare
        netuid, or it is accepted but never reveals. The SDK extrinsic handles both;
        this wraps it so the validator sets mecid-1 the same way it sets mecid-0."""
        from bittensor.core.extrinsics.weights import commit_timelocked_weights_extrinsic
        resp = commit_timelocked_weights_extrinsic(
            subtensor=self.st, wallet=wallet, netuid=self.netuid, mechid=mecid,
            uids=uids, weights=weights, block_time=12.0, mev_protection=False,
            wait_for_inclusion=True, wait_for_finalization=False,
            wait_for_revealed_execution=False)
        if hasattr(resp, "success"):
            return bool(resp.success), str(getattr(resp, "message", "") or "")
        if isinstance(resp, tuple):
            return bool(resp[0]), str(resp[1]) if len(resp) > 1 else ""
        return bool(resp), ""

    def metagraph(self):
        return self.st.metagraph(netuid=self.netuid)

    def weights_for_uid(self, uid: int, block: int | None = None) -> "dict[int, float] | None":
        """The on-chain weight vector a validator `uid` set on this subnet, as
        {miner_uid: normalized_weight} summing to 1, read from SubtensorModule.Weights
        (the actual chain state — NOT the metagraph, which doesn't load weights by
        default). Returns None if unset/unreadable. Read-only; used by the audit
        (docs/single-validator-model.md) to compare a validator's on-chain weights to
        a replay of its published journal."""
        try:
            bh = self.st.get_block_hash(block) if block else None
            raw = self.st.substrate.query("SubtensorModule", "Weights",
                                          [self.netuid, uid], block_hash=bh)
            pairs = getattr(raw, "value", None)
            if not pairs:
                return None
            total = sum(int(w) for _, w in pairs) or 1
            return {int(u): int(w) / total for u, w in pairs}
        except Exception:  # noqa: BLE001 — audit helper, never raise
            return None

    def uid_of(self, hotkey_ss58: str, mg=None) -> "int | None":
        """Metagraph uid for a hotkey on this subnet, or None if unregistered."""
        mg = mg or self.metagraph()
        for i, hk in enumerate(mg.hotkeys):
            if hk == hotkey_ss58:
                return i
        return None

    def commitment_at_block(self, hotkey_ss58: str, block: int) -> "str | None":
        """The commit_hex a hotkey had in on-chain CommitmentOf AT `block`, or None
        if unreadable (e.g. the state was pruned — needs an archive node for old
        blocks). Lets the audit confirm each journaled signal is anchored to a real
        on-chain commitment (no fabricated signals). Read-only; best-effort — mirrors
        the per-committer read in read_commitments_in_block_range. CommitmentOf is
        latest-wins, so reading at a signal's own inclusion block returns that
        signal's commitment."""
        try:
            bh = self.st.get_block_hash(block)
            reg = self.st.substrate.query(
                "Commitments", "CommitmentOf", [self.netuid, hotkey_ss58], block_hash=bh)
            v = reg.value if hasattr(reg, "value") else reg
            if not isinstance(v, dict):
                return None
            data = _extract_raw_str(v.get("info"))
            dec = decode_commitment(data) if data else None
            return dec.get("commit") if dec else None
        except Exception:  # noqa: BLE001 — audit helper, never raise
            return None

    def referral_at_block(self, recruiter_ss58: str, block: int) -> "str | None":
        """The recruit ss58 the recruiter's CommitmentOf held AT `block`, or None
        if unreadable / not a referral payload. Audit helper mirroring
        commitment_at_block — lets --referral-anchors confirm each journaled
        referral is anchored to a real on-chain sn89ref commitment."""
        try:
            bh = self.st.get_block_hash(block)
            reg = self.st.substrate.query(
                "Commitments", "CommitmentOf", [self.netuid, recruiter_ss58],
                block_hash=bh)
            v = reg.value if hasattr(reg, "value") else reg
            if not isinstance(v, dict):
                return None
            data = _extract_raw_str(v.get("info"))
            dec = decode_referral(data) if data else None
            return dec.get("recruit") if dec else None
        except Exception:  # noqa: BLE001 — audit helper, never raise
            return None

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
