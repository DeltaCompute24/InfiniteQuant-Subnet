"""Validity filters + weight computation (SPEC §6.4, §7).

Pure functions over journaled state so every validator computes identical
weights from identical inputs.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from . import config


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
def apply_validity_filters(rows: list[GradedRow]) -> list[GradedRow]:
    """Voids rows in-place per §6.4 and returns the list (ordered by t0, then
    hotkey as the same-block tiebreak — callers must pre-sort identically).

    Voided rows: not graded, don't count toward quotas, no strike.
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

        # ≥ gap since this hotkey's last ACCEPTED commit
        if gap and times and r.t0_unix - times[-1] < gap:
            r.status, r.void_reason = "void", "min_spacing"
            continue

        # ≤ cap per UTC day
        day = int(r.t0_unix // 86_400)
        if sum(1 for t in times if int(t // 86_400) == day) >= cap:
            r.status, r.void_reason = "void", "daily_quota"
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
    copies = sum(1 for _, _, cp in rows if cp)
    last_cp = max((t for t, _, cp in rows if cp), default=None)
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


def referral_pair_suspended_until(pair_rows: list[GradedRow], hk_a: str, hk_b: str,
                                  now_unix: float) -> float | None:
    """Pair-scoped no-copy gate: the unix time until which the (hk_a, hk_b)
    referral bonus is SUSPENDED, or None if it isn't. Pure/deterministic.

    Deliberately far stricter than the global §7.5 detector — the pair opted
    into scrutiny by pairing up, and a trip here only pauses the bonus (base
    emission is untouched, no public flag). Two triggers, either direction:

      * SHARP shadowing — same (pair, direction) commits within
        COPY_SHARP_LAG_S, collapsed into decisions by _episodes();
        ≥ REFERRAL_PAIR_SHARP_EPISODES trips.
      * LIVE-OVERLAP follows — mark_copies restricted to the pair (leader
        eligibility bypassed: both parties consented), episode-collapsed;
        ≥ REFERRAL_PAIR_OVERLAP_EPISODES trips.

    Events are counted over the trailing REFERRAL_PAIR_WINDOW_S; a trip
    suspends until REFERRAL_PAIR_TTL_S after the LAST event, then self-clears
    (same philosophy as COPY_PENALTY_TTL_S — a warning, not a scarlet letter).
    """
    cutoff = now_unix - config.REFERRAL_PAIR_WINDOW_S
    pair_hk = {hk_a, hk_b}
    rows = [r for r in pair_rows
            if r.hotkey in pair_hk and r.status != "void" and r.t0_unix >= cutoff]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.t0_unix, r.hotkey))

    suspended_until: float | None = None

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
        if _episodes(ts, config.COPY_EPISODE_S) >= config.REFERRAL_PAIR_SHARP_EPISODES:
            until = max(ts) + config.REFERRAL_PAIR_TTL_S
            suspended_until = max(suspended_until or 0.0, until)

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
        if _episodes(ts, config.COPY_EPISODE_S) >= config.REFERRAL_PAIR_OVERLAP_EPISODES:
            until = max(ts) + config.REFERRAL_PAIR_TTL_S
            suspended_until = max(suspended_until or 0.0, until)

    if suspended_until is not None and now_unix < suspended_until:
        return suspended_until
    return None


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
    recent = recent[-config.HIT_RATE_WINDOW_TRADES:]
    rep_wins = sum(1 for _, won, _ in recent if won)
    rep_decisive = len(recent)

    tw_all = tw_orig = tcopies = tdec = 0
    for t0, won, is_copy in decisive:
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
                    referral_pairs: list[tuple[str, str]] | None = None) -> dict[int, float]:
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

    referral_pairs (§ referral): VALID, UNSUSPENDED (recruiter, recruit) hotkey
    pairs — the caller (replay.weights_from_journal) has already applied
    valid_referral_pairs + referral_pair_suspended_until. While BOTH sides have
    a positive base tally and neither is excluded, the recruit's effective tally
    gains REFERRAL_RECRUIT_BONUS × its own base and the recruiter's gains
    REFERRAL_RECRUITER_BONUS × the recruit's base (total capped at
    REFERRAL_MAX_X × the recruiter's own base). Bonuses are computed from BASE
    tallies only — never from boosted ones — so A→B→C referral chains cannot
    compound. Pure share-shifting within the miner pool: the pro-rata split
    below normalizes over boosted tallies, and the emission cap is unchanged.

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
    def qtally(s: MinerState) -> float:
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
        per_recruiter: dict[str, float] = defaultdict(float)
        for recruiter, recruit in referral_pairs:
            r, c = by_hk.get(recruiter), by_hk.get(recruit)
            if r is None or c is None:          # deregistered/eliminated/struck → lapses
                continue
            if r.uid in excluded_uids or c.uid in excluded_uids:
                continue                        # globally copy-zeroed → lapses
            if base[recruiter] <= 0 or base[recruit] <= 0:
                continue                        # mutual-conditional: both must be earning
            bonus[recruit] += config.REFERRAL_RECRUIT_BONUS * base[recruit]
            per_recruiter[recruiter] += config.REFERRAL_RECRUITER_BONUS * base[recruit]
        for hk, b in per_recruiter.items():
            bonus[hk] += min(b, config.REFERRAL_MAX_X * base[hk])

    def eff(s: MinerState) -> float:
        return base[s.hotkey] + bonus.get(s.hotkey, 0.0)

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
    cap = config.MINER_EMISSION_CAP
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


