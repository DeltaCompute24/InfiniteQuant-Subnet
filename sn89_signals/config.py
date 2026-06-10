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

# Subnet owner X25519 public key (hex, 32 bytes). Miners encrypt W_owner to this.
# The corresponding private key is held by the subnet owner.
OWNER_PK_HEX = os.getenv(
    "SN89_OWNER_PK_HEX",
    "0000000000000000000000000000000000000000000000000000000000000000",  # set at deploy
)

# ── Submission rules (CONSENSUS — §6.4 of SPEC) ──────────────────────────────
MAX_SIGNALS_PER_UTC_DAY = 3
MIN_SPACING_S = 4 * 3600            # per-hotkey spacing
PLAGIARISM_COOLDOWN_S = 15 * 60     # any (pair, direction) across ALL miners
MAX_HORIZON_H = 72
DEFAULT_HORIZON_H = 72
LATENCY_BUFFER_S = 30               # entry anchor = commit-block timestamp + buffer
ENTRY_SECOND_SCAN_S = 120           # scan window for the 1-second entry bar; if no
                                    # 1s bar lands in it (sparse FX/metals off-hours)
                                    # fall back to the first 1-minute bar open

# ── Grading (CONSENSUS) ──────────────────────────────────────────────────────
WICK_TOL = 0.01                     # candle extreme >1% beyond body + both
                                    # neighbours = uncorroborated spike; clamped
                                    # unless a second feed (Hyperliquid, crypto)
                                    # traded the level the same minute

# ── Scoring (CONSENSUS — §7 of SPEC) ─────────────────────────────────────────
SCORE_WINDOW_S = 8 * 24 * 3600      # trailing 8 days
QUALIFY_MIN_DECISIVE = 20           # lifetime decisive (won+lost) to qualify
QUALIFY_MIN_HIT = 0.52              # trailing hit-rate gate
IMMUNITY_S = 8 * 24 * 3600          # from first commit observed for the hotkey
DUST_WEIGHT = 1e-4                  # normalized floor during immunity
BURN_UID = 0                        # absorbs weight when nobody qualifies
STRIKE_LIMIT = 3                    # consistency failures in 30d ⇒ zeroed 30d
STRIKE_WINDOW_S = 30 * 24 * 3600

# ── Collateral (CONSENSUS — docs/collateral.md) ──────────────────────────────
# Callers post SN89 alpha as collateral (vault custody + public EVM ledger).
# Unfunded hotkeys earn dust only; crossing the elimination floor burns the
# collateral and zeroes the hotkey for good (coming back = new hotkey + fresh
# collateral). Gating is OFF until a ledger contract address is configured.
COLLATERAL_CONTRACT = os.getenv("SN89_COLLATERAL_CONTRACT", "")  # ledger proxy
EVM_ENDPOINT = os.getenv("SN89_EVM_ENDPOINT",
                         "https://lite.chain.opentensor.ai")
VAULT_COLDKEY = os.getenv("SN89_VAULT_COLDKEY", "")    # vault wallet coldkey ss58
COLLATERAL_MIN_ALPHA = float(os.getenv("SN89_COLLATERAL_MIN_ALPHA", "100"))

ELIM_MIN_DECISIVE = 20         # lifetime decisive before the floor can trigger
ELIM_MIN_TRAILING = 10         # trailing decisive sample before it can trigger
ELIM_FLOOR_HIT = 0.40          # trailing hit-rate elimination floor (absolute)
ELIM_SLASH_PROPORTION = 1.0    # fraction of posted collateral burned
WITHDRAW_COOLDOWN_S = MAX_HORIZON_H * 3600   # after last signal settles

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
DB_PATH = os.getenv("SN89_DB_PATH", os.path.expanduser("~/.sn89/validator.db"))
