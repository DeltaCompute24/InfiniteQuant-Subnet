"""Validity filters + weight computation (SPEC §6.4, §7).

Pure functions over journaled state so every validator computes identical
weights from identical inputs.
"""
from __future__ import annotations

from collections import defaultdict
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
    horizon_h: int = config.MAX_HORIZON_H   # holding window, for copy-overlap
    is_copy: bool = False                   # set by mark_copies (§7.5)


# ── validity (deterministic re-derivation of the live gateway checks) ─────────
def apply_validity_filters(rows: list[GradedRow]) -> list[GradedRow]:
    """Voids rows in-place per §6.4 and returns the list (ordered by t0, then
    hotkey as the same-block tiebreak — callers must pre-sort identically).

    Voided rows: not graded, don't count toward quotas, no strike.
    """
    rows = sorted(rows, key=lambda r: (r.t0_unix, r.hotkey))
    per_hotkey_times: dict[str, list[float]] = {}           # spacing + daily quota
    open_pair_dir: dict[tuple[str, str, str], float] = {}   # self-overlap

    # NOTE: the old 15-min cross-miner (pair, direction) cooldown was retired —
    # multiple miners may now hold the same trade. Repeated shadowing of one
    # hotkey by another is handled out-of-band by detect_copiers() (§7.5), which
    # penalizes the copier's weight rather than voting the signal.
    for r in rows:
        if r.status == "void":
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
        times.append(r.t0_unix)
        open_pair_dir[okey] = r.t0_unix + config.MAX_HORIZON_H * 3600

    return rows


# ── copy penalty: mark the later entrant on a live identical trade (§7.5) ─────
def mark_copies(rows: list[GradedRow]) -> list[GradedRow]:
    """Sets r.is_copy=True when, at r's T0, an *identical* (pair, direction)
    position from a DIFFERENT hotkey committed earlier was still open
    (entry → entry + horizon). The earliest entrant on a trade is the original
    and is never a copy; everyone who opens the same live trade after it is.

    Outcome-independent and deterministic (ties broken by hotkey). The scoring
    layer then declines to credit a copied WIN — see §7.5 / COPY_PENALTY. Void
    rows are ignored and never mark anyone.
    """
    live = [r for r in rows if r.status != "void"]
    live.sort(key=lambda r: (r.t0_unix, r.hotkey))
    open_intervals: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    for r in live:
        key = (r.trade_pair, r.direction)
        end = r.t0_unix + (r.horizon_h or config.MAX_HORIZON_H) * 3600
        r.is_copy = any(start <= r.t0_unix < e and hk != r.hotkey
                        for (start, e, hk) in open_intervals[key])
        open_intervals[key].append((r.t0_unix, end, r.hotkey))
    return live


def is_habitual_copier(copies: int, decisive: int) -> bool:
    """True when a hotkey landed second into another hotkey's live trade on a
    high fraction of its decisive trades. Aggregated across ALL leaders, so
    rotating which miner you copy doesn't dodge it; gated by COPY_MIN_COPIES so
    an occasional honest second-landing (or a brand-new miner) isn't caught.

    Only a habitual copier's copied wins are stripped — see §7.5 / COPY_PENALTY.
    """
    if config.COPY_PENALTY == "off" or decisive <= 0:
        return False
    return copies >= config.COPY_MIN_COPIES and (copies / decisive) >= config.COPY_HABITUAL_RATE


# ── copy / collusion forensics: 30-day shadowing report (§7.5) ────────────────
@dataclass
class CopyReport:
    follower: str            # the copier (later commit)
    leader: str              # the hotkey being shadowed (earlier commit)
    sharp_events: int        # same (pair,dir) within COPY_SHARP_LAG_S
    soft_events: int         # same (pair,dir) within COPY_SOFT_LAG_S (⊇ sharp)
    flagged: bool            # sharp ≥ threshold ⇒ reversible zero weight
    low_diversity: bool      # soft ≥ threshold ⇒ report-only nudge