def tier_multiplier(rep_wins: int, rep_decisive: int) -> float:
    """Per-win tier from config.WIN_RATE_TIERS applied to the SHRUNK hit-rate, so a
    thin-sample hot streak can't grab a high tier. Returns 0.0 below the lowest tier."""
    r = shrunk_hit_rate(rep_wins, rep_decisive)
    for threshold, mult in config.WIN_RATE_TIERS:
        if r >= threshold:
            return mult
    return 0.0


def _qualifies(rep_wins: int, rep_decisive: int) -> bool:
    """Dispatch the qualify gate on the CONFIDENCE_SCORING flag."""
    if config.CONFIDENCE_SCORING:
        return is_qualified(rep_wins, rep_decisive)
    return is_qualified_legacy(rep_wins, rep_decisive)


def qualified_wins(decisive: list[tuple[float, bool, bool]], first_seen_unix: float,
                   habitual: bool = False) -> list[tuple[float, float]]:
    """Post-warmup WINs the miner earned WHILE QUALIFIED, each tagged with the
    point-in-time tier weight (≥ 1.0). PURE / deterministic.

    A win at t0 counts iff the miner's reputation-window hit-rate AS OF t0 (the
    trailing HIT_RATE_WINDOW_S ending at t0, capped at HIT_RATE_WINDOW_TRADES most
    recent decisive outcomes, the win itself included) passes the qualify gate —
    "a qualified win." A win earned while unqualified is skipped and never earns.
    The weight is the tier the miner held at that moment (WOLF 2× / SHARP 1.2× /
    base 1×), floored at 1.0 so a gate-passing win always counts at least base.

    LOSSES never appear in the output; they influence a win's qualification only
    through the hit-rate they contribute to the window up to that point. This is
    what decouples "a loss" from "loses emission": a loss can only un-qualify the
    miner (freezing accrual of FUTURE qualified wins), never erase banked ones.

    For a habitual copier, copied wins are excluded (mirrors trailing_wins).
    """
    warmup_end = first_seen_unix + config.IMMUNITY_S
    dec = sorted(decisive, key=lambda d: d[0])
    out: list[tuple[float, float]] = []
    for i, (t0, won, cp) in enumerate(dec):
        if not won or t0 < warmup_end or (habitual and cp):
            continue
        rep_cut = t0 - config.HIT_RATE_WINDOW_S
        window = [d for d in dec[:i + 1] if d[0] >= rep_cut][-config.HIT_RATE_WINDOW_TRADES:]
        rw = sum(1 for _, w2, _ in window if w2)
        rd = len(window)
        if _qualifies(rw, rd):
            out.append((t0, max(1.0, tier_multiplier(rw, rd))))
    return out


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
