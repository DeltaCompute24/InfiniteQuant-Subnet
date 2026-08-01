"""SN89 Signals — protocol constants.

Everything a miner or validator needs to agree on lives here. Values marked
CONSENSUS must be identical across all participants or grading diverges.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ── Network ──────────────────────────────────────────────────────────────────
NETUID = int(os.getenv("SN89_NETUID", "89"))          # mainnet 89; override on testnet
NETWORK = os.getenv("SN89_NETWORK", "finney")          # "test" for testnet
TEMPO = int(os.getenv("SN89_TEMPO", "360"))

# ── drand quicknet (CONSENSUS — same beacon MANTIS uses) ─────────────────────
DRAND_API = "https://api.drand.sh/v2"
DRAND_BEACON_ID = "quicknet"
DRAND_PERIOD_S = 3
DRAND_GENESIS_TIME = 1692803367
DRAND_PUBLIC_KEY = (
    "83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c"
    "8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e17e63aaf4bcb"
    "5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a"
)
ALG_LABEL = "x25519-hkdf-sha256+chacha20poly1305+drand-tlock"

# ── Envelope / reveal (CONSENSUS) ────────────────────────────────────────────
# Reveal delay cut 24h→2h (2026-07-12). A competitor reading the plaintext 2h in
# enters a stale, mostly-played-out move (crypto grade window is 8h) — a losing
# entry — so the longer timelock guarded no real edge, while the 24h gap between a
# trade resolving in-bot and revealing on-chain confused traders ("won but the
# validator shows 0"). MIGRATION: the validator accepts BOTH the new 2h round and
# the legacy 24h round (ACCEPTED_REVEAL_DELAYS_S), so in-flight 24h commits and
# hosted miners that haven't bumped config yet keep validating instead of voiding.
# Drop REVEAL_DELAY_LEGACY_S from the tuple once nothing committed before the flip
# remains sealed (≥24h after cutover).
REVEAL_DELAY_S = 2 * 3600           # tlock round = commit time + 2h
REVEAL_DELAY_LEGACY_S = 24 * 3600   # pre-2026-07-12 delay; ACCEPTED during migration
ACCEPTED_REVEAL_DELAYS_S = (REVEAL_DELAY_S, REVEAL_DELAY_LEGACY_S)
ROUND_TOLERANCE_S = 600             # blob round must be within ±10min of expected
                                    # (MANTIS UID-46 lesson: wrong round ⇒ void, never hang)
REVEAL_GRACE_S = int(os.getenv("SN89_REVEAL_GRACE_S", str(6 * 3600)))
                                    # §6.4 forfeit: a commitment whose blob is still
                                    # unfetchable this long AFTER its round matures is
                                    # a FORFEIT LOSS — the timelock hides a signal from
                                    # others but never excuses non-revelation, so a miner
                                    # can't commit, watch the market, then publish only
                                    # winners. Grace absorbs transient hosting/poll gaps;
                                    # a blob we already captured is never forfeited.

# Subnet owner X25519 public key (hex, 32 bytes). Miners encrypt W_owner to this.
# The corresponding private key is held by the subnet owner.
OWNER_PK_HEX = os.getenv(
    "SN89_OWNER_PK_HEX",
    # SN89 owner X25519 public key. Miners MUST wrap W_owner to this so the owner
    # can read submissions in real time (the network only sees them after the 24h
    # drand timelock). Validators void any reveal whose owner_pk ≠ this value, so
    # a miner can't opt out of owner visibility while still earning.
    "ec07fb1cc394f820e16059e17c386337f784963fc51e32f271a64c706b302d2a",
)

# ── Submission rules (CONSENSUS — §6.4 of SPEC) ──────────────────────────────
# Current-era limits: up to MAX_SIGNALS_PER_UTC_DAY per hotkey per UTC day,
# with at least MIN_GAP_S between a hotkey's ACCEPTED commits. Self-overlap
# stays allowed — a hotkey may hold multiple open calls on the same
# (pair, direction), and long-then-short on one pair is legal.
#
# Rules are AS-OF versioned like the bands: apply_validity_filters resolves
# the (cap, gap) in force at each signal's OWN t0 via submission_rules_as_of(),
# so a rules change NEVER retroactively voids signals accepted under the old
# rules. This gate is load-bearing — grade_revealed re-runs the validity
# filters over the whole journal every cycle, so an ungated tighten would
# rewrite historical records and break replay parity.
MAX_SIGNALS_PER_UTC_DAY = 3
MIN_GAP_S = 3600
# (effective_from_unix, max_per_utc_day, min_gap_s) — ascending, CONSENSUS.
# Era 1: the 2026-06-23 loosening (flat 6/day, no gap — 90f412d + c8ee1d4,
#        meant to let traders re-establish qualification quickly).
# Era 2: the 2026-07-18T00:00:00Z revert to 3/day + 1h gap (Whit 2026-07-17:
#        it worked better as it was; conviction over volume).
SUBMISSION_RULES_HISTORY = (
    (0, 6, 0),
    (1784332800, MAX_SIGNALS_PER_UTC_DAY, MIN_GAP_S),   # 2026-07-18T00:00:00Z
)


def submission_rules_as_of(t0_unix: float) -> tuple[int, int]:
    """(max_per_utc_day, min_gap_s) in force at t0 — mirrors bands_as_of."""
    cap, gap = SUBMISSION_RULES_HISTORY[0][1], SUBMISSION_RULES_HISTORY[0][2]
    for eff, c, g in SUBMISSION_RULES_HISTORY:
        if t0_unix >= eff:
            cap, gap = c, g
    return cap, gap


# Min-gap block-jitter tolerance (CONSENSUS). The min-gap rule is checked against
# T0 = the commitment's INCLUSION-BLOCK timestamp, which is quantized to the 12 s
# block time. A miner submitting exactly min_gap apart by its own clock can still
# have its two commits land in blocks up to one block closer together, so the
# on-chain gap comes out as much as ~12 s under min_gap — voiding an honest signal
# for `min_spacing` through no fault of the miner (observed: Canefis XAGUSD 2026-
# 07-23 10:00, real submit gap 3602 s, on-chain gap 3599 s, voided by 1 s). Void
# only when the gap is short by MORE than this slack. 30 s covers one block of
# jitter plus a couple of blocks of inclusion delay; against a 3600 s rule it is
# 0.8 %, so it cannot be used to submit meaningfully faster than hourly.
SUBMISSION_GAP_SLACK_S = int(os.getenv("SN89_SUBMISSION_GAP_SLACK_S", "30"))


MAX_HORIZON_H = 72                  # upper bound / overlap cap
DEFAULT_HORIZON_H = 72              # fallback for an unknown asset class

# Grade window (wash time) is FIXED BY ASSET CLASS, not miner-chosen — mirrors
# the IQ Signals program. Crypto was cut 30h→8h (2026-06-24) to align with the
# forex/metals book: the prior 1.75×/30h "follow-the-move" calibration was
# dropped, so crypto now uses its plain vol unit (1.0×, in the bands file) on a
# short window like fx. A miner's horizon_h field is normalized to its class value.
CLASS_HORIZON_H = {
    "crypto": 8,
    "forex": 12,
    "forex-commodities": 12,   # metals (XAU/XAG/XPT/XPD)
    "equities": 48,
}


def class_horizon_h(asset_class: str) -> int:
    return CLASS_HORIZON_H.get(asset_class, DEFAULT_HORIZON_H)


LATENCY_BUFFER_S = 0                # entry anchor = commit-block timestamp (buffer removed 2026-07-10 — the ~1-block inclusion delay already defeats sub-second wick sniping; entry can no longer be micro-timed by the miner)
ENTRY_SECOND_SCAN_S = 120           # scan window for the 1-second entry bar; if no
                                    # 1s bar lands in it (sparse FX/metals off-hours)
                                    # fall back to the first 1-minute bar open

# ── Grading (CONSENSUS) ──────────────────────────────────────────────────────
WICK_TOL = 0.01                     # CRYPTO: candle extreme >1% beyond body + both
                                    # neighbours = uncorroborated spike; clamped
                                    # unless a second feed (Hyperliquid) traded the
                                    # level the same minute
WICK_TOL_NONCRYPTO = 0.0025         # forex/metals/equities bands are tens of bps,
                                    # so a 1% gate lets a 0.5% rogue rollover wick
                                    # (#665 CADJPY 2026-06-10) through while being 2×
                                    # the whole SL band. A 1-min non-crypto move
                                    # beyond BOTH neighbours by >0.25% is data error.
# Polygon's consolidated forex feed is untrustworthy through the 5pm-ET daily
# rollover (liquidity vanishes, one contributor prints 20-60bps phantom wicks for
# ~15-20 min). Non-crypto bars in this UTC window are dropped from touch grading;
# a real breach persists past the window and is caught on the next clean bar.
FOREX_ROLLOVER_UTC = ((20, 55), (21, 20))   # [20:55, 21:20)

# ── Bracket-hit rule (CONSENSUS) ─────────────────────────────────────────────
# A level (TP/SL) is HIT when a 1-minute candle's CLOSE crosses it, not when the
# intrabar high/low wick pierces it. A practically tradeable fill requires price
# to PERSIST through the level; a single tick into the wick that reverts by the
# close is not a fill anyone could capture. This mirrors the SN8/PTN standard
# (a fill is priced off the traded/quoted price, never a candle wick) and needs
# only the 1-minute aggregates — no 1-second confirmation feed and no
# availability-dependent fallback, so every validator replays the grade
# identically. See grader.py.

# ── Grading rule (CONSENSUS, as-of versioned) ────────────────────────
# A level is HIT when a traded/quoted price TOUCHES it. Two substrates have been
# used to decide that:
#
#   "close_1m"    a 1-minute aggregate CLOSE crosses the level  (2026-07-20, 3d79274)
#   "touch_ticks" any tick in the anchored series touches it    (the HF rule)
#
# close_1m was never a principle. It was a prosthetic for an untrustworthy
# substrate: the high/low of a Polygon 1-minute bar can be a single untradeable
# print with no depth behind it, which is what stopped USDCAD #3546. Discarding
# every intrabar price was a blunt way to reject one bad print, and it also made
# wide TPs strictly harder to earn.
#
# It was justified in-code as matching the SN8/PTN standard. THAT WAS FALSE --
# PTN fires brackets on the EXTREMUM of a 30 s websocket window
# (order_trigger.py: LONG SL min_bid <= SL, LONG TP max_bid >= TP). PTN grades on
# the touch; we graded on the close and cited them as authority for it.
#
# touch_ticks fixes #3546 at the source instead: that 1-pip wick never appeared on
# the tick feed at all, so a touch rule reading real quotes would not have stopped
# him either. It also unifies both mechanisms on ONE rule -- LF and HF then differ
# only in bands, horizon, and which series they are handed.
#
# NEVER retroactive: resolved as-of the call's own t0, exactly like the bands, so
# arming it can never re-grade a call that already scored.
# ARMED 2026-07-24 (Whit): LF moves from close_1m to touch_ticks, unifying it with
# HF on one hit rule (a call wins when a real quoted tick TOUCHES TP; the tick feed
# already excludes the bad prints close_1m was a prosthetic for). Forward-only
# cutover 2026-07-25T00:00:00Z so no in-flight call changes rule mid-flight; a call
# committed before the cutover grades entirely under close_1m. Committed default (not
# an env var) so master == the live validator's rule (single-validator replay contract).
TOUCH_TICKS_FROM = int(os.getenv("SN89_TOUCH_TICKS_FROM", "1784937600"))

# Minimum ticks that must TOUCH a level before it scores under touch_ticks (Whit
# 2026-07-24). A lone tick that pierces TP/SL and reverts is a wick, not a fill
# anyone could capture — requiring ≥2 touches rejects it while a genuine touch
# (which lingers for many ticks) still scores. Grades off the tick's mid price
# `p` — the same series the trader's card shows as "mark", so card == grade.
MIN_TOUCH_TICKS = int(os.getenv("SN89_MIN_TOUCH_TICKS", "2"))


def grading_rule_as_of(t0_unix: float) -> str:
    """Which hit rule governs a call committed at t0."""
    if TOUCH_TICKS_FROM and t0_unix >= TOUCH_TICKS_FROM:
        return "touch_ticks"
    return "close_1m"


# ── Scoring (CONSENSUS — §7 of SPEC) ─────────────────────────────────────────
SCORE_WINDOW_S = 30 * 24 * 3600     # ELIMINATION / reporting window: the trailing
                                    # span for the elimination hit-rate floor and the
                                    # trailing_* reporting counters. (Distinct from
                                    # the emission decay and hit-rate windows below.)

# EMISSION decay window. A qualified win decays LINEARLY to zero over this span:
# full tier weight at the moment it lands, zero once it is EMISSION_DECAY_S old.
# One week — a win pays for a week and then it is gone, so a miner's share tracks
# what it did THIS week, not what it did a month ago. Recency is the whole point:
# emissions must follow current trading, and a stale book cannot coast.
EMISSION_DECAY_S = int(os.getenv("SN89_EMISSION_DECAY_S", str(7 * 24 * 3600)))

# Hit-rate REPUTATION window. The qualify gate and the per-win tier are computed
# over a ROLLING window — the trailing HIT_RATE_WINDOW_S, capped at the most
# recent HIT_RATE_WINDOW_TRADES decisive outcomes (whichever is tighter) — NOT
# over lifetime. Reputation decays: a once-great miner who starts trading badly
# loses its tier and falls below the gate as the good history ages out, and old
# wins can't prop up poor recent trading. A large, stable sample (≈2 months /
# ≈100 trades) so the tier doesn't flip on a short hot/cold streak.
HIT_RATE_WINDOW_S = 60 * 24 * 3600          # ≈2 months of history
HIT_RATE_WINDOW_TRADES = 100                # …capped at the most recent ~100 decisive

# The trade cap was silently shortening the window. Reputation is meant to span
# HIT_RATE_WINDOW_S (60 days); the cap is only there so a burst of activity can't
# make the sample unboundedly large. But at the 3/day allowance a full-cadence
# trader posts ~2.4 decisive/day, reaching 100 in ~42 days — so the CAP bound
# first and their reputation quietly covered 42 days, not 60. Trading more often
# should never shorten how far back a miner's record is read.
#
# 200 is above the theoretical maximum (3/day x 60d = 180 at zero wash), so the
# 60-day clock is always what binds.
#
# AS-OF VERSIONED, and never retroactive: qualified_wins resolves the cap at each
# WIN's own t0, so a win that scored under the old cap keeps the verdict it was
# given. Measured before shipping: the busiest miner on the subnet has 59 decisive
# in the 60-day window, so NO current miner is at the cap and no tally moves on
# the effective date. This is a latent defect being closed, not a live one.
HIT_RATE_WINDOW_TRADES_V2 = 200
HIT_RATE_WINDOW_TRADES_V2_FROM = int(
    os.getenv("SN89_HIT_RATE_TRADES_V2_FROM", "1784937600"))   # 2026-07-25T00:00:00Z


# ── cross-mechanism pair lock, LF side (CONSENSUS, as-of versioned) ──────────
# A pair a hotkey trades on ONE mechanism is locked on the other for
# hf.PAIR_LOCK_S. The HF side enforces synchronously at ingest (a signed refusal,
# the call never exists). The LF side cannot: an LF commit is already on chain and
# immutable by the time we can see it, so the only available action is to VOID it
# at reveal, with a reason and no strike.
#
# That asymmetry is inherent to the two transports, not a design choice, and it
# should be stated plainly to miners: on HF you are told no; on LF the call is
# accepted on chain and then voided.
#
# Replayability: the lock resolves from the PUBLISHED, anchored HF receipt logs,
# never from our private ingest state, so any third party reaches the same verdict.
# There is no race — LF validity is evaluated at reveal (2 h after commit) and HF
# windows anchor within 180 s, so every relevant HF window is long since sealed.
#
# 0 = not enforced. Arm only once HF receipts are actually being published, or
# every LF call would be judged against an empty lock set and nothing would void.
#
# ARMED 2026-07-24T04:00:00Z. The precondition above is met: HF windows publish to
# HF_PUBLIC_BASE and, since 03:30 UTC that day, anchor on chain. Until this was
# armed the lock ran on ONE side — hf_ingest refused an HF submit on a pair
# recently used on LF, while an LF call on a pair just used on HF graded and paid
# normally, which is the double-pay the rule exists to stop, in the direction the
# bot told users was closed. The default lives here rather than in the unit's
# environment on purpose: an env-only arming would make the live validator
# enforce a rule master does not, and on a single-validator subnet master IS the
# replay contract.
PAIR_LOCK_LF_FROM = int(os.getenv("SN89_PAIR_LOCK_LF_FROM", "1784865600"))


def pair_lock_lf_enforced_as_of(t0_unix: float) -> bool:
    """Whether an LF call at t0 is subject to the cross-mechanism lock."""
    return bool(PAIR_LOCK_LF_FROM and t0_unix >= PAIR_LOCK_LF_FROM)


def hit_rate_window_trades_as_of(t0_unix: float) -> int:
    """Reputation trade cap in force at t0 (mirrors bands_as_of)."""
    if HIT_RATE_WINDOW_TRADES_V2_FROM and t0_unix >= HIT_RATE_WINDOW_TRADES_V2_FROM:
        return HIT_RATE_WINDOW_TRADES_V2
    return HIT_RATE_WINDOW_TRADES

# ── Confidence-based scoring (CONSENSUS) ─────────────────────────────────────
# Master switch between the legacy point-estimate rules (raw hit-rate gate / tier
# / rolling-window elimination floor) and the confidence-based rules (Wilson
# lower-bound gate, beta-shrunk tier, lifetime upper-bound elimination, win cap).
# Default ON. Flip to "0" to A/B the legacy path on testnet. The legacy functions
# stay importable (suffixed *_legacy) so the §8 diff script can compare them.
CONFIDENCE_SCORING = bool(int(os.getenv("SN89_CONFIDENCE_SCORING", "1")))

QUALIFY_MIN_DECISIVE = int(os.getenv("SN89_QUALIFY_MIN_DECISIVE", "8"))  # decisive (won+lost) IN THE WINDOW: a small sanity
                                    # floor only — the Wilson bound below handles small-n
                                    # itself (was 10 under the legacy raw-hit gate).
QUALIFY_MIN_HIT = 0.55             # LEGACY raw-hit gate (CONFIDENCE_SCORING=0 path only).
QUALIFY_Z = 1.2816                 # one-sided ~90 % confidence for the qualify gate
QUALIFY_LB_FLOOR = 0.50            # qualify when the Wilson lower bound on the true
                                    # hit-rate ≥ this (i.e. 90 % sure you beat a coin flip)

# Tier multiplier prior. The per-win tier (WIN_RATE_TIERS) is applied to a
# beta-shrunk posterior mean (prior Beta(K/2, K/2) centred on 0.50), NOT the raw
# rate, so a thin-sample hot streak (e.g. 9/4) is pulled toward a coin flip and
# can't grab WOLF off 13 trades; a large sample (60/100) barely moves.
TIER_PRIOR_K = 12                  # beta prior strength; higher = more small-n shrinkage

# Emission win cap (conviction over grind). A miner's trailing-30d wins are capped
# at WIN_CAP before the tier multiplier, so above the cap only edge/tier (not raw
# volume) differentiates. Set to the monthly win output of the intended ~1 call/day
# cadence (~30 decisive/mo at a WOLF hit rate). Below the cap extra wins still help.
WIN_CAP = 20

# Per-win multiplier by reputation-window hit-rate tier — each recent win is worth
# more the higher your windowed hit-rate, so quality is rewarded above the gate,
# not just volume. Checked high → low; the first threshold met wins. Under the
# confidence path these thresholds apply to the SHRUNK rate (see TIER_PRIOR_K).
# Mirrors the IQ Signals tiers: QUALIFIED 55 % (1×) · SHARP 60 % (1.2×) · WOLF 70 % (2×).
WIN_RATE_TIERS = (
    (0.70, 2.0),   # WOLF
    (0.60, 1.2),   # SHARP
    (0.55, 1.0),   # QUALIFIED
)
# Continuous tier ramp cutover (Whit 2026-07-24). A win earned at t0 >= this unix
# is valued on a CONTINUOUS linear interpolation THROUGH the WIN_RATE_TIERS anchors
# rather than the discrete band — removing the tier cliff (shrunk 69.9% pays 1.2×
# and 70.0% pays 2.0× today; the ramp fills the gap so a maturing high-raw miner
# earns between SHARP and WOLF instead of being pinned to SHARP). At an exact anchor
# the ramp EQUALS the discrete tier, so WOLF/SHARP boundaries are unchanged. AS-OF
# gated: every already-banked win keeps its original discrete stamp and the public
# journal replays byte-identical before the cutover. = 2026-07-25T00:00:00Z.
CONTINUOUS_TIER_FROM = 1784937600

# ── Efficiency (wash) multiplier — scoring.efficiency_multiplier ─────────────
# The per-win VALUE is tier_multiplier x efficiency. The tier prices DIRECTION
# (hit rate); efficiency prices whether the calls actually MOVED. A miner who is
# right 70% of the time but fires mostly at setups that never travel the band is
# consuming grading capacity and paying the field nothing, and until now that cost
# it exactly zero.
#
# Measured on 2471 graded rows / 98 miners (2026-07-26): wash rate is a real miner
# attribute, not luck — chi2/df 2.29, z=+6.0 against the field-constant null. It
# survives asset-mix adjustment (z=+3.1), it is MORE reliable than hit rate
# (54-55% of observed spread is real vs 45%), and it is ORTHOGONAL to hit rate
# (r=-0.03), so it adds information rather than re-pricing the tier.
#
# NOT asset-adjusted, deliberately. Equalising wash across assets is the BANDS'
# job (scripts/recalibrate_bands.py holds structural wash constant per asset);
# adjusting here instead would hide a miscalibrated board and entrench it.
# EFFICIENCY_BASELINE_WASH must therefore be recalibrated whenever the band target
# moves, and this multiplier must NOT be armed while the board is still unequalised
# — until then it would price asset choice, not skill.
# ARMED 2026-07-26T06:00:00Z, the instant the equal-wash board
# (volnorm-equalwash40-20260726) took effect. CONSENSUS: this default is the
# value, not the env — .env.main is gitignored, so an arming instant that lived
# only there would leave every validator that pulls master computing weights
# with the multiplier OFF. Same pattern as TOUCH_TICKS_FROM/CONTINUOUS_TIER_FROM.
EFFICIENCY_FROM = int(os.getenv("SN89_EFFICIENCY_FROM", "1785045600"))

# The scale is anchored at PERFECTION, not at the field average: zero washes is the
# highest bar and pays the full 1.0. There is no bonus tier — a miner cannot earn
# MORE than their tier for being efficient, they can only lose for being wasteful.
# Anchoring on the field average instead would make the rule relative to whoever
# else happens to be mining, and would hand a bonus to miners who avoid washes by
# being decisively WRONG.
EFFICIENCY_SLOPE = float(os.getenv("SN89_EFFICIENCY_SLOPE", "0.6"))
# Sized so the FLOOR does not bind across the realistic field. At slope 1.0 the
# clamp engaged at ~45% wash — the field median — pinning half the miners to
# EFFICIENCY_MIN where the multiplier can no longer tell them apart. At 0.6 the
# live field spreads 0.70-0.93 and the floor only bites past ~75% wash.
EFFICIENCY_MIN = float(os.getenv("SN89_EFFICIENCY_MIN", "0.55"))
# INVARIANT (enforced in tests) — the efficiency span (1/MIN) must stay NARROWER
# than the tier span (2.0). A wash penalty rewards firing into high volatility, and
# at a small band relative to vol the outcome is microstructure rather than opinion
# (measured: at effective band scale 0.8 the true skill SD is 0.0% and miners become
# indistinguishable). Keeping this range inside the tier's means a miner who trashes
# their hit rate to cut washes always loses more than they gain. Invert it and the
# contest inverts with it.

# Thin samples are shrunk toward the EXPECTED wash rate, not toward zero. Toward
# zero would make a fresh hotkey look perfect and hand it the top of the scale, so
# churning hotkeys would launder a wash record. Pulling to the prior means an
# unproven miner starts exactly where an average one sits and has to earn its way
# down. Mirrors shrunk_hit_rate's Beta prior on the tier side.
EFFICIENCY_PRIOR_WASH = float(os.getenv("SN89_EFFICIENCY_PRIOR_WASH", "0.40"))
# = the board's structural wash target. Recalibrate this whenever that target moves.
EFFICIENCY_PRIOR_K = int(os.getenv("SN89_EFFICIENCY_PRIOR_K", "40"))

IMMUNITY_S = int(os.getenv("SN89_IMMUNITY_S", str(8 * 24 * 3600)))  # from first commit observed for the hotkey
DUST_WEIGHT = 1e-4                  # normalized floor during immunity
# Probation grace: a QUALIFIED miner that currently earns zero emission keeps the
# dust floor for this long instead of dropping straight to nothing. Covers (a) a
# miner whose earning wins have aged out of the decay window (was earning, now 0)
# and (b) a warmup-qualified miner that hasn't landed a post-warmup win yet. The
# clock runs from the close of the miner's earning window (last post-warmup win +
# EMISSION_DECAY_S), or from warmup end if it has no post-warmup win. 0 disables.
PROBATION_S = int(os.getenv("SN89_PROBATION_S", str(30 * 24 * 3600)))
BURN_UID = 0                        # absorbs weight when nobody qualifies
# Miner emission cap (Mantis-sn123-style). The field of real miners collectively
# receives at most this fraction of the total incentive weight; the remainder
# burns to BURN_UID (owner UID0). This is a CEILING, applied by scaling the
# combined miner weight (immune dust + qualified pro-rata) DOWN to the cap — it
# never inflates miner weight upward, so when nobody qualifies leftovers still
# burn exactly as before. 1.0 disables the cap (legacy full pass-through).
# 2026-07-23: doubled the miner incentive pool for the two-mechanism launch.
# 0.30 -> 0.60: real miners now collectively receive up to 60%% of incentive
# (was 30%%), burn 70%% -> 40%%. Applies to BOTH mechanisms (hf.HF_MINER_EMISSION_CAP
# defaults to this), so with a 50/50 emission split miners get 60%% of total.
# Must live in master, not an env override, or external validators would burn
# a different fraction and disagree with the chef in consensus.
MINER_EMISSION_CAP = float(os.getenv("SN89_MINER_EMISSION_CAP", "0.60"))
STRIKE_LIMIT = 3                    # consistency failures in 30d ⇒ zeroed 30d
STRIKE_WINDOW_S = 30 * 24 * 3600

# ── Elimination floor (CONSENSUS) ────────────────────────────────────────────
# Permanently zeroes a hotkey whose record is confidently below the floor (coming
# back = new hotkey, fresh track record). Track-record only — no collateral.
# CONFIDENCE path: terminal when the LIFETIME Wilson upper bound on the true
# hit-rate < ELIM_UB_CEIL (i.e. 90 % sure the true rate is below the ceiling). The
# lifetime bound tightens monotonically toward the truth, so a genuinely-good
# trader is never noise-killed on a cold streak; only a never-real trader trips it.
# Elimination is decoupled from "stop paying" (that is the recency-windowed,
# reversible qualify gate) so it can afford to be slow and statistically conservative.
ELIM_MIN_DECISIVE = 40         # lifetime decisive required before terminal removal
ELIM_Z = 1.2816                # one-sided ~90 % for the lifetime elimination bound
ELIM_UB_CEIL = 0.45            # eliminate when the lifetime UPPER bound on hit-rate < this

# LEGACY rolling-window floor (CONFIDENCE_SCORING=0 path / §8 diff script only).
ELIM_MIN_DECISIVE_LEGACY = 20  # lifetime decisive before the legacy floor can trigger
ELIM_MIN_TRAILING = 10         # trailing decisive sample before the legacy floor triggers
ELIM_FLOOR_HIT = 0.40          # trailing hit-rate elimination floor (absolute)

# ── Copy / collusion detection (§7.5 — replaces the retired 15-min cooldown) ──
# Multiple miners holding the same trade is now allowed; what we penalize is one
# hotkey *repeatedly* shadowing another. Detection is a pairwise coincidence
# counter over a 30-day rolling window — deterministic over the journal, so every
# validator flags the same hotkeys. The LATER of two same-(pair,direction)
# commits is the copier (first commit is always innocent).
# PRIMARY enforcement: a per-signal scoring penalty on the copier. The earliest
# entrant on a trade is the original; any DIFFERENT hotkey that opens the same
# (pair, direction) while the original's position is still live is a copier, and
# its outcome is penalized (a copied WIN does not count as a win). Spraying one
# winning call across N keys therefore self-destructs: the original key keeps the
# win, the other N-1 book penalized wins that drag their hit-rate below the gate.
COPY_PENALTY = os.getenv("SN89_COPY_PENALTY", "loss")
                                    # "loss" — a HABITUAL copier's copied wins count
                                    #          toward decisive but NOT toward wins
                                    #          (tanks hit-rate below the gate);
                                    # "off"  — report-only, no scoring impact (calibration)

# A copied win is only stripped for a *habitual* copier: a hotkey whose share of
# decisive trades that landed second into another hotkey's live trade exceeds
# this rate. This is aggregated across ALL leaders, so a copier who rotates
# victims (multiple UIDs / a hacked feed copying one leader after another) can't
# escape it — and an honest miner who only occasionally lands second on a
# crowded trade is NOT penalized.
COPY_HABITUAL_RATE = float(os.getenv("SN89_COPY_HABITUAL_RATE", "0.5"))
COPY_MIN_COPIES = int(os.getenv("SN89_COPY_MIN_COPIES", "5"))
COPY_PENALTY_TTL_S = int(os.getenv("SN89_COPY_PENALTY_TTL_S", str(2 * 24 * 3600)))
                                    # The flag is a LOUD, SHORT-LIVED WARNING, not a 30-day
                                    # scarlet letter. Detection still weighs COPY_WINDOW_S of
                                    # history (you need history to see a pattern), but the
                                    # PENALTY only bites while the LAST copy event is within
                                    # this TTL. Stop landing second and it clears in 2 days;
                                    # keep doing it and it re-trips every cycle. This bounds the
                                    # blast radius of a false positive to 2 days instead of 30.
                                    # need at least this many copied trades in the
                                    # window before the rate gate can fire (a new
                                    # miner with 2 unlucky second-landings is safe)

# SECONDARY forensic: a 30-day pairwise shadowing report (who repeatedly follows
# whom). Report-only by default — surfaced to the operator, no automatic penalty;
# the per-signal COPY_PENALTY above is what actually bites.
COPY_WINDOW_S = 30 * 24 * 3600      # rolling lookback for the shadowing report
COPY_SHARP_LAG_S = 15 * 60          # same (pair,dir) within 15 min = sharp coincidence
COPY_SOFT_LAG_S = 24 * 3600         # same (pair,dir) within 24 h = soft overlap
COPY_SHARP_MIN_EVENTS = int(os.getenv("SN89_COPY_SHARP_MIN_EVENTS", "8"))
                                    # sharp EPISODES vs one leader ⇒ report as copier.
                                    # 2026-07-16: 3 -> 6. On the curated IQ-Signals cohort,
                                    # independent traders running the SAME price-triggered
                                    # strategy (e.g. "long gold at the intraday low") co-enter
                                    # at the same extreme on every occasion it prints, so 3-5
                                    # episodes is the honest shared-strategy NOISE FLOOR, not
                                    # copying. Across the live journal every leader→follower
                                    # pair sits at ≤5 episodes EXCEPT one at 12 (2.4× the next)
                                    # — a clean gap. 6 flags only that outlier. Verified: gold
                                    # 2026-07-16 day-low 3974.47 @13:05:28; two "copiers" both
                                    # longed within 5 min of that bottom — the PRICE was the
                                    # common cause, not a chat.
                                    #
                                    # 2026-07-30: 6 -> 8. The gap 6 was cut at has closed.
                                    # The rule for this threshold is the GAP IN THE DATA, and
                                    # on 18 days of post-2h-reveal journal the distribution of
                                    # sharp episodes over all directed leader→follower pairs is
                                    #   1×60  2×5  3×4  4×6  5×6  6×1  7×1  12×1
                                    # so 6 and 7 now sit flush against the 4-5 noise floor and
                                    # the only gap left is 7→12. The penalised set is 3,3,2,1,0
                                    # at thresholds 4..8 and flat 0 across 8..13 — the flat
                                    # plateau the 07-16 note demanded starts at 8, not 6.
                                    # 8 still catches the 12-episode ring the moment it
                                    # re-trips, and it clears the two keys sitting in the floor:
                                    # 5EoLdj8t (6 episodes, ALL SIX on moves 2-4 hotkeys held
                                    # within ±30 min, three of them inside bursts already
                                    # adjudicated as shared triggers — 07-14 14:09 XAGUSD queue,
                                    # 07-15 12:19-12:25 PPI, 07-16 gold-ATL) and 5H6Q2A (7).
                                    # NOTE what is NOT claimed: 5EoLdj8t's follow of 5GvLHY is
                                    # one-directional (6 forward, 0 reverse), so rotation does
                                    # not exonerate it. The cut rests on the floor being 4-5,
                                    # not on that key being innocent.
COPY_EPISODE_S = int(os.getenv("SN89_COPY_EPISODE_S", str(30 * 60)))
                                    # Sharp follows of the SAME leader landing inside this
                                    # window are ONE episode, not N. The daily cap (6 at the
                                    # time, 3 since 2026-07-18) means a high-impact print
                                    # (CPI/PPI/NFP) makes a trader fire much of its daily
                                    # allowance within minutes:
                                    # on 2026-07-15 12:19-12:25, 8 hotkeys fired 34 signals
                                    # and every pair skewed one way. Counting each pair
                                    # separately turns ONE reaction into 5-6 "independent"
                                    # coincidences — pseudo-replication, and the reason
                                    # honest news traders trip a 3-event gate. Copying the
                                    # same leader on DIFFERENT occasions is the real signal.
COPY_SOFT_MIN_EVENTS = 8            # soft coincidences vs one leader ⇒ report low-diversity
COPY_REQUIRE_SHADOW = bool(int(os.getenv("SN89_COPY_REQUIRE_SHADOW", "1")))
                                    # Strip a habitual copier's wins ONLY if it ALSO
                                    # carries the pairwise forensic signature below
                                    # (>= COPY_SHARP_MIN_EVENTS sharp follows of ONE
                                    # leader). Landing second inside a leader's 8-12h
                                    # horizon is NOT evidence: on a 13-pair board 41%
                                    # of all decisive calls land second, and 69% of
                                    # marks sit on trades >=4 hotkeys held at once —
                                    # that is a crowded consensus move (news), not a
                                    # copy. Repeatedly shadowing ONE miner inside 15
                                    # min is evidence. Set 0 for the legacy rate-only
                                    # behaviour.
COPY_ZERO_WEIGHT = bool(int(os.getenv("SN89_COPY_ZERO_WEIGHT", "0")))
                                    # set 1 to ALSO zero forensic-flagged copiers' weight
                                    # (off by default — COPY_PENALTY is the real lever)

# ── Copy-leader eligibility (anti-grief) ──────────────────────────────────────
# A commit can only make a LATER entrant a "copier" if its own hotkey is an
# ELIGIBLE LEADER: a real signal source, not a throwaway griefer. Without this,
# anyone could register a hotkey (no qualification or collateral needed), occupy
# a (pair, direction) early — or BOTH directions of a pair — and manufacture
# copy-flags against honest miners who merely traded the same pair. The 24h
# timelock makes live copying impossible, so overlap alone is never proof that
# anyone copied the griefer. A leader must (a) have a real track record and
# (b) not be a both-direction spammer.
COPY_LEADER_MIN_DECISIVE = int(os.getenv(
    "SN89_COPY_LEADER_MIN_DECISIVE", "10"))   # pinned at the legacy QUALIFY_MIN_DECISIVE
                                              # so the confidence-gate retune (10→8) does
                                              # NOT shift copy-leader eligibility (out of scope)
                                    # a leader needs ≥ this many decisive outcomes
                                    # before its position can copy-flag others
COPY_EXCLUDE_BOTH_DIR = bool(int(os.getenv("SN89_COPY_EXCLUDE_BOTH_DIR", "1")))
                                    # a hotkey holding LONG+SHORT on one pair at
                                    # once is never an eligible leader (griefer tell)

# ── Referral / recruiter incentive (CONSENSUS) ────────────────────────────────
# An existing miner (the RECRUITER) vouches for a new hotkey (the RECRUIT) by
# committing `sn89ref:1:<recruit_ss58>` on-chain BEFORE the recruit registers.
# The commit's inclusion block is the proof time: the referral is valid only if
# the recruit's registration block is at least REFERRAL_MIN_LEAD_BLOCKS later
# (defeats mempool front-running of a pending registration). While BOTH sides
# are actively earning (decayed qualified-win tally > 0), the recruit's tally is
# boosted by REFERRAL_RECRUIT_BONUS (10%) and the recruiter's by
# REFERRAL_RECRUITER_BONUS (20%) × each recruit's tally (capped in total at
# REFERRAL_MAX_X × the recruiter's own tally). If either side stops earning,
# both bonuses lapse that cycle and resume on re-earn. Bonuses redistribute
# WITHIN the miner pool (pre-MINER_EMISSION_CAP) — nothing new is minted.
# STRICT pair no-copy: shadowing inside the pair suspends the pair's bonus for
# REFERRAL_PAIR_TTL_S after the last copy event (base emission is untouched —
# the global §7.5 machinery is separate and unchanged).
# Env overrides are for testnet A/B only — the consensus default lives in git.
REFERRAL_ENABLED = bool(int(os.getenv("SN89_REFERRAL_ENABLED", "1")))  # LIVE (enabled 2026-07-20)
REFERRAL_RECRUIT_BONUS = float(os.getenv("SN89_REFERRAL_RECRUIT_BONUS", "0.10"))
REFERRAL_RECRUITER_BONUS = float(os.getenv("SN89_REFERRAL_RECRUITER_BONUS", "0.20"))
REFERRAL_MAX_X = float(os.getenv("SN89_REFERRAL_MAX_X", "1.0"))
                                    # recruiter's TOTAL referral bonus ≤ this × own tally
                                    # (referrals can at most double a recruiter's emission)
REFERRAL_MIN_LEAD_BLOCKS = int(os.getenv("SN89_REFERRAL_MIN_LEAD_BLOCKS", "10"))
                                    # ≈2 min at 12s blocks — far beyond mempool residence
REFERRAL_MAX_RECRUITS = int(os.getenv("SN89_REFERRAL_MAX_RECRUITS", "0"))
                                    # per recruiter; 0 = UNLIMITED (no breadth
                                    # cap). >0 keeps only the earliest N recruits
                                    # by commit_block. Total bonus is still bounded
                                    # by REFERRAL_MAX_X × own tally regardless.
# Pair no-copy gate — deliberately STRICTER than the global copy detector
# (the pair opted into scrutiny by pairing up; a false positive only pauses a
# bonus, never touches base emission). Two honest same-strategy traders who
# co-fire on news should simply not refer each other.
REFERRAL_PAIR_SHARP_EPISODES = int(os.getenv("SN89_REFERRAL_PAIR_SHARP_EPISODES", "2"))
                                    # sharp (≤COPY_SHARP_LAG_S) follow episodes either
                                    # direction within the pair ⇒ suspend
REFERRAL_PAIR_OVERLAP_EPISODES = int(os.getenv("SN89_REFERRAL_PAIR_OVERLAP_EPISODES", "3"))
                                    # live-overlap (mark_copies) episodes either
                                    # direction within the pair ⇒ suspend
REFERRAL_PAIR_WINDOW_S = int(os.getenv("SN89_REFERRAL_PAIR_WINDOW_S", str(30 * 24 * 3600)))
                                    # trailing window over which pair episodes count
REFERRAL_PAIR_TTL_S = int(os.getenv("SN89_REFERRAL_PAIR_TTL_S", str(30 * 24 * 3600)))
                                    # bonus stays suspended until this long after the
                                    # LAST pair-copy event, then self-clears

# ── Asset universe / bands (CONSENSUS — vendored from the live IQ Signals board)
_BANDS_PATH = Path(os.getenv(
    "SN89_BANDS_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "signals-bands.json"),
))


def load_bands() -> dict:
    """Returns {"version":…, "bands": {ASSET: {tp_bps, sl_bps, asset_class}}}."""
    with open(_BANDS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def allowed_assets() -> dict:
    return load_bands().get("bands", {})


# ── Band versioning (CONSENSUS) ───────────────────────────────────────────────
# A signal is graded against the band that was in force at its COMMIT block, not
# whatever the board says at reveal — so a later board update never retroactively
# voids or re-grades an in-flight signal (the README promise). The live board is
# signals-bands.json (single version, hot-read, what the miner reads). An OPTIONAL
# history file lets a validator resolve the board as-of any past commit time; when
# it is absent the current board is treated as effective for all time, i.e. exactly
# today's single-version behaviour.
_BANDS_HISTORY_PATH = Path(os.getenv(
    "SN89_BANDS_HISTORY_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "signals-bands-history.json"),
))
_bands_history_cache: "tuple[float, list] | None" = None


def load_bands_history() -> list:
    """[{version, effective_from_unix, bands}, …] ascending by effective_from_unix.
    Falls back to a single entry (the current board, effective from epoch) when no
    history file exists, so an operator who never records history keeps exactly
    today's behaviour. Cached on the file mtime (this is read per graded signal)."""
    global _bands_history_cache
    if _BANDS_HISTORY_PATH.exists():
        mtime = _BANDS_HISTORY_PATH.stat().st_mtime
        if _bands_history_cache is None or _bands_history_cache[0] != mtime:
            with open(_BANDS_HISTORY_PATH, "r", encoding="utf-8") as fh:
                hist = json.load(fh)
            hist = sorted(hist, key=lambda e: int(e.get("effective_from_unix", 0)))
            _bands_history_cache = (mtime, hist)
        return _bands_history_cache[1]
    cur = load_bands()
    return [{"version": cur.get("version", "current"),
             "effective_from_unix": 0, "bands": cur.get("bands", {})}]


