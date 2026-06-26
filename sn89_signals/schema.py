"""Signal payload schema, canonicalization, and validation.

The canonical byte encoding of a signal is JSON with sorted keys and no
whitespace (`canonical_bytes`). The on-chain commitment is
SHA256(canonical_bytes); the random `nonce` field inside the signal acts as
the hiding salt, so identical setups from different miners (or the same miner
twice) never produce the same commitment.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, asdict, field

from . import config

VALID_DIRECTIONS = ("LONG", "SHORT")
SCHEMA_VERSION = 1


@dataclass
class Signal:
    trade_pair: str
    direction: str
    tp_bps: float
    sl_bps: float
    ts_miner: int                       # miner wall-clock ms (informational only —
                                        # entry derives from the commit block, §6.2)
    hotkey: str
    asset_class: str = ""
    horizon_h: int = config.DEFAULT_HORIZON_H
    comment: str = ""
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    v: int = SCHEMA_VERSION

    # ── canonical form ───────────────────────────────────────────────────────
    def canonical_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def commitment(self) -> bytes:
        """32-byte on-chain commitment."""
        return hashlib.sha256(self.canonical_bytes()).digest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Signal":
        d = json.loads(raw.decode("utf-8"))
        return cls(**d)


class ValidationError(ValueError):
    pass


def validate(sig: Signal, bands: dict | None = None,
             t0_unix: float | None = None) -> Signal:
    """Structural + board validation. Raises ValidationError with a
    miner-actionable message. Does NOT check cooldowns/spacing — those are
    stateful and live in the validator/grader (and the same logic voids at
    reveal, so passing here is necessary but not sufficient).

    `t0_unix` (the commitment's consensus-exact inclusion time) resolves the
    board version in force AT COMMIT, so a board update between commit and reveal
    never retroactively fails an in-flight signal that was valid when committed.
    """
    if bands is None:
        bands = (config.allowed_assets_as_of(t0_unix) if t0_unix is not None
                 else config.allowed_assets())

    if sig.v != SCHEMA_VERSION:
        raise ValidationError(f"unsupported schema version {sig.v}")
    if sig.direction not in VALID_DIRECTIONS:
        raise ValidationError(f"direction must be one of {VALID_DIRECTIONS}")
    if not sig.hotkey or not sig.hotkey.startswith("5"):
        raise ValidationError("hotkey must be an SS58 address")
    if len(sig.comment or "") > 280:
        raise ValidationError("comment > 280 chars")
    if len(sig.nonce) < 16:
        raise ValidationError("nonce too short")

    band = bands.get(sig.trade_pair)
    if band is None:
        raise ValidationError(
            f"{sig.trade_pair} is not on the board; allowed: {sorted(bands.keys())}")

    # Board class is canonical; overwrite whatever the miner claimed.
    sig.asset_class = band.get("asset_class", sig.asset_class)

    # Horizon (wash window) is class-fixed, not miner-chosen — normalize it to
    # the asset's class window so the miner's Nh token can't change grading.
    sig.horizon_h = config.class_horizon_h(sig.asset_class)

    # 1:1 brackets at exactly the board band (program rule — fixed bands,
    # not miner-chosen, same as today's bot).
    want_tp = float(band["tp_bps"])
    want_sl = float(band["sl_bps"])
    if float(sig.tp_bps) != want_tp or float(sig.sl_bps) != want_sl:
        raise ValidationError(
            f"{sig.trade_pair} band is fixed at tp={want_tp}/sl={want_sl} bps "
            f"(got {sig.tp_bps}/{sig.sl_bps})")

    return sig
