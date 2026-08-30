"""Validity filters + weight computation (SPEC §6.4, §7).

Pure functions over journaled state so every validator computes identical
weights from identical inputs.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import time as _time

from . import config, sessions


# ── statistics helpers (deterministic: only +,-,*,/ and math.sqrt — all IEEE-754
# correctly-rounded, so every validator computes the identical bound; no
# NormalDist / erf / numpy in the scoring path) ──────────────────────────────
def _wilson(wins: int, n: int, z: float) -> tuple[float, float]:
    """Two-sided Wilson score interval (lower, upper) for a binomial rate.
    Stable at small n and never leaves [0, 1]."""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


# -- signed points (CONSENSUS) -----------------------------------------------
# Deterministic: only + - * / sqrt exp log and a bounded series, all IEEE-754
# correctly-rounded, so every validator computes the identical number. Same
# constraint _wilson lives under -- no erf, no NormalDist, no numpy.


def _p_no_touch(a: float) -> float:
    """P(sup|W| < a) over t<=1 for standard Brownian motion.

    Reflection series. Converges fast; 40 terms is far past machine precision
    for every a this is called with, and a fixed term count keeps it a pure
    function of its input rather than of a convergence test.
    """
    if a <= 0:
        return 0.0
    acc = 0.0
    for k in range(40):
        m = 2 * k + 1
        acc += ((-1) ** k) / m * math.exp(-(m * m) * math.pi * math.pi / (8 * a * a))
    return max(0.0, min(1.0, (4.0 / math.pi) * acc))


def resolve_probability(z: float) -> float:
    """Chance a symmetric band z sigma-units wide is touched inside its window.

    z = band / (sigma * sqrt(horizon)) is the ONLY variable that matters: band
    and horizon are not independent knobs, and two calls with the same z are the
    same claim however they were drawn.
    """
    return 1.0 - _p_no_touch(z)


def sigma_from_board(tp_bps: float, horizon_s: int,
                     target_resolve: float | None = None) -> float:
    """Pair volatility in bps per sqrt(second), DERIVED from the board entry.

    Every band in HF_BANDS_HISTORY is solved so the pair resolves
    HF_POINTS_TARGET_RESOLVE of the time, so a board row IS a point on the
    resolve curve and sigma falls out of it. Deriving beats shipping a sigma
    table: a second consensus table drifts from the first, and this repo already
    carries the same symbol map in six places.
    """
    tgt = config.HF_POINTS_TARGET_RESOLVE if target_resolve is None else target_resolve
    z_ref = z_for_resolve(tgt)
    if tp_bps <= 0 or horizon_s <= 0 or z_ref <= 0:
        return 0.0
    return tp_bps / (z_ref * math.sqrt(horizon_s))


def z_for_resolve(p: float) -> float:
    """The z whose resolve probability is p. Bisection, fixed iteration count
    so it is deterministic rather than tolerance-dependent."""
    lo, hi = 0.01, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if resolve_probability(mid) > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def points_for(tp_bps: float, horizon_s: int, sigma: float,
               gamma: float | None = None) -> float:
    """What a WIN on this shape pays. A loss costs the same; a wash is zero.

    pts = 1 + gamma * log2(1 / p_res(z))  -- the information content of the
    claim. A no-view miner wins at p_res/2 (the band has to be reached, then
    direction is a coin flip), so this is -log2 of that prior, and paying a win
    its own surprise is what makes the scale difficulty-adjusted.

    Because a loss costs exactly this, a no-view miner's expectation is zero at
    every band and window for ANY gamma -- the two sides cancel whatever this
    function looks like. That is what frees gamma to be a product dial.
    """
    g = config.HF_POINTS_GAMMA if gamma is None else gamma
    if sigma <= 0 or horizon_s <= 0 or tp_bps <= 0:
        return 0.0
    z = tp_bps / (sigma * math.sqrt(horizon_s))
    pr = resolve_probability(z)
    if pr <= 0.0:
        pr = 1e-12
    return 1.0 + g * (math.log(1.0 / pr) / math.log(2.0))


def signed_points(status: str, tp_bps: float, horizon_s: int, sigma: float,
                  gamma: float | None = None) -> float:
    """+pts on a win, -pts on a loss, 0 on a wash or a void.

    The symmetry is the mechanism, not a stylistic choice. Pricing a wash
    negative was measured and rejected: a wash cost of -k*pts collapses the
    optimum to the narrowest legal band at every skill level, because the cost
    scales with the same pts() the widening was meant to earn.
    """
    if status not in ("won", "lost"):
        return 0.0
    p = points_for(tp_bps, horizon_s, sigma, gamma)
    return p if status == "won" else -p


def confident_edge(wins: int, n: int, z: float = config.QUALIFY_Z) -> float:
    """Lower confidence bound on a hotkey's true hit-rate — the qualify metric."""
    return _wilson(wins, n, z)[0]