def bands_as_of(t0_unix: float) -> dict:
    """The board {ASSET: {tp_bps, sl_bps, asset_class}} in force at commit time."""
    hist = load_bands_history()
    chosen = hist[0]
    for entry in hist:
        if int(entry.get("effective_from_unix", 0)) <= t0_unix:
            chosen = entry
        else:
            break
    return chosen.get("bands", {})


def allowed_assets_as_of(t0_unix: float) -> dict:
    return bands_as_of(t0_unix)


def asset_class_for(trade_pair: str, t0_unix: "float | None" = None,
                    bands: "dict | None" = None) -> str:
    """Canonical asset class for a pair, taken from the BOARD — never from the
    miner's committed `asset_class`, which the miner can forge to bend feed
    routing, the wash window, and the wick-sanitisation tolerance. Grading derives
    the class from the pair; when t0_unix is given it resolves the commit-time band
    so a later board update can't re-class an in-flight signal."""
    b = bands if bands is not None else (
        allowed_assets_as_of(t0_unix) if t0_unix is not None else allowed_assets())
    return (b.get(trade_pair) or {}).get("asset_class", "")


def horizon_h_for(trade_pair: str, t0_unix: "float | None" = None) -> int:
    """Grade/wash window for a pair, derived from the board class (not the miner)."""
    return class_horizon_h(asset_class_for(trade_pair, t0_unix))


