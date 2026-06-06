"""Validity filters + weight computation (SPEC §6.4, §7).

Pure functions over journaled state so every validator computes identical
weights from identical inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass
class GradedRow:
    hotkey: str
    trade_pair: str
    direction: str
    t0_unix: float          # commit-block time (canonical)
    status: str             # won | lost | washed | void | pending
    void_reason: str | None = None


# ── validity (deterministic re-derivation of the live gateway checks) ─────────
def apply_validity_filters(rows: list[GradedRow]) -> list[GradedRow]:
    """Voids rows in-place per §6.4 and returns the list (ordered by t0, then
    hotkey as the same-block tiebreak — callers must pre-sort identically).

    Voided rows: not graded, don't count toward quotas, no strike.
    """
    rows = sorted(rows, key=lambda r: (r.t0_unix, r.hotkey))
    last_pair_dir: dict[tuple[str, str], float] = {}        # plagiarism cooldown
    per_hotkey_times: dict[str, list[float]] = {}           # spacing + daily quota
    open_pair_dir: dict[tuple[str, str, str], float] = {}   # self-overlap

    for r in rows:
        if r.status == "void":
            continue
        key = (r.trade_pair, r.direction)

        # 15-min cross-miner cooldown — first commit wins (§6.4 CONFIRMED)
        prev = last_pair_dir.get(key)
        if prev is not None and r.t0_unix - prev < config.PLAGIARISM_COOLDOWN_S:
            r.status, r.void_reason = "void", "plagiarism_cooldown"
            continue

        # per-hotkey spacing ≥ 4h
        times = per_hotkey_times.setdefault(r.hotkey, [])
        if times and r.t0_unix - times[-1] < config.MIN_SPACING_S:
            r.status, r.void_reason = "void", "min_spacing"
            continue

        # ≤ 3 per UTC day
        day = int(r.t0_unix // 86_400)
        if sum(1 for t in times if int(t // 86_400) == day) >= config.MAX_SIGNALS_PER_UTC_DAY:
            r.status, r.void_reason = "void", "daily_quota"
            continue

        # no duplicate open (hotkey, pair, direction)
        okey = (r.hotkey, r.trade_pair, r.direction)
        open_until = open_pair_dir.get(okey)
        if open_until is not None and r.t0_unix < open_until:
            r.status, r.void_reason = "void", "overlapping_open"
            continue

        # accepted
        last_pair_dir[key] = r.t0_unix
        times.append(r.t0_unix)
        open_pair_dir[okey] = r.t0_unix + config.MAX_HORIZON_H * 3600

    return rows


# ── weights (§7.2 CONFIRMED: gate → pro-rata wins, trailing 8 days) ───────────
@dataclass
class MinerState:
    hotkey: str
    uid: int
    first_seen_unix: float   # first commit observed (immunity clock)
    lifetime_decisive: int
    trailing_wins: int       # decisive WONs inside SCORE_WINDOW_S
    trailing_decisive: int


def compute_weights(states: list[MinerState], now_unix: float,
                    burn_uid: int = config.BURN_UID) -> dict[int, float]:
    """{uid: normalized_weight}. Immune miners get the dust floor; qualified
    miners split the rest pro-rata by trailing-window wins; leftovers burn.
    """
    weights: dict[int, float] = {}

    immune = [s for s in states
              if now_unix - s.first_seen_unix < config.IMMUNITY_S]
    for s in immune:
        weights[s.uid] = config.DUST_WEIGHT

    qualified = [
        s for s in states
        if s.lifetime_decisive >= config.QUALIFY_MIN_DECISIVE
        and s.trailing_decisive > 0
        and (s.trailing_wins / s.trailing_decisive) >= config.QUALIFY_MIN_HIT
        and s.trailing_wins > 0
    ]

    budget = 1.0 - sum(weights.values())
    total_wins = sum(s.trailing_wins for s in qualified)
    if total_wins > 0 and budget > 0:
        for s in qualified:
            weights[s.uid] = weights.get(s.uid, 0.0) + budget * (s.trailing_wins / total_wins)
    else:
        weights[burn_uid] = weights.get(burn_uid, 0.0) + max(budget, 0.0)

    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()} if total > 0 else {burn_uid: 1.0}
