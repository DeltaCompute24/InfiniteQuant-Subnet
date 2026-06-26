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
TEMPO = 360

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
REVEAL_DELAY_S = 24 * 3600          # tlock round = commit time + 24h
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
# No spacing gate and no self-overlap limit: a hotkey may fire up to
# MAX_SIGNALS_PER_UTC_DAY signals per UTC day with no minimum gap between them
# and may hold multiple open calls on the same (pair, direction).
MAX_SIGNALS_PER_UTC_DAY = 6
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


LATENCY_BUFFER_S = 30               # entry anchor = commit-block timestamp + buffer
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

# ── Median-fill confirmation (CONSENSUS) ─────────────────────────────────────
# A practically tradeable fill requires price to PERSIST through a level, not
# just tick it once. A single trade into a 1-minute candle's high/low is not a
# fill anyone could capture, so we confirm any candle touch against the MEDIAN
# CLOSE of 1-second bars over a rolling window (30s, preferring the recent 15s
# when it holds enough samples) and trigger only on a strict crossing. A
# momentary wick can never move the median, so it doesn't score. Polygon 1s
# aggs are immutable, so every validator replays the confirmation identically.
LIMIT_FILL_MEDIAN_CONFIRM = True    # gate touch grades through the median window
LIMIT_FILL_WINDOW_S = 30            # full rolling window for the median
LIMIT_FILL_RECENT_S = 15            # preferred recent sub-window (window / 2)
LIMIT_FILL_MIN_SAMPLES = 10         # use the recent window when it holds >this many
                                    # 1s bars, else fall back to the full window

# ── Scoring (CONSENSUS — §7 of SPEC) ─────────────────────────────────────────
SCORE_WINDOW_S = 30 * 24 * 3600     # EMISSION window: a miner's share is sized by
                                    # its WON count in the trailing 30 days, so you
                                    # must keep trading to earn. (Distinct from the
                                    # hit-rate window below.)

# Hit-rate REPUTATION window. The qualify gate and the per-win tier are computed
# over a ROLLING window — the trailing HIT_RATE_WINDOW_S, capped at the most
# recent HIT_RATE_WINDOW_TRADES decisive outcomes (whichever is tighter) — NOT
# over lifetime. Reputation decays: a once-great miner who starts trading badly
# loses its tier and falls below the gate as the good history ages out, and old
# wins can't prop up poor recent trading. A large, stable sample (≈2 months /
# ≈100 trades) so the tier doesn't flip on a short hot/cold streak.
HIT_RATE_WINDOW_S = 60 * 24 * 3600          # ≈2 months of history
HIT_RATE_WINDOW_TRADES = 100                # …capped at the most recent ~100 decisive

QUALIFY_MIN_DECISIVE = 10           # decisive (won+lost) IN THE WINDOW to qualify
QUALIFY_MIN_HIT = 0.55              # reputation-window hit-rate gate (QUALIFIED floor)

# Per-win multiplier by reputation-window hit-rate tier — each recent win is worth
# more the higher your windowed hit-rate, so quality is rewarded above the gate,
# not just volume. Checked high → low; the first threshold met wins. Below
# QUALIFY_MIN_HIT a miner isn't qualified (multiplier 0). Mirrors the IQ Signals
# program tiers: QUALIFIED 55 % (1×) · SHARP 60 % (1.2×) · WOLF 70 % (2×).
WIN_RATE_TIERS = (
    (0.70, 2.0),   # WOLF
    (0.60, 1.2),   # SHARP
    (0.55, 1.0),   # QUALIFIED
)
IMMUNITY_S = 8 * 24 * 3600          # from first commit observed for the hotkey
DUST_WEIGHT = 1e-4                  # normalized floor during immunity
BURN_UID = 0                        # absorbs weight when nobody qualifies
STRIKE_LIMIT = 3                    # consistency failures in 30d ⇒ zeroed 30d
STRIKE_WINDOW_S = 30 * 24 * 3600

# ── Elimination floor (CONSENSUS) ────────────────────────────────────────────
# A sustained sub-floor hit rate zeroes the hotkey permanently (coming back =
# new hotkey, fresh track record). Track-record only — there is no collateral.
ELIM_MIN_DECISIVE = 20         # lifetime decisive before the floor can trigger
ELIM_MIN_TRAILING = 10         # trailing decisive sample before it can trigger
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
                                    # need at least this many copied trades in the
                                    # window before the rate gate can fire (a new
                                    # miner with 2 unlucky second-landings is safe)

# SECONDARY forensic: a 30-day pairwise shadowing report (who repeatedly follows
# whom). Report-only by default — surfaced to the operator, no automatic penalty;
# the per-signal COPY_PENALTY above is what actually bites.
COPY_WINDOW_S = 30 * 24 * 3600      # rolling lookback for the shadowing report
COPY_SHARP_LAG_S = 15 * 60          # same (pair,dir) within 15 min = sharp coincidence
COPY_SOFT_LAG_S = 24 * 3600         # same (pair,dir) within 24 h = soft overlap
COPY_SHARP_MIN_EVENTS = 3           # sharp coincidences vs one leader ⇒ report as copier
COPY_SOFT_MIN_EVENTS = 8            # soft coincidences vs one leader ⇒ report low-diversity
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
    "SN89_COPY_LEADER_MIN_DECISIVE", str(QUALIFY_MIN_DECISIVE)))
                                    # a leader needs ≥ this many decisive outcomes
                                    # before its position can copy-flag others
COPY_EXCLUDE_BOTH_DIR = bool(int(os.getenv("SN89_COPY_EXCLUDE_BOTH_DIR", "1")))
                                    # a hotkey holding LONG+SHORT on one pair at
                                    # once is never an eligible leader (griefer tell)

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

DB_PATH = os.getenv("SN89_DB_PATH", os.path.expanduser("~/.sn89/validator.db"))