# ── Massive / Polygon (validators + owner only; miners don't need a key) ─────
# Requires a PAID Massive (formerly Polygon.io) subscription with intraday
# (1-second + 1-minute) aggregates on the Currencies (forex + metals) and
# Crypto feeds — see README "Running a validator".
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")


def polygon_ticker(asset: str, asset_class: str) -> str:
    if asset_class == "crypto":
        return f"X:{asset}"
    if asset_class == "equities":
        return str(asset).replace("_", ".")
    return f"C:{asset}"  # forex + forex-commodities (metals)


# ── Blob transport ───────────────────────────────────────────────────────────
# Miners host blobs at any HTTPS URL they control; the layout is always
#   {base}/{hotkey}/{nonce}.json
# and the on-chain commitment carries the blob hash, so the transport is
# untrusted. Three ways to serve, in order of how little setup they need:
#
#   1. Owner-hosted relay (zero setup) — push the encrypted blob through the IQ
#      relay with your /miner feed token; it serves it at the default base
#      below. Keys never leave your box; the relay can't read or forge a signal.
#   2. Your own S3/R2 bucket — set the SN89_R2_* creds below.
#   3. Local disk + any static server — set SN89_BLOB_DIR (testnet soaks).
#
# R2_PUBLIC_BASE defaults to the relay so validators discover relay-hosted
# miners (the common case) with no config; self-hosters override it.
R2_PUBLIC_BASE = os.getenv("SN89_R2_PUBLIC_BASE",
                           "https://partner.infinitequant.app/sn89/blob")
