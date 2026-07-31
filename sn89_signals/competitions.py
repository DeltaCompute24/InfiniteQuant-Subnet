"""Unified multi-competition weighting — N competitions, ONE on-chain mechanism.

The chain caps a subnet at 2 mechanisms (MaxMechanismCount = 2) and netuid 89
already runs both, so a third competition cannot take a mechanism slot. Instead
every competition (LF, HF, Closers, and any future fourth) produces its OWN
normalized weight vector over the SAME miner population, and this module blends
them into the single vector the validator commits:

    combined[uid] = Σ_c  share_c · vector_c[uid]

Two properties are load-bearing:

  * NORMALIZE-THEN-WEIGHT. Each competition's vector is normalized within its
    own participant set BEFORE the share is applied, so a specialist in one
    competition receives exactly share_c × their share of that competition —
    identical to what a dedicated mechanism at that emission split would pay.
    Weighting RAW scores instead would let the metrics' natural scales decide
    the split, which silently starves whichever competition runs smaller
    numbers. That is the one implementation choice that makes "for the miners
    it's the same" true.

  * A DEAD COMPETITION BURNS ITS OWN SHARE. If a competition's compute fails or
    returns nothing, its share_c goes to the burn UID rather than being
    redistributed — redistribution would let an attacker who can stall one
    competition's feed inflate every other competition's payout.

The shares are consensus constants (config.COMP_WEIGHTS, committed to master and
announced like a band change) — after the merge the chain no longer enforces the
split, so the code, the repo and the journal are the only public record of it.
"""
from __future__ import annotations

from typing import Callable

from . import config

# A competition is just (key, compute) — compute() -> {uid: weight}, normalized,
# burn included. Shares live in config.COMP_WEIGHTS keyed by the same key, so
# adding a fourth competition is one registry entry plus one share entry.
Compute = Callable[[], dict[int, float]]


def parse_shares(spec: str) -> dict[str, float]:
    """Parse "lf:0.375,hf:0.375,closers:0.25" -> {key: share}. Shares must be
    positive and are renormalized to sum exactly 1.0 so a hand-edited spec that
    sums to 0.99 cannot silently leak emission."""
    out: dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, val = part.partition(":")
        share = float(val)
        if share < 0:
            raise ValueError(f"negative share for {key!r}")
        if key.strip() in out:
            raise ValueError(f"duplicate competition key {key!r}")
        out[key.strip()] = share
    total = sum(out.values())
    if total <= 0:
        raise ValueError(f"competition shares sum to {total}")
    return {k: v / total for k, v in out.items()}


def _normalized(vec: dict[int, float]) -> dict[int, float] | None:
    """Defensive renormalization. Every compute() already returns a normalized
    vector (scoring.compute_weights ends by dividing by the total), but the
    combined commit is the subnet's entire emission — verify rather than trust.
    Returns None for an empty/degenerate vector so the caller burns its share."""
    if not vec:
        return None
    clean = {int(u): float(w) for u, w in vec.items() if w > 0}
    total = sum(clean.values())
    if total <= 0:
        return None
    return {u: w / total for u, w in clean.items()}


def combine(vectors: dict[str, dict[int, float] | None],
            shares: dict[str, float] | None = None,
            burn_uid: int = config.BURN_UID) -> dict[int, float]:
    """Blend per-competition vectors into the single on-chain vector.

    vectors: {competition_key: {uid: weight} | None}. None (or an empty/
             degenerate vector) means the competition could not be computed this
             cycle — its share burns.
    shares:  {competition_key: share}, defaults to config.COMP_WEIGHTS. Keys in
             `shares` with no vector burn; vectors with no share are IGNORED
             (share 0) so a competition can be staged dark before its share is
             granted — the reverse of a silent hot-launch.
    """
    shares = shares if shares is not None else config.COMP_WEIGHTS
    combined: dict[int, float] = {}
    for key, share in shares.items():
        if share <= 0:
            continue
        vec = _normalized(vectors.get(key) or {})
        if vec is None:
            combined[burn_uid] = combined.get(burn_uid, 0.0) + share
            continue
        for uid, w in vec.items():
            combined[uid] = combined.get(uid, 0.0) + share * w
    total = sum(combined.values())
    if total <= 0:
        return {burn_uid: 1.0}
    return {u: w / total for u, w in combined.items()}