def shrunk_hit_rate(wins: int, n: int, k: float = config.TIER_PRIOR_K) -> float:
    """Beta-posterior mean, prior Beta(k/2, k/2) centred at 0.50 — the tier metric.
    Pulls small samples toward a coin flip; leaves large samples nearly untouched."""
    return (wins + k / 2.0) / (n + k)


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
def apply_validity_filters(rows: list[GradedRow],
                           hf_locks: dict | None = None) -> list[GradedRow]:
    """Voids rows in-place per §6.4 and returns the list (ordered by t0, then
    hotkey as the same-block tiebreak — callers must pre-sort identically).

    Voided rows: not graded, don't count toward quotas, no strike.

    `hf_locks` is the cross-mechanism pair-lock index ((hotkey, PAIR, mecid) ->
    last submit ms), built from the PUBLISHED HF receipt logs. Passing None (the
    default) disables the check entirely, which is also what happens before
    config.PAIR_LOCK_LF_FROM. Callers that cannot see the HF logs must pass None
    rather than an empty dict, so "no lock data" never masquerades as "no locks".
    """
    rows = sorted(rows, key=lambda r: (r.t0_unix, r.hotkey))
    per_hotkey_times: dict[str, list[float]] = {}           # accepted commits

    # NOTE: the old 15-min cross-miner (pair, direction) cooldown was retired —
    # multiple miners may now hold the same trade. Repeated shadowing of one
    # hotkey by another is handled out-of-band by detect_copiers() (§7.5), which
    # penalizes the copier's weight rather than voting the signal.
    #
    # Self-overlap stays allowed — a hotkey may hold multiple open calls on
    # the same (pair, direction), and long-then-short on one pair is legal.
    # The per-hotkey limits (daily cap + min gap between ACCEPTED commits) are
    # AS-OF versioned: each row is judged by the rules in force at its OWN t0
    # (config.submission_rules_as_of), so the 2026-07-18 revert to 3/day + 1h
    # never retroactively voids rows accepted under the flat-6/no-gap era —
    # this function re-runs over the whole journal every grade cycle.
    for r in rows:
        if r.status == "void":
            continue

        cap, gap = config.submission_rules_as_of(r.t0_unix)
        times = per_hotkey_times.setdefault(r.hotkey, [])

        # Dead horizon: an FX/metals call whose window is mostly a closed market
        # had no chance to resolve, so grading it says nothing about the trader.
        # Checked FIRST because it is a property of the call alone -- it does not
        # depend on what else this hotkey submitted -- and, like every void here,
        # it `continue`s without appending to `times`, so it consumes no quota.
        # The class and horizon come from the BOARD as of t0, never from the
        # miner's committed fields (config.asset_class_for's forgery note).
        if config.fx_dead_horizon_enforced_as_of(r.t0_unix):
            cls = config.asset_class_for(r.trade_pair, r.t0_unix)
            if cls in sessions.SESSION_BOUND_CLASSES:
                horizon_h = config.horizon_h_for(r.trade_pair, r.t0_unix)
                if sessions.open_fraction(r.t0_unix, horizon_h) < config.FX_MIN_OPEN_FRACTION:
                    r.status, r.void_reason = "void", "fx_dead_horizon"
                    continue

        # ≥ gap since this hotkey's last ACCEPTED commit, minus a block-jitter
        # tolerance: T0 is the inclusion-block timestamp (12 s quantized), so an
        # honestly-spaced pair of commits can read a few seconds under min_gap on
        # chain. Void only when the shortfall exceeds SUBMISSION_GAP_SLACK_S.
        if gap and times and r.t0_unix - times[-1] < gap - config.SUBMISSION_GAP_SLACK_S:
            r.status, r.void_reason = "void", "min_spacing"
            continue

        # ≤ cap per UTC day
        day = int(r.t0_unix // 86_400)
        if sum(1 for t in times if int(t // 86_400) == day) >= cap:
            r.status, r.void_reason = "void", "daily_quota"
            continue

        # cross-mechanism pair lock: this pair traded on HF inside the window?
        # Checked AFTER the quota checks so a locked call does not also consume
        # the hotkey's daily allowance -- it was never a valid call to begin with.
        if hf_locks is not None and config.pair_lock_lf_enforced_as_of(r.t0_unix):
            from . import hf as _hf
            if _hf.is_pair_locked(hf_locks, r.hotkey, r.trade_pair,
                                  _hf.MECH_LF, int(r.t0_unix * 1000)):
                r.status, r.void_reason = "void", "pair_locked_other_mechanism"
                continue

        # accepted
        times.append(r.t0_unix)

    return rows


# ── elimination floor (terminal — hotkey zeroed permanently) ─────────────────
def elimination_t0(decisive: list[tuple[float, bool]]) -> float | None:
    """First decisive t0 at which a hotkey is confidently below the floor, else
    None. `decisive` is the hotkey's full decisive history as (t0_unix, won).
    Same signature/return contract regardless of path, so the validator caller
    (`refresh_eliminations`) is unchanged.

    CONFIDENCE path: terminal when the LIFETIME Wilson upper bound on the true
    hit-rate falls below ELIM_UB_CEIL. The lifetime bound tightens monotonically
    toward the truth, so a true-≥50 % trader's upper bound stays well above the
    ceiling forever (never noise-killed on a cold streak) while a true-≤40 %
    trader's upper bound falls through it and stays there (reliably removed).
    Evaluated at each decisive event using that event's t0, so the verdict is a
    pure function of the graded journal — identical across validators.
    """
    if not config.CONFIDENCE_SCORING:
        return _elimination_t0_legacy(decisive)
    decisive = sorted(decisive)
    wins = 0
    for i, (t0, won) in enumerate(decisive):
        wins += 1 if won else 0
        n = i + 1
        if n < config.ELIM_MIN_DECISIVE:
            continue
        if _wilson(wins, n, config.ELIM_Z)[1] < config.ELIM_UB_CEIL:
            return t0
    return None


def _elimination_t0_legacy(decisive: list[tuple[float, bool]]) -> float | None:
    """LEGACY rolling-window point floor (CONFIDENCE_SCORING=0 / §8 diff script).
    Terminal when the trailing-SCORE_WINDOW_S hit-rate drops below ELIM_FLOOR_HIT.
    Retained only so the illustration can diff old vs new — the rolling running
    minimum over ~100 evaluation points is exactly what noise-kills good traders.
    """
    decisive = sorted(decisive)
    window: list[tuple[float, bool]] = []
    for i, (t0, won) in enumerate(decisive):
        window.append((t0, won))
        while window and window[0][0] < t0 - config.SCORE_WINDOW_S:
            window.pop(0)
        if i + 1 < config.ELIM_MIN_DECISIVE_LEGACY or len(window) < config.ELIM_MIN_TRAILING:
            continue
        wins = sum(1 for _, w in window if w)
        if wins / len(window) < config.ELIM_FLOOR_HIT:
            return t0
    return None


# ── copy penalty: mark the later entrant on a live identical trade (§7.5) ─────
def both_direction_spammers(rows: list[GradedRow]) -> set[str]:
    """Hotkeys that held LONG and SHORT on the SAME pair with overlapping live
    windows. That is never a real directional signal — it's the tell of a
    griefer occupying both sides of a pair so that every other miner who trades
    it lands "second" and gets copy-flagged. Such a hotkey is barred from the
    copy-leader role (the 24h timelock makes live copying impossible, so mere
    overlap is not evidence anyone copied *them*). Pure/deterministic.
    """
    by_hk_pair: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    for r in rows:
        if r.status == "void":
            continue
        end = r.t0_unix + (r.horizon_h or config.MAX_HORIZON_H) * 3600
        by_hk_pair[(r.hotkey, r.trade_pair)].append((r.t0_unix, end, r.direction))
    spammers: set[str] = set()
    for (hk, _pair), ivs in by_hk_pair.items():
        longs = [(s, e) for (s, e, d) in ivs if d == "LONG"]
        shorts = [(s, e) for (s, e, d) in ivs if d == "SHORT"]
        if any(ls < se and ss < le for (ls, le) in longs for (ss, se) in shorts):
            spammers.add(hk)
    return spammers


def mark_copies(rows: list[GradedRow],
                eligible_leaders: set[str] | None = None) -> list[GradedRow]:
    """Sets r.is_copy=True when, at r's T0, an *identical* (pair, direction)
    position from a DIFFERENT, ELIGIBLE-LEADER hotkey committed earlier was still
    open (entry → entry + horizon). The earliest eligible entrant on a trade is
    the original and is never a copy; a later entrant on the same live trade is.

    `eligible_leaders` gates who can make someone a copier: only a real signal
    source (track record, not a throwaway griefer; see the validator) counts as
    a leader. When None, ANY earlier different hotkey marks — the legacy
    behaviour, retained for unit tests. This is the anti-grief lever: without
    it, a zero-stake hotkey could occupy a (pair, direction) early — or both
    directions of a pair — and manufacture copy-flags against honest miners who
    merely traded the same pair, which the 24h timelock makes impossible to have
    actually copied.

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
        r.is_copy = any(
            start <= r.t0_unix < e and hk != r.hotkey
            and (eligible_leaders is None or hk in eligible_leaders)
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


def copy_gate_inputs(decisive: list[tuple[float, bool, bool]],
                     now_unix: float) -> tuple[int, int, float | None]:
    """(copies, decisive, last_copy_unix) inside COPY_WINDOW_S.

    `last_copy_unix` is the t0 of the most recent COPY-marked decisive trade in
    the window (None if there is none) — the penalty TTL is measured from it, so
    a flag self-clears COPY_PENALTY_TTL_S after the trader stops landing second.
    Pure/deterministic.
    """
    cutoff = now_unix - config.COPY_WINDOW_S
    rows = [d for d in decisive if d[0] >= cutoff]
    copies = sum(1 for d in rows if d[2])
    last_cp = max((d[0] for d in rows if d[2]), default=None)
    return copies, len(rows), last_cp


def is_penalised_copier(copies: int, decisive: int, has_shadow_signature: bool) -> bool:
    """Whether to strip this hotkey's copied wins (§7.5).

    Two independent signals must agree:
      * the RATE gate (is_habitual_copier) — it lands second on a high share of
        its decisive trades; and
      * the SHADOW gate — detect_copiers found >= COPY_SHARP_MIN_EVENTS *sharp*
        (<= COPY_SHARP_LAG_S) follows of a SINGLE leader, i.e. a real 1:1
        fingerprint rather than "we both traded gold today".

    The rate gate alone cannot separate a copier from an honest miner on a
    crowded board: landing second inside a leader's 8-12h horizon is the norm,
    not the exception. The shadow gate supplies the intent evidence the rate
    gate lacks. Set COPY_REQUIRE_SHADOW=0 to restore the rate-only behaviour.
    """
    if not is_habitual_copier(copies, decisive):
        return False
    if config.COPY_REQUIRE_SHADOW and not has_shadow_signature:
        return False
    return True


# ── copy / collusion forensics: 30-day shadowing report (§7.5) ────────────────
@dataclass
class CopyReport:
    follower: str            # the copier (later commit)
    leader: str              # the hotkey being shadowed (earlier commit)
    sharp_events: int        # same (pair,dir) within COPY_SHARP_LAG_S (RAW count)
    soft_events: int         # same (pair,dir) within COPY_SOFT_LAG_S (⊇ sharp)
    flagged: bool            # sharp EPISODES ≥ threshold ⇒ reversible zero weight
    low_diversity: bool      # soft ≥ threshold ⇒ report-only nudge
    sharp_episodes: int = 0  # sharp follows collapsed by COPY_EPISODE_S — what `flagged` uses


def _episodes(ts, gap: float) -> int:
    """Collapse follow timestamps into distinct DECISIONS.

    Follows of the same leader inside `gap` are one reaction, not several: a
    trader firing its 6-signal daily allowance across BTC/ETH/XAU/EUR within a
    couple of minutes of a print is making ONE call, and scoring it as 5-6
    independent coincidences is pseudo-replication. Anchored (not chained), so
    an episode can never span more than `gap`. Pure/deterministic.
    """
    ts = sorted(ts)
    if not ts:
        return 0
    n, anchor = 1, ts[0]
    for t in ts[1:]:
        if t - anchor > gap:
            n += 1
            anchor = t
    return n


def detect_copiers(rows: list[GradedRow], now_unix: float,
                   eligible_leaders: set[str] | None = None) -> dict[str, list[CopyReport]]:
    """Pairwise copy detection over the trailing COPY_WINDOW_S (deterministic).

    For each (pair, direction), walk commits in time order; the later of two
    commits from *different* hotkeys is the follower, attributed to the nearest
    preceding ELIGIBLE-LEADER on that (pair, direction). `eligible_leaders` (see
    mark_copies) bars a throwaway griefer from being treated as the leader that
    others "shadow"; when None, any preceding hotkey can be the leader (legacy
    behaviour, retained for unit tests). A follower within
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
    sharp_ts: dict[tuple[str, str], list[float]] = defaultdict(list)
    soft: dict[tuple[str, str], int] = defaultdict(int)
    for evs in groups.values():
        for i, f in enumerate(evs):
            leader = next((evs[j] for j in range(i - 1, -1, -1)
                           if evs[j].hotkey != f.hotkey
                           and (eligible_leaders is None
                                or evs[j].hotkey in eligible_leaders)), None)
            if leader is None:
                continue
            dt = f.t0_unix - leader.t0_unix
            if dt < 0:
                continue
            pair = (leader.hotkey, f.hotkey)
            if dt <= config.COPY_SHARP_LAG_S:
                sharp[pair] += 1
                sharp_ts[pair].append(f.t0_unix)
                soft[pair] += 1
            elif dt <= config.COPY_SOFT_LAG_S:
                soft[pair] += 1

    reports: dict[str, list[CopyReport]] = defaultdict(list)
    for (leader, follower) in sorted(set(sharp) | set(soft)):
        s, so = sharp.get((leader, follower), 0), soft.get((leader, follower), 0)
        eps = _episodes(sharp_ts.get((leader, follower), ()), config.COPY_EPISODE_S)
        flagged = eps >= config.COPY_SHARP_MIN_EVENTS
        low_div = so >= config.COPY_SOFT_MIN_EVENTS
        if flagged or low_div:
            reports[follower].append(
                CopyReport(follower, leader, s, so, flagged, low_div, eps))
    return dict(reports)


def flagged_copier_hotkeys(reports: dict[str, list[CopyReport]]) -> set[str]:
    """Followers with at least one sharp-flagged leader ⇒ zero weight."""
    return {f for f, rs in reports.items() if any(r.flagged for r in rs)}


# ── referral / recruiter incentive (§ referral) ───────────────────────────────
def valid_referral_pairs(referrals: list[dict]) -> list[tuple[str, str]]:
    """Deterministic validity filter over journaled referral claims.

    referrals: [{recruiter_hk, recruit_hk, commit_block, recruit_reg_block}].
    A claim is valid iff the recruit's FIRST-OBSERVED registration block is at
    least REFERRAL_MIN_LEAD_BLOCKS after the referral's inclusion block — the
    chain-anchored "committed before the recruit registered" proof. The lead
    margin defeats mempool front-running (sniping a stranger's pending
    registration extrinsic); the first-sighting reg block (journaled once,
    never re-read live — see validator.refresh_referrals) defeats
    deregister/re-register laundering of an already-registered recruit.

    One recruit belongs to at most ONE recruiter (earliest commit_block wins,
    recruiter hotkey as the same-block tiebreak), and a recruiter keeps at most
    its REFERRAL_MAX_RECRUITS earliest recruits (0 = unlimited). Pure/deterministic; the pair
    no-copy gate is applied separately (it needs the graded rows).
    """
    claims = []
    for r in referrals or []:
        recruiter, recruit = r.get("recruiter_hk"), r.get("recruit_hk")
        cb, rb = r.get("commit_block"), r.get("recruit_reg_block")
        if not recruiter or not recruit or recruiter == recruit:
            continue
        if cb is None or rb is None:
            continue                       # recruit never registered (yet)
        if int(cb) + config.REFERRAL_MIN_LEAD_BLOCKS > int(rb):
            continue                       # too late: registered first / front-run
        claims.append((int(cb), recruiter, recruit))

    # one recruit, one recruiter — earliest claim wins
    by_recruit: dict[str, tuple[int, str]] = {}
    for cb, recruiter, recruit in sorted(claims, key=lambda c: (c[0], c[1])):
        by_recruit.setdefault(recruit, (cb, recruiter))

    # per-recruiter breadth cap — earliest recruits win
    by_recruiter: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for recruit, (cb, recruiter) in by_recruit.items():
        by_recruiter[recruiter].append((cb, recruit))
    pairs: list[tuple[str, str]] = []
    for recruiter in sorted(by_recruiter):
        kept = sorted(by_recruiter[recruiter])
        if config.REFERRAL_MAX_RECRUITS > 0:            # 0 = unlimited (no breadth cap)
            kept = kept[:config.REFERRAL_MAX_RECRUITS]
        pairs.extend((recruiter, recruit) for _, recruit in kept)
    return sorted(pairs)


def apply_referral_transfers(pairs: list[tuple[str, str]],
                             transfers: list[dict]) -> list[tuple[str, str]]:
    """Remap referral pairs through journaled sn89refx transfers (§ referrer
    mechanism). Deterministic and deliberately narrow:

      * ONE transfer per original recruiter hotkey — the earliest commit_block
        wins (to_hk as the same-block tiebreak); every later transfer from
        that hotkey is inert forever. This is the "can only be done once for
        each referral hotkey" rule, enforced at replay so no validator state
        can drift it.
      * NON-CHAINING: the remap is a single pass over the ORIGINAL pairs. If
        A→B and B→C, A's recruits land on B and stay there — B's transfer
        moves only referrals B itself committed. Chains would let a stolen
        hotkey re-route someone else's transferred base.
      * self-transfers are inert.

    transfers: [{from_hk, to_hk, commit_block}] as journaled (raw facts; this
    function IS the validity filter, mirroring valid_referral_pairs).
    """
    best: dict[str, tuple[int, str]] = {}
    for t in transfers or []:
        frm, to, cb = t.get("from_hk"), t.get("to_hk"), t.get("commit_block")
        if not frm or not to or frm == to or cb is None:
            continue
        cand = (int(cb), to)
        if frm not in best or cand < best[frm]:
            best[frm] = cand
    remap = {frm: to for frm, (_, to) in best.items()}
    return sorted((remap.get(recruiter, recruiter), recruit)
                  for recruiter, recruit in pairs)


def referrer_scores(pairs: list[tuple[str, str]],
                    tally_by_hk: dict[str, float],
                    withheld_recruits: set[str] | None = None) -> dict[str, float]:
    """§ referrer mechanism: score[recruiter] = Σ recruit tallies. The tally is
    the recruit's decayed qualified-win tally — the same currency that sizes
    the recruit's own emission — so a referrer earns exactly in proportion to
    how much their recruits are currently earning. A recruiter whose recruits
    are all cold scores 0 (no floor: the mechanism pays performance of the
    base, not its size).

    withheld_recruits: recruits whose pair the RECRUITER is currently shadowing
    (referral_pair_followers). That pair contributes nothing to the recruiter's
    score for as long as it holds. This is the recruiter's half of the pair
    no-copy gate and the only place it bites them, because mecid-1 is the only
    referrer bonus that still pays — the in-band 20% share-shift retired
    2026-08-03. It withholds a BONUS and nothing else: the recruiter's own
    mecid-0 emission, the recruit's emission, and the recruit's +10% are all
    untouched, and the pair stays valid so it resumes when the gate clears.
    """
    withheld = withheld_recruits or set()
    scores: dict[str, float] = {}
    for recruiter, recruit in pairs:
        if recruit in withheld:
            continue
        t = float(tally_by_hk.get(recruit, 0.0))
        if t > 0:
            scores[recruiter] = scores.get(recruiter, 0.0) + t
    return scores


def blended_recruit_tallies(tallies_by_comp: dict[str, dict[str, float]],
                            shares: dict[str, float]) -> dict[str, float]:
    """Collapse per-competition raw tallies into ONE cross-competition tally.

        blended[hk] = Σ_c  share_c · tally_c[hk] / Σ_field tally_c

    NORMALIZE-THEN-WEIGHT, the same property competitions.combine relies on: a
    recruit is credited with their share OF a competition, scaled by what that
    competition is worth. Summing raw tallies instead would let the metrics'
    natural scales decide — Closers sums winsorized vol-normalized scores while
    LF sums tier-weighted decayed wins, and whichever runs bigger numbers would
    quietly become the only competition a recruiter is paid for.

    Two things this deliberately consumes are RAW tallies, not weight vectors:

      * a weight vector carries the immunity DUST floor, so scoring off it would
        pay a recruiter for registering hotkeys and never trading them — the
        cheapest sybil in the mechanism. A tally has no floor.
      * the LF vector carries the recruit's own +10% referral bonus, so scoring
        off it would compound the bonus into the score that caused it.

    `shares` normally comes from config.comp_weights_as_of(now) with the burn
    placeholders dropped and the remainder renormalized (see referrer_shares).
    A competition with an empty field contributes zero to everyone, which is
    correct for a RELATIVE score — there is nothing to burn in a score.
    """
    blended: dict[str, float] = {}
    for comp, share in shares.items():
        if share <= 0:
            continue
        field = tallies_by_comp.get(comp) or {}
        total = sum(v for v in field.values() if v > 0)
        if total <= 0:
            continue
        for hk, v in field.items():
            if v > 0:
                blended[hk] = blended.get(hk, 0.0) + share * (v / total)
    return blended


def referrer_shares(comp_shares: dict[str, float]) -> dict[str, float]:
    """The competition shares a recruiter is scored across.

    Drops non-competition keys — `reserve` is a pure BURN placeholder holding
    the referrers' own carve-out inside mecid-0, and crediting a recruit for it
    would pay the referrer pool out of itself. The remainder is renormalized so
    the LF/HF/Closers RELATIVE weighting is what it looks like, independent of
    how much reserve happens to be parked in the row this era.
    """
    live = {k: v for k, v in comp_shares.items()
            if v > 0 and k not in config.REFERRER_SCORE_EXCLUDED_COMPS}
    total = sum(live.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in live.items()}


def referrer_weights(scores: dict[str, float], uid_by_hk: dict[str, int],
                     burn_uid: int | None = None,
                     now_unix: float | None = None) -> dict[int, float]:
    """{uid: weight} for the referrer mechanism (mecid 1). Pro-rata by score,
    field capped at MINER_EMISSION_CAP like every other competition, remainder
    burns. A referrer only earns if their hotkey holds a UID — registration is
    the sybil cost of joining the referrer class; no trading required."""
    burn_uid = config.BURN_UID if burn_uid is None else burn_uid
    weights: dict[int, float] = {}
    pool = sum(v for hk, v in scores.items() if uid_by_hk.get(hk) is not None)
    cap = config.miner_emission_cap_as_of(
        _time.time() if now_unix is None else now_unix)
    if pool > 0:
        for hk, v in scores.items():
            uid = uid_by_hk.get(hk)
            if uid is not None and v > 0:
                weights[uid] = weights.get(uid, 0.0) + cap * (v / pool)
    weights[burn_uid] = weights.get(burn_uid, 0.0) + max(0.0, 1.0 - sum(weights.values()))
    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()} if total > 0 else {burn_uid: 1.0}


def referral_pair_followers(pair_rows: list[GradedRow], hk_a: str, hk_b: str,
                            now_unix: float) -> dict[str, float]:
    """Pair-scoped no-copy gate, DIRECTION-AWARE: {follower_hotkey:
    suspended_until} for each side of (hk_a, hk_b) that is currently shadowing
    the other. Empty dict = the pair is clean. Pure/deterministic.

    Deliberately far stricter than the global §7.5 detector — the pair opted
    into scrutiny by pairing up, and a trip here only withholds a BONUS (both
    sides' base emission is untouched, no public flag, no elimination). Two
    triggers, evaluated PER FOLLOWER:

      * SHARP shadowing — same (pair, direction) commits within
        COPY_SHARP_LAG_S, collapsed into decisions by _episodes();
        ≥ REFERRAL_PAIR_SHARP_EPISODES trips.
      * LIVE-OVERLAP follows — mark_copies restricted to the pair (leader
        eligibility bypassed: both parties consented), episode-collapsed;
        ≥ REFERRAL_PAIR_OVERLAP_EPISODES trips.

    Events are counted over the trailing REFERRAL_PAIR_WINDOW_S; a trip
    suspends that follower until REFERRAL_PAIR_TTL_S after ITS OWN last event,
    then self-clears (same philosophy as COPY_PENALTY_TTL_S — a warning, not a
    scarlet letter). Both sides can be suspended at once, on independent
    clocks; neither inherits the other's.

    ⚠ Both triggers were always tallied per follower — the episode counters are
    keyed by follower hotkey and always were. Until 2026-08-24 the result was
    collapsed with max() and the whole PAIR was dropped, so the side that
    copied and the side that was copied FROM lost their bonus together. On
    2026-08-18 the thresholds had to be widened to 4/8 because a recruiter
    shadowed its own recruit and the RECRUIT was the one suspended for it —
    treating the symptom. This is the cause: the penalty now lands on whoever
    followed. See referral_pair_suspended_until for the symmetric view, which
    survives for display only.
    """
    cutoff = now_unix - config.REFERRAL_PAIR_WINDOW_S
    pair_hk = {hk_a, hk_b}
    rows = [r for r in pair_rows
            if r.hotkey in pair_hk and r.status != "void" and r.t0_unix >= cutoff]
    if not rows:
        return {}
    rows.sort(key=lambda r: (r.t0_unix, r.hotkey))

    suspended: dict[str, float] = {}

    def _trip(follower: str, ts: list[float], threshold: int) -> None:
        if _episodes(ts, config.COPY_EPISODE_S) < threshold:
            return
        until = max(ts) + config.REFERRAL_PAIR_TTL_S
        if now_unix < until:
            suspended[follower] = max(suspended.get(follower, 0.0), until)

    # sharp shadowing, per direction-of-follow
    groups: dict[tuple[str, str], list[GradedRow]] = defaultdict(list)
    for r in rows:
        groups[(r.trade_pair, r.direction)].append(r)
    sharp_ts: dict[str, list[float]] = defaultdict(list)   # follower → event t0s
    for evs in groups.values():
        for i, f in enumerate(evs):
            leader = next((evs[j] for j in range(i - 1, -1, -1)
                           if evs[j].hotkey != f.hotkey), None)
            if leader is None:
                continue
            if 0 <= f.t0_unix - leader.t0_unix <= config.COPY_SHARP_LAG_S:
                sharp_ts[f.hotkey].append(f.t0_unix)
    for follower, ts in sharp_ts.items():
        _trip(follower, ts, config.REFERRAL_PAIR_SHARP_EPISODES)

    # live-overlap follows (fresh copies of the rows — mark_copies mutates is_copy)
    marked = mark_copies([GradedRow(hotkey=r.hotkey, trade_pair=r.trade_pair,
                                    direction=r.direction, t0_unix=r.t0_unix,
                                    status=r.status, horizon_h=r.horizon_h)
                          for r in rows], eligible_leaders=pair_hk)
    overlap_ts: dict[str, list[float]] = defaultdict(list)
    for r in marked:
        if r.is_copy:
            overlap_ts[r.hotkey].append(r.t0_unix)
    for follower, ts in overlap_ts.items():
        _trip(follower, ts, config.REFERRAL_PAIR_OVERLAP_EPISODES)

    return suspended


def referral_pair_suspended_until(pair_rows: list[GradedRow], hk_a: str, hk_b: str,
                                  now_unix: float) -> float | None:
    """Symmetric view of referral_pair_followers: the latest unix time until
    which EITHER side of (hk_a, hk_b) is suspended, or None if neither is.

    ⚠ This no longer decides anyone's money. It answers "is this pair under
    the copy gate at all", which is what a status line wants; the weight paths
    take referral_pair_followers and withhold only the follower's own bonus.
    Do not reintroduce it into a scoring path — collapsing the two sides here
    is exactly the bug that made a recruit pay for its recruiter's copying.
    """
    followers = referral_pair_followers(pair_rows, hk_a, hk_b, now_unix)
    return max(followers.values()) if followers else None


# ── weights (§7.2 CONFIRMED: gate → tier-weighted pro-rata wins, trailing 30 days) ─
@dataclass
class MinerState:
    hotkey: str
    uid: int
    first_seen_unix: float   # first commit observed (immunity clock)
    rep_wins: int            # WONs inside the reputation window — hit-rate + tier
    rep_decisive: int        # decisive (won+lost) in the window — gate + hit-rate denom
    trailing_wins: int       # WONs inside SCORE_WINDOW_S (30d) — legacy/reporting only
    # QUALIFIED post-warmup wins as [(t0_unix, tier_weight)] — a win the miner
    # earned WHILE qualified (point-in-time gate), tagged with the tier multiplier
    # it earned at that moment (≥1.0). This is what now SIZES the emission: a
    # time-decayed, tier-weighted tally of these (see compute_weights). Losses
    # never appear here.
    qwins: list[tuple[float, float]] = field(default_factory=list)
    # SIGNED points history [(t0, points)], losses included. Empty on every
    # network that has not armed HF_POINTS_FROM, which is all of them today.
    # Kept beside qwins rather than replacing it so a disarmed chain computes
    # byte-identically to before this field existed.
    qcalls: list[tuple[float, float]] = field(default_factory=list)
    # Resolved history as [(t0, was_wash, q)], q being the wash probability the
    # miner DECLARED by drawing that shape. Only the excess over it is charged.
    wash_hist: list[tuple[float, bool, float]] = field(default_factory=list)


def score_inputs(decisive: list[tuple[float, bool, bool]], first_seen_unix: float,
                 now_unix: float) -> tuple[int, int, int, int, int, int]:
    """From a hotkey's decisive history [(t0_unix, won, is_copy)] compute the
    scoring inputs:
      (rep_wins, rep_decisive,
       trailing_wins_all, trailing_wins_orig, trailing_copies, trailing_decisive)

    * rep_* are the REPUTATION window: decisive trades in the trailing
      HIT_RATE_WINDOW_S, capped at the most recent HIT_RATE_WINDOW_TRADES
      (whichever is tighter). They drive the qualify gate and the hit-rate tier.
      Reputation DECAYS — old outcomes age out, so a once-great miner that goes
      cold loses its tier/qualification, and stale wins can't mask bad recent
      trading. Warmup outcomes still count toward the record (only emission
      excludes warmup wins, below).
    * trailing_* span the last SCORE_WINDOW_S, and a win only counts toward
      EMISSIONS if it was committed AFTER this hotkey's warmup (immunity) ended
      (t0 ≥ first_seen + IMMUNITY_S). Warmup wins build the record but never pay;
      a miner must post NEW wins after warmup to earn above the dust floor.
    """
    score_cutoff = now_unix - config.SCORE_WINDOW_S
    rep_cutoff = now_unix - config.HIT_RATE_WINDOW_S
    warmup_end = first_seen_unix + config.IMMUNITY_S

    # reputation window: in the last HIT_RATE_WINDOW_S, then the most recent
    # HIT_RATE_WINDOW_TRADES of those (sort by t0 so "most recent" is well-defined
    # regardless of the caller's row order).
    recent = sorted((d for d in decisive if d[0] >= rep_cutoff), key=lambda d: d[0])
    recent = recent[-config.hit_rate_window_trades_as_of(now_unix):]
    rep_wins = sum(1 for d in recent if d[1])
    rep_decisive = len(recent)

    tw_all = tw_orig = tcopies = tdec = 0
    for d in decisive:
        t0, won, is_copy = d[0], d[1], d[2]
        if t0 >= score_cutoff:
            tdec += 1
            if is_copy:
                tcopies += 1
            if won and t0 >= warmup_end:        # post-warmup wins only earn
                tw_all += 1
                if not is_copy:
                    tw_orig += 1
    return rep_wins, rep_decisive, tw_all, tw_orig, tcopies, tdec


def compute_weights(states: list[MinerState], now_unix: float,
                    burn_uid: int = config.BURN_UID,
                    excluded_uids: set[int] | None = None,
                    referral_pairs: list[tuple[str, str]] | None = None,
                    referral_suspended: set[str] | None = None,
                    use_points: bool | None = None) -> dict[int, float]:
    """{uid: normalized_weight}. Immune miners get the dust floor; the pool is
    split across miners by a time-decayed, tier-weighted tally of their QUALIFIED
    wins (a relative competition of recent qualified wins); a probation dust floor
    catches qualified-caliber miners whose tally is momentarily zero; leftovers
    burn (then MINER_EMISSION_CAP caps the whole field, remainder to burn_uid).

    Emission is NOT gated on CURRENT qualification and a loss never subtracts from
    a miner's tally: a miner that was earning and then posts a loss that drops it
    below the gate keeps earning off its banked qualified wins (which decay), with
    no cliff. Qualification is enforced only at the win — a win counts toward the
    tally only if the miner was qualified as of that win (see qualified_wins). The
    linear 1-week decay means a miner must keep landing qualified wins to hold its
    share; parking (not trading) lets the tally decay to zero.

    excluded_uids (flagged copiers, §7.5) earn nothing — neither the immunity
    dust floor nor a pro-rata share — for as long as they stay flagged. Their
    forfeited budget burns; the exclusion is reversible (recomputed each cycle).

    referral_pairs (§ referral): VALID (recruiter, recruit) hotkey pairs — the
    caller (replay.weights_from_journal) has already applied
    valid_referral_pairs. While BOTH sides have a positive base tally and
    neither is excluded, the recruit's effective tally gains
    REFERRAL_RECRUIT_BONUS × its own base and the recruiter's gains
    REFERRAL_RECRUITER_BONUS × the recruit's base (total capped at
    REFERRAL_MAX_X × the recruiter's own base). Bonuses are computed from BASE
    tallies only — never from boosted ones — so A→B→C referral chains cannot
    compound. Pure share-shifting within the miner pool: the pro-rata split
    below normalizes over boosted tallies, and the emission cap is unchanged.

    referral_suspended (§ referral no-copy, DIRECTION-AWARE): hotkeys currently
    shadowing the other side of one of their own pairs
    (referral_pair_followers). A suspended hotkey forfeits ITS OWN referral
    bonus and nothing else — its base tally, its emission and the other side's
    bonus are all untouched. Before 2026-08-24 the caller instead dropped the
    whole pair from referral_pairs, which charged the copied-from side for the
    copier's behaviour.

    Eliminated hotkeys must be filtered out by the caller before this — they get
    nothing, not dust.
    """
    excluded_uids = excluded_uids or set()
    weights: dict[int, float] = {}

    immune = [s for s in states
              if now_unix - s.first_seen_unix < config.IMMUNITY_S
              and s.uid not in excluded_uids]
    for s in immune:
        weights[s.uid] = config.DUST_WEIGHT

    # ── emission = a time-decayed, tier-weighted tally of QUALIFIED wins ─────────
    # Each miner's earning size is decayed_qwin_tally(qwins): the sum of its
    # qualified wins, tier-weighted and linearly decayed over EMISSION_DECAY_S.
    # There is NO current-qualification gate on EARNING — a miner that has fallen
    # below the gate but still holds recent qualified wins keeps earning, its
    # tally simply decaying. Qualification only governs whether NEW wins enter the
    # tally (a win is "qualified" only if the miner was qualified as of that win;
    # see qualified_wins). A loss never subtracts here — it can only un-qualify the
    # miner and thus freeze accrual — so there is no cliff, and parking (stopping
    # trading) to protect emissions doesn't work: the tally decays to zero.
    # use_points: None means "ask the chain" (the arming stamp). A caller passes
    # False when its competition has no priceable history yet -- LF today. Without
    # that, arming points on a chain would zero every LF miner at once: their
    # states carry no qcalls, so the tally is 0 and the whole LF share burns.
    # The gate is per COMPETITION, not per chain, because the competitions are
    # folded in one at a time.
    points_mode = (config.points_enforced_as_of(now_unix)
                   if use_points is None else bool(use_points))

    def qtally(s: MinerState) -> float:
        if points_mode:
            # CLAMPED AT ZERO, and the clamp lives here rather than in
            # decayed_points_tally on purpose. A negative weight has no meaning
            # on chain, but every other caller -- the referrer score, any
            # reporting surface -- needs to see how far underwater a miner is.
            # A tally that clamped itself would hide that from all of them.
            #
            # Underwater therefore means "earns nothing and must climb back to
            # positive", not "is eliminated". Elimination stays the lifetime
            # Wilson upper-bound rule and is never triggered by a points total.
            return max(0.0, decayed_points_tally(s.qcalls, now_unix))
        return decayed_qwin_tally(s.qwins, now_unix)

    # ── probation floor ─────────────────────────────────────────────────────────
    # A qualified-caliber miner whose tally is currently zero keeps the dust floor
    # for PROBATION_S rather than dropping straight to nothing — covering (a) a
    # miner qualified in/after warmup with no qualified win YET (clock from warmup
    # end) and (b) one whose qualified wins have all decayed out of the window
    # (clock from the close of its earning window = last qualified win +
    # EMISSION_DECAY_S). Placed before the budget so this dust comes out of the pool
    # like immune dust (and, being tiny, survives the emission-cap min-scale).
    if config.PROBATION_S > 0:
        for s in states:
            if s.uid in excluded_uids or s.uid in weights:   # excluded, or immune-dusted
                continue
            if qtally(s) > 0:                                # actively earning
                continue
            last_qwin = max((t0 for t0, _ in s.qwins), default=0.0)
            if not (last_qwin or _qualifies(s.rep_wins, s.rep_decisive)):
                continue                                     # never qualified → no floor
            warmup_end = s.first_seen_unix + config.IMMUNITY_S
            earn_close = (last_qwin + config.EMISSION_DECAY_S) if last_qwin else warmup_end
            anchor = max(warmup_end, earn_close)
            if 0.0 <= now_unix - anchor < config.PROBATION_S:
                weights[s.uid] = config.DUST_WEIGHT

    # ── referral bonus (§ referral) — from BASE tallies only, never boosted ones ─
    # (base-not-effective is what stops an A→B→C chain from compounding.)
    base = {s.hotkey: qtally(s) for s in states}
    bonus: dict[str, float] = defaultdict(float)
    if referral_pairs and config.REFERRAL_ENABLED:
        by_hk = {s.hotkey: s for s in states}
        suspended = referral_suspended or set()
        per_recruiter: dict[str, float] = defaultdict(float)
        for recruiter, recruit in referral_pairs:
            r, c = by_hk.get(recruiter), by_hk.get(recruit)
            if r is None or c is None:          # deregistered/eliminated/struck → lapses
                continue
            if r.uid in excluded_uids or c.uid in excluded_uids:
                continue                        # globally copy-zeroed → lapses
            if base[recruiter] <= 0 or base[recruit] <= 0:
                continue                        # mutual-conditional: both must be earning
            # Pair no-copy gate, per side: the follower forfeits its own bonus,
            # the side it shadowed keeps theirs. Withholding a bonus is the
            # WHOLE penalty — base[] above is untouched either way.
            if recruit not in suspended:
                bonus[recruit] += config.REFERRAL_RECRUIT_BONUS * base[recruit]
            # § referrer mechanism: once mecid-1 pays referrers directly, the
            # in-band 20% recruiter share-shift retires (double-paying the same
            # referral from two pools). The recruit's own bonus above stays —
            # it is a trader-entry incentive, not a referrer reward.
            if not config.referrer_active(now_unix) and recruiter not in suspended:
                per_recruiter[recruiter] += config.REFERRAL_RECRUITER_BONUS * base[recruit]
        for hk, b in per_recruiter.items():
            bonus[hk] += min(b, config.REFERRAL_MAX_X * base[hk])

    def eff(s: MinerState) -> float:
        return base[s.hotkey] + bonus.get(s.hotkey, 0.0)

    # ── excess-wash debt ────────────────────────────────────────────────────────
    # Charged only where a miner washed MORE than their own shapes predicted, and
    # ranked by where they stand in the field: the top of the board pays nothing,
    # the bottom pays in full. Continuous, so nobody sits on a threshold.
    #
    # The standing distribution has to be built across the WHOLE field, which is
    # why this cannot live inside qtally() -- a per-miner function cannot know
    # anyone's percentile. It is computed once here, over the raw tallies.
    if points_mode:
        raw = sorted(decayed_points_tally(s.qcalls, now_unix) for s in states)
        def _pct(v: float) -> float:
            if len(raw) < 2:
                return 1.0          # a field of one: nobody is below anybody
            below = sum(1 for r in raw if r < v)
            return below / (len(raw) - 1.0)
        debts: dict[str, float] = {}
        for s in states:
            if not s.wash_hist:
                continue
            t = decayed_points_tally(s.qcalls, now_unix)
            sur = wash_surprise(s.wash_hist)
            debts[s.hotkey] = wash_debt(t, sur, standing_pct=_pct(t))
        if debts:
            _base = base
            base = {hk: v - debts.get(hk, 0.0) for hk, v in _base.items()}

    # ── distribute the pool by decayed qualified-win tally (relative share) ──────
    earners = [s for s in states if s.uid not in excluded_uids and eff(s) > 0]
    budget = 1.0 - sum(weights.values())
    total_q = sum(eff(s) for s in earners)
    if total_q > 0 and budget > 0:
        for s in earners:
            weights[s.uid] = weights.get(s.uid, 0.0) + budget * (eff(s) / total_q)
    else:
        weights[burn_uid] = weights.get(burn_uid, 0.0) + max(budget, 0.0)

    # ── emission cap (Mantis-sn123-style) ──────────────────────────────────────
    # Real miners collectively receive at most MINER_EMISSION_CAP of the total;
    # the rest burns to burn_uid (owner UID0). This is a CEILING: we scale the
    # combined miner weight DOWN to the cap (min-scale, never up), so when the
    # field would earn less than the cap — e.g. only immune dust, nobody
    # qualified — nothing is inflated and leftovers burn exactly as before.
    cap = config.miner_emission_cap_as_of(now_unix)
    if cap < 1.0:
        miner_total = sum(w for u, w in weights.items() if u != burn_uid)
        if miner_total > 0:
            scale = min(1.0, max(0.0, cap) / miner_total)
            for u in list(weights):
                if u != burn_uid:
                    weights[u] *= scale
        # burn absorbs everything not paid out to the field
        paid = sum(w for u, w in weights.items() if u != burn_uid)
        weights[burn_uid] = weights.get(burn_uid, 0.0) + max(0.0, 1.0 - paid)

    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()} if total > 0 else {burn_uid: 1.0}


def win_multiplier(hit_rate: float) -> float:
    """Per-win tier multiplier from config.WIN_RATE_TIERS (high → low) applied to a
    raw hit-rate. Returns 0.0 below the lowest tier. LEGACY tier function — the
    confidence path uses tier_multiplier() (shrunk rate) instead."""
    for threshold, mult in config.WIN_RATE_TIERS:
        if hit_rate >= threshold:
            return mult
    return 0.0


# Legacy alias so the §8 diff script can name the old tier explicitly.
win_multiplier_legacy = win_multiplier


def is_qualified(rep_wins: int, rep_decisive: int) -> bool:
    """Confidence qualify gate: enough sample, and the Wilson lower bound on the
    true hit-rate is at least QUALIFY_LB_FLOOR (≈90 % sure you beat a coin flip)."""
    return (rep_decisive >= config.QUALIFY_MIN_DECISIVE
            and confident_edge(rep_wins, rep_decisive) >= config.QUALIFY_LB_FLOOR)


def is_qualified_legacy(rep_wins: int, rep_decisive: int) -> bool:
    """LEGACY raw-hit qualify gate (rep_decisive ≥ floor and raw hit ≥ MIN_HIT)."""
    return (rep_decisive >= config.QUALIFY_MIN_DECISIVE
            and rep_decisive > 0
            and (rep_wins / rep_decisive) >= config.QUALIFY_MIN_HIT)


def _tier_ramp(r: float) -> float:
    """Continuous per-win multiplier: linear interpolation THROUGH the
    WIN_RATE_TIERS anchors (no cliff between bands), floored at the base tier and
    capped at the top tier. At an exact anchor it equals the discrete tier — only
    the gaps between bands fill in. Deterministic (only +,-,*,/ and sorted)."""
    asc = sorted(config.WIN_RATE_TIERS, key=lambda x: x[0])   # base -> top
    base_th, base_m = asc[0]
    top_th, top_m = asc[-1]
    if r <= base_th:
        return base_m
    if r >= top_th:
        return top_m
    for (a_th, a_m), (b_th, b_m) in zip(asc, asc[1:]):
        if a_th <= r < b_th:
            return a_m + (r - a_th) / (b_th - a_th) * (b_m - a_m)
    return top_m


def tier_multiplier(rep_wins: int, rep_decisive: int, t0_unix: float | None = None) -> float:
    """Per-win tier from config.WIN_RATE_TIERS applied to the SHRUNK hit-rate, so a
    thin-sample hot streak can't grab a high tier. Returns 0.0 below the lowest tier.

    AS-OF: a win at t0_unix >= config.CONTINUOUS_TIER_FROM is valued on the
    CONTINUOUS ramp (_tier_ramp); before the cutover, and whenever t0_unix is None,
    the original discrete banding is used — so replay of pre-cutover wins is
    byte-identical and only new wins get the smoother curve."""
    r = shrunk_hit_rate(rep_wins, rep_decisive)
    if t0_unix is not None and t0_unix >= config.CONTINUOUS_TIER_FROM:
        return _tier_ramp(r)
    for threshold, mult in config.WIN_RATE_TIERS:
        if r >= threshold:
            return mult
    return 0.0


def efficiency_multiplier(graded: list[tuple[float, bool]], t0_unix: float) -> float:
    """Wash-efficiency factor for a win at t0, in [EFFICIENCY_MIN, 1.0].

    `graded` is the miner's FULL resolved history as [(t0_unix, is_wash)] — wins,
    losses and washes. The tier reads only decisive outcomes, so washes are
    invisible to it; this is the term that prices them.

        shrunk = (washes + K * PRIOR_WASH) / (n + K)
        eff    = clamp(1 - SLOPE * shrunk, EFFICIENCY_MIN, 1.0)

    Anchored at PERFECTION: zero washes pays the full 1.0 and nothing pays more.
    There is no bonus tier, so a miner can never earn above their tier for being
    efficient — only lose for being wasteful. Anchoring on the field average
    instead would make the rule relative to whoever else happens to be mining, and
    would pay a bonus to miners who avoid washes by being decisively WRONG.

    Thin samples shrink toward PRIOR_WASH rather than toward zero, so a fresh
    hotkey starts where an average miner sits instead of looking perfect — without
    that, churning hotkeys would launder a wash record.

    AS-OF and PURE: judged on the trailing reputation window ending at THIS win's
    t0 — the same 60-day clock the W/L history ages out on — so a banked win keeps
    the value it was given and the public journal replays byte-identical. Returns
    1.0 before EFFICIENCY_FROM: this is a new rule and it prices only what a miner
    does after it is armed.
    """
    if not config.EFFICIENCY_FROM or t0_unix < config.EFFICIENCY_FROM:
        return 1.0
    # Floored at EFFICIENCY_FROM, not just the reputation window. Wash rate is only
    # comparable across miners once the BOARD equalises structural wash per asset,
    # so history graded under the previous board must not be scored here — for the
    # 60 days after arming, a window reaching back past the cutover would still be
    # mostly old-board data and would pay gold callers for asset choice (XAUUSD
    # produced 14% structural wash against crypto's ~40%). It also makes the rule
    # genuinely forward-only: nothing a miner did before arming can cost it.
    lo = max(t0_unix - config.HIT_RATE_WINDOW_S, float(config.EFFICIENCY_FROM))
    window = [w for t, w in graded if lo <= t <= t0_unix]
    n = len(window)
    k = config.EFFICIENCY_PRIOR_K
    shrunk = ((sum(1 for w in window if w) + k * config.EFFICIENCY_PRIOR_WASH)
              / (n + k))
    return max(config.EFFICIENCY_MIN, min(1.0, 1.0 - config.EFFICIENCY_SLOPE * shrunk))


def _qualifies(rep_wins: int, rep_decisive: int) -> bool:
    """Dispatch the qualify gate on the CONFIDENCE_SCORING flag."""
    if config.CONFIDENCE_SCORING:
        return is_qualified(rep_wins, rep_decisive)
    return is_qualified_legacy(rep_wins, rep_decisive)


def _resolved_by(row: tuple, t0_unix: float) -> bool:
    """Had this decisive row's outcome become KNOWN by t0_unix?

    `row` is (t0, won, is_copy[, resolved_unix]). resolved_unix is the journaled
    exit_at_ms in seconds — when the status became final. A row without one keeps
    LEGACY treatment (counted as known), matching scripts/audit_journal.py: absent
    a timestamp there is nothing to reconstruct from and either assumption is a
    guess. 16 such rows exist and are old and long-settled.
    """
    resolved = row[3] if len(row) > 3 else None
    return True if resolved is None else float(resolved) <= t0_unix


def qualified_wins(decisive: list[tuple], first_seen_unix: float,
                   habitual: bool = False,
                   graded: list[tuple[float, bool]] | None = None
                   ) -> list[tuple[float, float]]:
    """Post-warmup WINs the miner earned WHILE QUALIFIED, each tagged with the
    point-in-time tier weight (≥ 1.0). PURE / deterministic.

    A win at t0 counts iff the miner's reputation-window hit-rate AS OF t0 passes
    the qualify gate — "a qualified win." A win earned while unqualified is
    skipped and never earns. The weight is the tier the miner held at that moment
    (WOLF 2× / SHARP 1.2× / base 1×), floored at 1.0 so a gate-passing win always
    counts at least base.

    AS OF t0 means CAUSALLY as of t0, from config.CAUSAL_QWIN_FROM: the window is
    the trailing HIT_RATE_WINDOW_S of decisive outcomes that had already RESOLVED
    by t0 (plus the win itself), capped at HIT_RATE_WINDOW_TRADES most recent.
    Before that stamp it was every decisive call with an earlier t0 whether or not
    it had resolved — which read the outcomes of positions still OPEN at t0, and
    let a loss that opened before a win but graded after it reach back and erase a
    win already banked and paying. See config.CAUSAL_QWIN_FROM for the measurement.

    `decisive` rows are (t0, won, is_copy) or (t0, won, is_copy, resolved_unix).
    Callers that can supply the journaled resolution time SHOULD — without it
    every row falls back to legacy treatment and the causal window is a no-op.

    LOSSES never appear in the output; they influence a win's qualification only
    through the hit-rate they contribute to the window up to that point. This is
    what decouples "a loss" from "loses emission": a loss can only un-qualify the
    miner (freezing accrual of FUTURE qualified wins), never erase banked ones.

    For a habitual copier, copied wins are excluded (mirrors trailing_wins).
    """
    warmup_end = first_seen_unix + config.IMMUNITY_S
    dec = sorted(decisive, key=lambda d: d[0])
    out: list[tuple[float, float]] = []
    for i, row in enumerate(dec):
        t0, won, cp = row[0], row[1], row[2]
        if not won or t0 < warmup_end or (habitual and cp):
            continue
        rep_cut = t0 - config.HIT_RATE_WINDOW_S
        # cap resolved at THIS win's t0 — a win that scored under the old cap keeps
        # the verdict it was given, exactly like the bands
        if config.causal_qwin_enforced_as_of(t0):
            # dec[:i] and not dec, because resolution always postdates a row's own
            # t0: anything resolved by this t0 necessarily opened before it.
            window = [d for d in dec[:i] if d[0] >= rep_cut and _resolved_by(d, t0)]
            window.append(row)                       # the win itself always counts
        else:
            window = [d for d in dec[:i + 1] if d[0] >= rep_cut]
        window = window[-config.hit_rate_window_trades_as_of(t0):]
        rw = sum(1 for d in window if d[1])
        rd = len(window)
        if _qualifies(rw, rd):
            # Efficiency is applied OUTSIDE the max(1.0, ...) floor on purpose. Most
            # of the field sits at exactly 1.00x, so folding it inside would clamp
            # the penalty away for everyone it is meant to reach.
            eff = efficiency_multiplier(graded, t0) if graded else 1.0
            out.append((t0, max(1.0, tier_multiplier(rw, rd, t0)) * eff))
    return out


def qualified_calls(decisive: list[tuple], first_seen_unix: float,
                    habitual: bool = False,
                    sigma_for=None) -> list[tuple[float, float]]:
    """Post-warmup calls scored in SIGNED POINTS, as [(t0, points)].

    The points analogue of qualified_wins, and it differs in one way that
    matters: LOSSES ARE IN IT. A signed score whose losses are dropped is not a
    signed score, and the no-view-earns-zero property -- the thing the whole
    scheme rests on -- is false the moment one side is filtered.

    The point-in-time qualify gate applies to BOTH sides for the same reason. If
    only wins were gated, a miner would bank wins while qualified and shed
    losses while not, which is a free ratchet.

    Each row is (t0, won, is_copy, resolved_unix, tp_bps, horizon_s). The band
    is at 4 and 5; index 3 is resolved_unix and is read by _resolved_by -- see
    tests/test_decisive_tuple_layout.py for why that ordering is asserted.
    A row with no band falls back to the board via the caller, and a row we
    cannot price at all contributes nothing rather than a guess.
    """
    warmup_end = first_seen_unix + config.IMMUNITY_S
    dec = sorted(decisive, key=lambda d: d[0])
    out: list[tuple[float, float]] = []
    for i, row in enumerate(dec):
        t0, won, cp = row[0], row[1], row[2]
        if t0 < warmup_end or (habitual and cp):
            continue
        tp = row[4] if len(row) > 4 else None
        hz = row[5] if len(row) > 5 else None
        if not tp or not hz:
            continue                     # unpriceable: contribute nothing
        rep_cut = t0 - config.HIT_RATE_WINDOW_S
        if config.causal_qwin_enforced_as_of(t0):
            window = [d for d in dec[:i] if d[0] >= rep_cut and _resolved_by(d, t0)]
            window.append(row)
        else:
            window = [d for d in dec[:i + 1] if d[0] >= rep_cut]
        window = window[-config.hit_rate_window_trades_as_of(t0):]
        rw = sum(1 for d in window if d[1])
        rd = len(window)
        if not _qualifies(rw, rd):
            continue
        # SIGMA COMES FROM THE PAIR'S BOARD ROW, never from this call's own band.
        # sigma_from_board(tp, hz) on the call's OWN numbers is circular: it
        # returns tp/(z_ref*sqrt(hz)), so z is always z_ref and every shape
        # prices at the same 1.326 no matter how bold. Caught by an end-to-end
        # check where a miner drawing twice the band on a quarter of the clock
        # scored identically to one taking the board.
        #
        # scoring.py must not import hf (hf imports scoring), so the board lookup
        # arrives as a callable from the caller that owns the board.
        pair = row[6] if len(row) > 6 else None
        sigma = sigma_for(pair, t0) if (sigma_for and pair) else 0.0
        if sigma <= 0:
            continue                     # unpriceable without a board sigma
        pts = signed_points("won" if won else "lost",
                            float(tp), int(hz), sigma)
        out.append((t0, pts))
    return out


def wash_surprise(resolved: list[tuple[float, bool, float]]) -> float:
    """How far a miner's washes exceed what their own shapes predicted.

    `resolved` is [(t0, was_wash, q)] where q = 1 - p_res(z) is the wash
    probability the miner DECLARED by drawing that shape. Returns a standard
    score; only positive values mean anything, and 0 when there is nothing to
    judge.

    Neutral to band width by construction: a wider band raises q as much as it
    raises the wash count, so drawing bold does not buy expected punishment.
    That is the whole reason this is measured against the declaration rather
    than against a fixed prior -- efficiency_multiplier's fixed 0.40 charges a
    miner for choosing wide on top of the payout that already prices it.
    """
    n = len(resolved or ())
    if n == 0:
        return 0.0
    obs = sum(1 for _t, w, _q in resolved if w)
    exp = sum(q for _t, _w, q in resolved)
    var = sum(q * (1.0 - q) for _t, _w, q in resolved)
    if var <= 0.0:
        return 0.0                       # every shape was a near-certainty
    return (obs - exp) / math.sqrt(var)


def wash_debt(tally: float, surprise: float, standing_pct: float = 0.0,
              window_s: float | None = None) -> float:
    """Points deducted for washing beyond what the miner's shapes predicted.

    Zero unless the surprise clears HF_WASH_EXCESS_Z, so an honest miner washing
    at their declared rate never pays. Sized as HF_WASH_DEBT_HOURS of that
    miner's OWN recent earning rate, so it self-scales instead of needing a
    constant that would be wrong for every miner but one.

    standing_pct is the miner's percentile in the field (0 = bottom, 1 = top).
    The top of the board pays nothing and the bottom pays the full debt, which
    is the ranked behaviour asked for: a good miner who washes sits above a
    worse one who washes at the same moment. Continuous, so nobody sits on a
    threshold.

    NEVER eliminates. A wash says nothing about whether the miner was right, so
    elimination stays the lifetime Wilson upper bound.
    """
    if surprise < config.HF_WASH_EXCESS_Z:
        return 0.0
    W = config.HF_POINTS_WINDOW_S if window_s is None else window_s
    if W <= 0:
        return 0.0
    day_rate = abs(tally) * (config.HF_WASH_DEBT_HOURS * 3600.0) / W
    ranked = max(0.0, min(1.0, 1.0 - standing_pct))
    return day_rate * ranked * config.HF_WASH_DEBT_MAX_FRAC


def decayed_points_tally(calls: list[tuple[float, float]], now_unix: float) -> float:
    """Signed, time-decayed points over the rolling window. May be NEGATIVE.

    compute_weights clamps at zero before this becomes a weight -- a negative
    weight has no meaning on chain -- but the clamp belongs THERE and not here.
    A tally that clamped itself would hide how far underwater a miner is from
    every caller that is not building a weight vector.

    The per-day cap keeps the FIRST HF_POINTS_DAILY_CAP calls of each UTC day.
    Chronological, never best-of: a cap that kept a miner's best calls would let
    them shed losses after the fact.
    """
    W = config.HF_POINTS_WINDOW_S
    live = [(t0, p) for t0, p in calls if 0.0 <= now_unix - t0 < W]
    live.sort(key=lambda x: x[0])
    per_day: dict[int, int] = {}
    total = 0.0
    for t0, p in live:
        day = int(t0 // 86_400)
        n = per_day.get(day, 0)
        if n >= config.HF_POINTS_DAILY_CAP:
            continue
        per_day[day] = n + 1
        total += p * (1.0 - (now_unix - t0) / W)
    return total


def decayed_qwin_tally(qwins: list[tuple[float, float]], now_unix: float) -> float:
    """Time-decayed, tier-weighted tally of a miner's qualified wins — the emission
    size. Each win decays LINEARLY to zero over EMISSION_DECAY_S (1 week): a win
    posted now is worth its full tier weight, one EMISSION_DECAY_S old is worth 0. Only the most
    recent WIN_CAP live wins count (conviction over grind — beyond the cap, edge/
    tier differentiates, not raw volume). Relative quantity — compute_weights
    normalizes it across the field."""
    W = config.EMISSION_DECAY_S
    live = [(t0, tw) for t0, tw in qwins if 0.0 <= now_unix - t0 < W]
    live.sort(key=lambda x: x[0], reverse=True)          # most recent first
    return sum(tw * (1.0 - (now_unix - t0) / W) for t0, tw in live[:config.WIN_CAP])