R2_ENDPOINT = os.getenv("SN89_R2_ENDPOINT", "")        # s3 api endpoint (miner upload)
R2_BUCKET = os.getenv("SN89_R2_BUCKET", "")
R2_ACCESS_KEY_ID = os.getenv("SN89_R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("SN89_R2_SECRET_ACCESS_KEY", "")

# Owner-hosted relay (option 1). Active when a token is present; the feed token
# from /miner doubles as the relay token, so `follow` mode needs no bucket.
RELAY_URL = os.getenv("SN89_RELAY_URL",
                      "https://partner.infinitequant.app/api/sn89/blob")
RELAY_TOKEN = os.getenv("SN89_RELAY_TOKEN", "") or os.getenv("SN89_FEED_TOKEN", "")

# ── Validator runtime ────────────────────────────────────────────────────────
POLL_INTERVAL_S = 30

# After a FAILED set_weights, wait at least this many blocks before retrying
# instead of re-attempting every poll. Weight commits are rate-limited
# (commit-reveal / weights_rate_limit); retrying faster than that just piles up
# unrevealed commits (TooManyUnrevealedCommits) without landing any sooner. Set
# to the subnet's weights_rate_limit. (A successful commit is still throttled to
# once per TEMPO by the normal cadence.)
WEIGHTS_RETRY_BLOCKS = int(os.getenv("SN89_WEIGHTS_RETRY_BLOCKS", "100"))

# Set mechanism-1 (HF) weights from the validator, every tempo, right after mecid 0.
# ON by default so validators set BOTH mechanisms out of the box on update. The HF
# weights are graded from the PUBLIC window logs (hf_grade), so every validator
# reproduces the same vector — same trust model as mecid 0. Any failure here NEVER
# touches the mecid-0 commit (which already landed above). Flip to 0 to disable.
HF_MECID1_WEIGHTS = os.getenv("SN89_HF_MECID1_WEIGHTS", "1") == "1"

# ── Unified multi-competition weighting (competitions.py) ────────────────────
# The chain caps a subnet at 2 mechanisms, so a third competition (Closers)
# cannot take a mechanism slot. When COMBINED_WEIGHTS is on, the validator
# computes each competition's OWN normalized vector (LF, HF, Closers, any
# future fourth), blends them by COMP_WEIGHTS (normalize-then-weight — the one
# choice that keeps specialists' payouts identical to dedicated mechanisms),
# and commits the blend as the SINGLE mecid-0 vector; mecid-1 is then ramped to
# 0% via MechanismEmissionSplit and eventually retired. OFF by default: mainnet
# behavior is byte-identical until this flips, which is a coordinated,
# announced migration (all 8 permitted validators must flip together — two
# meanings of the same mecid in one tempo is a consensus split).
# After the merge the split is CODE-enforced, not chain-enforced: COMP_WEIGHTS
# is a consensus constant. Commit changes to master and announce them like a
# band change — the journal is the only public record of the split.
# The env flag is the TESTNET switch. The MAINNET flip is the timestamp below —
# a committed consensus constant, so every validator that has pulled master
# flips in the same tempo with no env coordination (an env-coordinated flip
# would split consensus on whichever validator restarts with a stale unit
# file). 0 = never. To launch: commit the cutover timestamp + announce, then
# ramp MechanismEmissionSplit only after the flip is observed on all signers.
COMBINED_WEIGHTS_FROM_UNIX = int(os.getenv("SN89_COMBINED_WEIGHTS_FROM", "0"))

# ── Referrer mechanism on mecid-1 (§ referrer) ───────────────────────────────
# When armed (and the combined path is active), the freed mecid-1 slot pays a
# REFERRER CLASS instead of parking all-burn: score = Σ recruits' decayed
# qualified-win tallies (after one-time sn89refx base transfers), pool split
# pro-rata, field capped like every competition. Referrers need a UID
# (registration = sybil cost) but never need to trade. Arming this also
# RETIRES the in-band 20% recruiter bonus inside mecid-0 (the recruit's own
# 10% entry bonus stays — it is a trader incentive, not a referrer one);
# MechanismEmissionSplit then sets the combined/referrer emission ratio.
REFERRER_MECID1 = os.getenv("SN89_REFERRER_MECID1", "0") == "1"
COMBINED_WEIGHTS = os.getenv("SN89_COMBINED_WEIGHTS", "0") == "1"


def combined_weights_active(now_unix: float) -> bool:
    """Whether the unified path is live at `now` — env force (testnet) OR the
    committed mainnet cutover has passed."""
    return COMBINED_WEIGHTS or (
        COMBINED_WEIGHTS_FROM_UNIX > 0 and now_unix >= COMBINED_WEIGHTS_FROM_UNIX)


def _parse_comp_weights(spec: str) -> dict:
    # inline (not competitions.parse_shares) to keep config import-order free
    out = {}
    for part in spec.split(","):
        if part.strip():
            k, _, v = part.partition(":")
            out[k.strip()] = float(v)
    t = sum(out.values())
    return {k: v / t for k, v in out.items()} if t > 0 else {}


# Shares are AS-OF versioned like the bands: an auditor replaying an old epoch
# must resolve the shares in force AT that epoch, not today's. Append a new
# (effective_from_unix, spec) entry for every ramp step — never edit history.
# The env override replaces the WHOLE history (testnet convenience only).
COMP_WEIGHTS_HISTORY: tuple = (
    # (0, launch-neutral): the merge ships payout-neutral — closers at 0 —
    # so the neutral-merge soak can reconcile LF/HF payouts against the
    # dedicated-mechanism baseline before any share moves.
    (0, "lf:0.5,hf:0.5,closers:0.0"),
)
_COMP_ENV = os.getenv("SN89_COMP_WEIGHTS", "")
if _COMP_ENV:
    COMP_WEIGHTS_HISTORY = ((0, _COMP_ENV),)


def comp_weights_as_of(t_unix: float) -> dict:
    spec = COMP_WEIGHTS_HISTORY[0][1]
    for eff, s in COMP_WEIGHTS_HISTORY:
        if t_unix >= eff:
            spec = s
    return _parse_comp_weights(spec)


COMP_WEIGHTS = comp_weights_as_of(2**62)   # current-era shares (latest entry)

# Blob fetching is bounded so a slow or malicious host can't stall the
# synchronous validator loop (audit #7). Each fetch has a hard wall-clock
# deadline (bucket.fetch); per loop iteration the outstanding fetches run in a
# small thread pool under a total budget, and whatever doesn't finish is simply
# retried next loop (the on-chain commitment is durable, and a captured blob is
# pinned). Validators fetch ONLY from R2_PUBLIC_BASE (the owner relay by
# default, or an operator-set bucket) — that base is the trust boundary; the
# blob is encrypted and integrity-checked against the on-chain hash regardless.
BLOB_FETCH_DEADLINE_S = float(os.getenv("SN89_BLOB_FETCH_DEADLINE_S", "8"))
BLOB_FETCH_WORKERS = int(os.getenv("SN89_BLOB_FETCH_WORKERS", "8"))
BLOB_FETCH_BUDGET_S = float(os.getenv("SN89_BLOB_FETCH_BUDGET_S", "20"))

# Commitment-history scan (audit #5). When on, ingest also scans the blocks
# since the last poll so a commitment a miner OVERWROTE before any validator
# observed it isn't lost (the cross-validator split-brain). OFF by default: the
# exact on-chain event/extrinsic shape for set_commitment varies by runtime and
# MUST be validated on testnet before enabling on mainnet. SCAN_MAX_BLOCKS bounds
# the per-poll scan so a long downtime doesn't trigger an unbounded backfill.
SCAN_COMMITMENT_HISTORY = bool(int(os.getenv("SN89_SCAN_COMMITMENT_HISTORY", "0")))
SCAN_MAX_BLOCKS_PER_POLL = int(os.getenv("SN89_SCAN_MAX_BLOCKS_PER_POLL", "120"))
# (Genesis backfill for late-joining validators was removed — see the single-
# validator model: docs/single-validator-model.md. One authoritative validator +
# child-key delegation makes multi-validator catch-up unnecessary.)

DB_PATH = os.getenv("SN89_DB_PATH", os.path.expanduser("~/.sn89/validator.db"))