def detect_copiers(rows: list[GradedRow], now_unix: float) -> dict[str, list[CopyReport]]:
    """Pairwise copy detection over the trailing COPY_WINDOW_S (deterministic).

    For each (pair, direction), walk commits in time order; the later of two
    commits from *different* hotkeys is the follower, attributed to the nearest
    preceding leader on that (pair, direction). A follower within
    COPY_SHARP_LAG_S of its leader is a *sharp* event (≤15 min — a coordinated /
    shadowing fingerprint, since the 24h timelock makes live copying impossible,
    so tight coincidences mean shared intent); within COPY_SOFT_LAG_S it is a
    *soft* event (≤24 h — same-trade overlap). Counts accumulate per
    (leader→follower) directed pair across the window.

    The first commit on a (pair, direction) is always innocent. Penalties are
    reversible: the window rolls, so once the coincidences age out the flag
    clears on the next weight cycle.

    Returns {follower_hotkey: [CopyReport, ...]} for every pair that crosses a
    threshold, sorted (leader, follower) for stable output.
    """
    cutoff = now_unix - config.COPY_WINDOW_S
    live = [r for r in rows if r.status != "void" and r.t0_unix >= cutoff]
    live.sort(key=lambda r: (r.t0_unix, r.hotkey))

    groups: dict[tuple[str, str], list[GradedRow]] = defaultdict(list)
    for r in live:
        groups[(r.trade_pair, r.direction)].append(r)

    sharp: dict[tuple[str, str], int] = defaultdict(int)
    soft: dict[tuple[str, str], int] = defaultdict(int)
    for evs in groups.values():
        for i, f in enumerate(evs):
            leader = next((evs[j] for j in range(i - 1, -1, -1)
                           if evs[j].hotkey != f.hotkey), None)
            if leader is None:
                continue
            dt = f.t0_unix - leader.t0_unix
            if dt < 0:
                continue
            pair = (leader.hotkey, f.hotkey)
            if dt <= config.COPY_SHARP_LAG_S:
                sharp[pair] += 1
                soft[pair] += 1
            elif dt <= config.COPY_SOFT_LAG_S:
                soft[pair] += 1

    reports: dict[str, list[CopyReport]] = defaultdict(list)
    for (leader, follower) in sorted(set(sharp) | set(soft)):
        s, so = sharp.get((leader, follower), 0), soft.get((leader, follower), 0)
        flagged = s >= config.COPY_SHARP_MIN_EVENTS
        low_div = so >= config.COPY_SOFT_MIN_EVENTS
        if flagged or low_div:
            reports[follower].append(
                CopyReport(follower, leader, s, so, flagged, low_div))
    return dict(reports)


def flagged_copier_hotkeys(reports: dict[str, list[CopyReport]]) -> set[str]:
    """Followers with at least one sharp-flagged leader ⇒ zero weight."""
    return {f for f, rs in reports.items() if any(r.flagged for r in rs)}


# ── weights (§7.2 CONFIRMED: gate → tier-weighted pro-rata wins, trailing 30 days) ─
@dataclass
class MinerState:
    hotkey: str
    uid: int
    first_seen_unix: float   # first commit observed (immunity clock)
    lifetime_decisive: int
    trailing_wins: int       # decisive WONs inside SCORE_WINDOW_S (trailing 30d)
    trailing_decisive: int


def compute_weights(states: list[MinerState], now_unix: float,
                    burn_uid: int = config.BURN_UID,
                    excluded_uids: set[int] | None = None) -> dict[int, float]:
    """{uid: normalized_weight}. Immune miners get the dust floor; qualified
    miners split the rest pro-rata by trailing-window wins, each win scaled by
    the miner's hit-rate tier (§7.3); leftovers burn.

    excluded_uids (flagged copiers, §7.5) earn nothing — neither the immunity
    dust floor nor a pro-rata share — for as long as they stay flagged. Their
    forfeited budget burns. The exclusion is reversible: it is recomputed every
    weight cycle from the rolling copy window.
    """
    excluded_uids = excluded_uids or set()
    weights: dict[int, float] = {}

    immune = [s for s in states
              if now_unix - s.first_seen_unix < config.IMMUNITY_S
              and s.uid not in excluded_uids]
    for s in immune:
        weights[s.uid] = config.DUST_WEIGHT

    qualified = [
        s for s in states
        if s.uid not in excluded_uids
        and s.lifetime_decisive >= config.QUALIFY_MIN_DECISIVE
        and s.trailing_decisive > 0
        and (s.trailing_wins / s.trailing_decisive) >= config.QUALIFY_MIN_HIT
        and s.trailing_wins > 0
    ]

    # tier-weighted wins: each win counts for more at higher hit-rate (§7.3)
    def effective_wins(s: MinerState) -> float:
        return s.trailing_wins * win_multiplier(s.trailing_wins / s.trailing_decisive)

    budget = 1.0 - sum(weights.values())
    total_eff = sum(effective_wins(s) for s in qualified)
    if total_eff > 0 and budget > 0:
        for s in qualified:
            weights[s.uid] = weights.get(s.uid, 0.0) + budget * (effective_wins(s) / total_eff)
    else:
        weights[burn_uid] = weights.get(burn_uid, 0.0) + max(budget, 0.0)

    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()} if total > 0 else {burn_uid: 1.0}


def win_multiplier(hit_rate: float) -> float:
    """Per-win tier multiplier from config.WIN_RATE_TIERS (high → low). Returns
    0.0 below the lowest tier (a non-qualifying hit-rate)."""
    for threshold, mult in config.WIN_RATE_TIERS:
        if hit_rate >= threshold:
            return mult
    return 0.0
