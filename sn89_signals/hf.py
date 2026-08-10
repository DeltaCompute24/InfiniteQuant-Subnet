"""SN89 high-frequency category — sub-mechanism 1 (CONSENSUS).

STAGED, NOT LIVE. Nothing in the running validator, miner or harvester imports this
module yet, and `MechanismEmissionSplit[89]` remains [65535, 0] (mech 1 earns zero).
Wiring it up is a deliberate, separate step — see docs and the phasing table.

The HF category differs from mechanism 0 in four ways and only four:

  1. TRANSPORT — a submission is bound by a countersigned RECEIPT issued in-band by
     the owner ingest, not by an on-chain commitment. Receipts are Merkle-anchored to
     chain once per window, so the journal stays replayable (§ receipts/anchors below).
     This is what buys sub-second pickup: an on-chain commit cannot beat the 12 s block
     time, and T0 on mech 0 is the commit block's own timestamp.
  2. BANDS + HORIZON — sized from measured excursion so a correct short call resolves
     instead of washing on the clock (HF_BANDS_HISTORY).
  3. LIMITS — 30/day, 250 ms min gap, a rolling 24 h cross-mechanism pair lock, and
     ONE OPEN POSITION PER PAIR (HF_OPEN_GATE_FROM): no second call on a pair while
     the previous one is still running. Open ends at the resolving touch, not at the
     horizon, so re-entry is immediate once the call is decided.
  4. SCORING SCOPE — HF outcomes tally into their own MinerState and their own weight
     vector (mecid 1). Mechanism 0's scoring is untouched.

Qualification is deliberately IDENTICAL to mech 0 (8 decisive, Wilson LB >= 0.50): a
volume gate on a volume category selects for stamina, not edge.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time

from . import config

# ── mechanism ────────────────────────────────────────────────────────────────
MECID = 1                                  # SubtensorModule mechanism id for HF
MECH_LF = 0

# ── bands + horizon (CONSENSUS) ──────────────────────────────────────────────
# Sized from 31 days of sub-minute marks: for a two-sided band B, a trade resolves
# inside horizon H iff max |excursion| over H >= B, so the band that resolves a
# fraction p of the time IS the (1-p) quantile of that excursion. These are the
# 80 %-resolve bands (20 % wash) — the WIDEST band that still resolves in time.
# Wider is better (further above the spread floor) right up until it stops resolving,
# which is exactly the failure the LF board has at HF horizons: XAUUSD LF is 62 bps
# = 254 pips, unreachable in half an hour.
#
# A pair only earns a slot if its band clears ~8x the typical spread — below that the
# outcome is microstructure, not opinion. That test is what excludes USDCAD (6.9x),
# USDCHF (6.0x), AUDUSD (7.2x), NZDUSD (4.7x) and XAGUSD (7.0x) from v1.
#
#   pair: (tp_bps, sl_bps, horizon_s, asset_class)
HF_BOARD_V1 = {
    # metals + crypto @ 30 min
    "XAUUSD": (12.0, 12.0,  1800, "forex-commodities"),   # 47 pips  18.9x spread
    "BTCUSD": (19.0, 19.0,  1800, "crypto"),              #          20.5x
    "ETHUSD": (24.0, 24.0,  1800, "crypto"),              #          13.2x
    "SOLUSD": (24.0, 24.0,  1800, "crypto"),              #           9.2x
    "XRPUSD": (24.0, 24.0,  1800, "crypto"),              #          18.2x
    # FX majors @ 120 min — FX cannot clear the spread floor on a 30 min clock
    "EURUSD": ( 5.0,  5.0,  7200, "forex"),               #  6 pips   9.9x
    "GBPUSD": ( 6.0,  6.0,  7200, "forex"),               #  8 pips   9.0x
    "USDJPY": ( 4.0,  4.0,  7200, "forex"),               #  6 pips   8.0x
}

# Typical spread per pair in BPS, measured 2026-07-22. The board is built against
# these: a pair only earns a slot if its band clears MIN_BAND_SPREAD_RATIO x spread,
# because below that the outcome is microstructure rather than opinion. Kept in the
# module (not a script) so the invariant is testable and a ninth pair cannot be added
# without meeting it.
HF_TYPICAL_SPREAD_BPS = {
    "XAUUSD": 0.61, "BTCUSD": 0.91, "ETHUSD": 1.81, "SOLUSD": 2.56,
    "XRPUSD": 1.32, "EURUSD": 0.53, "GBPUSD": 0.67, "USDJPY": 0.49,
}
MIN_BAND_SPREAD_RATIO = 8.0


def band_spread_ratio(pair: str, t0_unix: float = 0.0) -> float | None:
    """Band / typical spread. None when the spread is unmeasured — which must block
    a listing rather than pass it silently."""
    row = (hf_bands_as_of(t0_unix) or HF_BOARD_V1).get(str(pair).upper())
    sp = HF_TYPICAL_SPREAD_BPS.get(str(pair).upper())
    if not row or not sp:
        return None
    return row[0] / sp


# As-of versioned exactly like the LF board, so a band change never re-grades a past
# call and any validator can resolve the board in force at any t0.
#
# The first entry's effective_from is the LAUNCH time, not 0, and hf_bands_as_of
# returns None before it. A default-to-v1 fallback would have made a pre-launch
# timestamp resolve to a board that did not exist at that time — the same class of
# error as grading against a board row the miner was never subject to.
# The board exists from this instant, not before. This is a VALIDITY gate, not an
# earning gate -- whether HF pays anything is governed solely by
# MechanismEmissionSplit on chain, which is a separate lever. Keeping the two apart
# means HF can accept and grade calls while still paying zero, which is exactly the
# preview posture.
HF_LAUNCH_FROM = int(os.getenv("SN89_HF_LAUNCH_FROM", "1784764800"))   # 2026-07-23T00:00:00Z

# v2 — 2026-08-08T00:00:00Z. Scheduled 45d structural-wash recalibration
# (scripts/recalibrate_bands.py, HF target 14%, 7pp deadband). XAUUSD was the only
# pair outside the band: 22.1% structural wash at 12.0 bps on the 2026-06-22 ->
# 2026-08-06 window, and 21.7% on the 2026-08-01 run, so this is a volatility regime
# and not one noisy window. 10.0 bps lands at 14.17%. The solver returned 9.9
# (13.84%); 10.0 is closer to target and is a clean number. Spread ratio 16.4x, well
# clear of MIN_BAND_SPREAD_RATIO. Every other pair held: BTC 11.9, ETH 15.7, SOL 19.3,
# XRP 17.9, EUR 11.3, GBP 16.2, JPY 14.7. SOL and XRP are drifting toward the top of
# the deadband on a shared 24.0 band and are the likely next split.
HF_BOARD_V2 = dict(HF_BOARD_V1, XAUUSD=(10.0, 10.0, 1800, "forex-commodities"))
HF_V2_FROM = int(os.getenv("SN89_HF_V2_FROM", "1786147200"))          # 2026-08-08T00:00:00Z

HF_BANDS_HISTORY = (
    (HF_LAUNCH_FROM, HF_BOARD_V1),
    (HF_V2_FROM, HF_BOARD_V2),
)


def hf_bands_as_of(t0_unix: float) -> dict | None:
    """The HF board in force at t0, or None if HF was not yet live.

    None is load-bearing: validate_submission then rejects every pair, which is the
    correct answer before launch. Returning a board would silently grade calls that
    could not have existed.
    """
    board = None
    for eff, b in HF_BANDS_HISTORY:
        if t0_unix >= eff:
            board = b
    return board


def hf_horizon_s(pair: str, t0_unix: float) -> int | None:
    board = hf_bands_as_of(t0_unix)
    row = (board or {}).get(pair.upper())
    return None if row is None else int(row[2])


# ── submission limits (CONSENSUS) ────────────────────────────────────────────
# (effective_from_unix, max_per_utc_day, min_gap_ms, max_open_per_pair)
#
# max_open_per_pair was DECLARED from launch and NEVER ENFORCED: `check_rate`
# discarded the field, `is_pair_locked` only ever looked at the OTHER mechanism, and
# `tools/publish_mech_state.py` published "max_open_per_pair": 4 to the board — a
# limit we advertised and did not apply. On 2026-07-27 a trader copy/pasted five
# SHORT BTCUSD calls a second apart and got five receipts: nothing in the path
# objected, because the only spacing gate is the 250 ms min gap.
#
# From HF_OPEN_GATE_FROM the limit is 1 and it is real (`check_pair_open`): a hotkey
# may not submit on a pair while its own prior call on that pair is still OPEN.
# "Open" is not a clock — it ends at the FIRST decisive touch or at the horizon wash,
# whichever comes first (`open_until_ms`), so re-entry is allowed the moment the
# previous call resolves. On a ±19 bps / 30 min band most calls resolve well inside
# the horizon, which is why this is not simply "one call per pair per horizon".
HF_OPEN_GATE_FROM = 1785189600          # 2026-07-27T22:00:00Z

HF_RULES_HISTORY = (
    (0, 30, 250, 4),
    (HF_OPEN_GATE_FROM, 30, 250, 1),
)


def hf_rules_as_of(t0_unix: float) -> tuple[int, int, int]:
    cap, gap, openmax = HF_RULES_HISTORY[0][1:]
    for eff, c, g, o in HF_RULES_HISTORY:
        if t0_unix >= eff:
            cap, gap, openmax = c, g, o
    return cap, gap, openmax


# ── cross-mechanism pair lock (CONSENSUS) ────────────────────────────────────
# A pair a HOTKEY submits on one mechanism is locked on the other for a rolling 24 h.
# Rolling, not calendar: a day boundary would create a nightly rush and let a trader
# submit LF at 23:59 then HF at 00:01.
#
# 24 h rather than a week (Whit, 2026-07-23). The rule only has to stop the SAME VIEW
# being paid twice, and an HF call resolves in 30-120 min — so once a day has passed
# the LF call and any HF call on that pair are answering different questions, not the
# same one twice. A week also made the lock expensive during preview: a tester would
# surrender a pair on the EARNING book for seven days to try a book paying nothing.
#
# KEYED TO HOTKEY ONLY (Whit, 2026-07-22). Known and accepted consequence: a trader
# running two hotkeys can hold the same pair on both mechanisms. Registration cost is
# the only friction. Do not "fix" this by keying to coldkey or signals-user without
# an explicit decision — it changes the rule for self-hosted miners, who have no
# signals-user identity at all.
PAIR_LOCK_S = int(os.getenv("SN89_PAIR_LOCK_S", str(24 * 3600)))   # 24h (Whit, 2026-07-23)
PAIR_LOCK_MS = PAIR_LOCK_S * 1000


def pair_lock_until_ms(last_submit_ms: int) -> int:
    return int(last_submit_ms) + PAIR_LOCK_MS


def is_pair_locked(index: dict, hotkey: str, pair: str, mecid: int, t_ms: int) -> bool:
    """True if `hotkey` used `pair` on the OTHER mechanism inside the rolling window.

    `index` maps (hotkey, PAIR, mecid) -> last submit ms, built from BOTH the on-chain
    commit stream (mech 0) and the receipt log (mech 1). Symmetric: LF-then-HF and
    HF-then-LF are both blocked.
    """
    other = MECH_LF if mecid == MECID else MECID
    last = index.get((hotkey, pair.upper(), other))
    if last is None:
        return False
    return 0 <= int(t_ms) - int(last) < PAIR_LOCK_MS


def build_lock_index(rows) -> dict:
    """rows: iterable of (hotkey, pair, mecid, ts_ms) -> newest-wins index."""
    idx: dict = {}
    for hk, pair, mecid, ts in rows:
        k = (hk, str(pair).upper(), int(mecid))
        if ts > idx.get(k, -1):
            idx[k] = int(ts)
    return idx


def load_hf_locks(log_dir: str, since_ms: int) -> list:
    """HF receipts as lock rows, read from a LOCAL ingest log dir (flat <w>.jsonl).

    NOT the LF side's feed, despite what this docstring claimed until 2026-07-24.
    It reads our own ingest state, which no third party can replay, and its flat
    glob does not even match the published layout (<w>/receipts.jsonl), so it
    returns nothing when pointed at the public dir. An LF void resolved from here
    would be unverifiable — and silently empty.

    The LF side uses `hf_grade.load_hf_lock_rows(base, since_ms)`, which fetches
    the published, Merkle-anchored windows. This stays for local tooling.

    Returns (hotkey, pair, MECID, ts_ms).
    """
    import json
    import pathlib as _p
    rows = []
    d = _p.Path(log_dir)
    if not d.is_dir():
        raise HFLockFeedError(f"HF log dir missing: {log_dir}")
    for f in sorted(d.glob("*.jsonl")):
        try:
            for line in f.open():
                e = json.loads(line)
                sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
                payload = sub.get("payload") or {}
                if str(payload.get("kind", "")) == "closers":
                    continue          # a closers vote never locks the pair
                pair = payload.get("trade_pair")
                ts = rcpt.get("grid_t0_ms")
                if pair and ts and int(ts) >= since_ms:
                    rows.append((sub.get("hk"), str(pair).upper(), MECID, int(ts)))
        except Exception as e:
            raise HFLockFeedError(f"{f.name}: {e}") from e
    return rows


def load_mech0_locks(db_path: str, since_ms: int) -> list:
    """Mechanism-0 submissions as lock rows, read from the validator journal.

    Without this the cross-mechanism check is inert: `is_pair_locked` was only ever
    fed HF accepts, so an LF gold call could not lock gold on HF — the exact
    double-pay the rule exists to prevent. Returns (hotkey, pair, MECH_LF, ts_ms).

    Reads the miner's committed pair from the revealed plaintext. A commit whose
    blob is still sealed cannot lock anything, which is correct: until it reveals
    nobody knows what pair it was, including us.
    """
    import json
    import sqlite3
    rows = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        for r in con.execute(
                "SELECT hotkey, t0_unix, plaintext FROM signals "
                "WHERE plaintext IS NOT NULL AND t0_unix > ? "
                "AND status != 'void'", (since_ms / 1000.0,)):
            try:
                pair = json.loads(r["plaintext"]).get("trade_pair")
            except Exception:
                continue
            if pair:
                rows.append((r["hotkey"], str(pair).upper(), MECH_LF,
                             int(r["t0_unix"] * 1000)))
        con.close()
    except Exception as e:
        # NEVER swallow this silently. An empty lock feed looks identical to
        # "nobody traded", so a broken query disables the rule and double-pay
        # resumes with no symptom. (A trailing comma in IN ('void',) did exactly
        # that during development.) Surface it and let the caller decide.
        print(f"[hf] LOCK FEED FAILED — cross-mechanism lock is NOT enforcing: {e}",
              flush=True)
        raise HFLockFeedError(str(e)) from e
    return rows


# ── pricing grid (CONSENSUS) ─────────────────────────────────────────────────
# Entry is NOT priced at receipt time — that would make the category a race to
# colocate beside the ingest, where a few ms of RTT decides ties. Everything inside
# one bucket fills at one published tick.
# The grid MUST NOT be finer than the feed's own timestamp granularity. Measured on
# the Polygon tick bus 2026-07-22 over a 12 s window:
#
#   BTCUSD  (crypto XT.* trades)  45 distinct src_ts, ALL with non-zero ms, ~250 ms apart
#   ETHUSD  (crypto XT.* trades)  13 distinct src_ts, non-zero ms, ~600 ms apart
#   XAUUSD  (forex  C.*  quotes)  10 distinct src_ts, ms part ALWAYS 000, ~1000 ms apart
#   EURUSD  (forex  C.*  quotes)   5 distinct src_ts, ms part ALWAYS 000, ~1500 ms apart
#
# Polygon's consolidated forex/metals quote feed timestamps to the SECOND. Pricing
# gold on a 250 ms grid would be fiction: the sub-second component could only come
# from OUR receive clock, which no third party can replay — reintroducing exactly the
# trust hole the receipts exist to close. So the grid is per asset class, and a class
# can only be tightened when its feed can prove the resolution.
#
# A 1 s grid is not a compromise on gold: against a 30 min horizon it is 1/1800 of the
# hold, and it kills the colocation race just as completely as 250 ms would.
GRID_MS_BY_CLASS = {
    "crypto": 250,
    "forex": 1000,
    "forex-commodities": 1000,
}
GRID_MS_DEFAULT = 1000
MIN_SETTLE_MS = 50


def grid_ms_for(pair: str, t0_unix: float = 0.0) -> int:
    # Falls back to the v1 board when t0 predates launch: the grid is a mechanical
    # property of the FEED, not a governed consensus value, so it must resolve even
    # for a timestamp with no board. Never returns finer than the coarsest grid for
    # an unknown pair.
    board = hf_bands_as_of(t0_unix) or HF_BOARD_V1
    row = board.get(str(pair).upper())
    return GRID_MS_DEFAULT if row is None else GRID_MS_BY_CLASS.get(row[3], GRID_MS_DEFAULT)


def grid_t0_ms(t_recv_ms: int, pair: str = None, t0_unix: float = 0.0) -> int:
    """Next grid point at least MIN_SETTLE_MS after receipt, on the pair's own grid."""
    g = GRID_MS_DEFAULT if pair is None else grid_ms_for(pair, t0_unix)
    t = int(t_recv_ms) + MIN_SETTLE_MS
    return -(-t // g) * g                      # ceil to the grid


# ── canonical bytes + receipts (CONSENSUS) ───────────────────────────────────
SUBMIT_DOMAIN = b"SN89-HF-v1|"
RECEIPT_DOMAIN = b"SN89-HF-RCPT-v1|"
# Anchor cadence is capped by the commitment SPACE BUDGET, not by us. Measured on
# testnet 496, 2026-07-22:
#
#   Commitments.MaxSpace          = 3100 bytes
#   UsedSpaceOf[netuid, hotkey]   = {last_epoch, used_space} - resets every EPOCH
#   a 91-byte anchor is charged     100 bytes
#   epoch = tempo 360 blocks x 12 s = 72 min
#
# => 31 anchors per epoch, i.e. one per ~139 s. A 60 s window would need 72 anchors
# (7200 bytes) against a 3100-byte budget and would run dry a third of the way into
# every epoch. `Commitments.set_max_space` is global and root-only, so the budget is
# not ours to raise.
#
# 180 s => 24 anchors/epoch = 2400 bytes, leaving ~700 bytes (7 anchors) of headroom
# for INCLUSION REPAIR catch-up anchors, which draw on the same budget.
#
# This is settlement granularity, NOT submission latency: a call is still bound in
# ~300 us by its receipt. The window only sets how long until that receipt is
# chain-anchored - at most 3 minutes.
ANCHOR_WINDOW_S = 180
ANCHOR_BYTES_CHARGED = 100         # measured, not the payload length
ANCHOR_SPACE_BUDGET = 3100         # Commitments.MaxSpace, per hotkey per epoch
CHALLENGE_WINDOW_S = 24 * 3600     # inclusion-repair deadline (see docs)
MAX_CLOCK_SKEW_MS = 5000


def canonical_json(obj) -> str:
    """Sorted keys, no whitespace — same canonicalization as the mech-0 blob path."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def submit_signing_bytes(hk: str, seq: int, nonce: str, payload: dict,
                         ts_miner: int) -> bytes:
    return (SUBMIT_DOMAIN + f"{hk}|{int(seq)}|{nonce}|".encode()
            + canonical_json(payload).encode() + f"|{int(ts_miner)}".encode())


def payload_hash(signing_bytes: bytes) -> str:
    return hashlib.blake2b(signing_bytes, digest_size=32).hexdigest()


def receipt_signing_bytes(hk: str, seq: int, ph: str, t_recv_us: int,
                          grid_ms: int, ing: str) -> bytes:
    return (RECEIPT_DOMAIN
            + f"{hk}|{int(seq)}|{ph}|{int(t_recv_us)}|{int(grid_ms)}|{ing}".encode())


def leaf(receipt_bytes: bytes) -> bytes:
    return hashlib.blake2b(receipt_bytes, digest_size=32).digest()


def merkle_root(leaves: list[bytes]) -> str:
    """blake2b-256, duplicate-last on odd levels. Empty window -> 32 zero bytes."""
    if not leaves:
        return "00" * 32
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.blake2b(level[i] + level[i + 1], digest_size=32).digest()
                 for i in range(0, len(level), 2)]
    return level[0].hex()


def window_start_ms(t_ms: int) -> int:
    w = ANCHOR_WINDOW_S * 1000
    return (int(t_ms) // w) * w


def leaf_order_key(r: dict):
    """The ONLY ordering a verifier may use: (t_recv_us, hotkey, seq)."""
    return (int(r["t_recv_us"]), str(r["hk"]), int(r["seq"]))


def anchor_payload(w_start_ms: int, receipts: list[dict], log_url_tag: str,
                   tick_root: str | None = None, tick_n: int = 0) -> dict:
    """The window's on-chain commitment.

    Carries TWO roots. `root` binds what miners said; `tick_root` binds the prices we
    will grade them against. Anchoring only the receipts would leave us free to
    restate the price series afterwards and decide outcomes retroactively — the tick
    root is what makes grading falsifiable rather than merely published.
    """
    ordered = sorted(receipts, key=leaf_order_key)
    root = merkle_root([leaf(receipt_signing_bytes(
        r["hk"], r["seq"], r["ph"], r["t_recv_us"], r["grid_t0_ms"], r["ing"]))
        for r in ordered])
    out = {"v": 1, "t": "hf-anchor", "w": int(w_start_ms), "n": len(ordered),
           "root": root, "log": log_url_tag}
    if tick_root is not None:
        out["tick_root"] = tick_root
        out["tick_n"] = int(tick_n)
    return out


# ── published tick series (CONSENSUS) ────────────────────────────────────────
# One line per DISTINCT source timestamp per asset, ordered by (t, asset). The
# recorder dedupes on the feed's own src_ts, so the series carries exactly the
# resolution the feed actually provides and never interpolates.
def tick_bytes(t: dict) -> bytes:
    return canonical_json({k: t[k] for k in ("a", "t", "p") if k in t}).encode()


def tick_order_key(t: dict):
    return (int(t["t"]), str(t["a"]))


def tick_root(ticks: list[dict]) -> str:
    return merkle_root([hashlib.blake2b(tick_bytes(t), digest_size=32).digest()
                        for t in sorted(ticks, key=tick_order_key)])


def price_at(ticks_sorted: list[dict], t_ms: int):
    """Price for one asset at a grid point: the LAST tick at or before it.

    Never interpolates and never looks forward. Returns None when the series has no
    tick at or before t — a grid point with no price does not grade, it voids.
    """
    out = None
    for t in ticks_sorted:
        if int(t["t"]) <= int(t_ms):
            out = t
        else:
            break
    return None if out is None else float(out["p"])


# ── validity (CONSENSUS) ─────────────────────────────────────────────────────
class HFLockFeedError(Exception):
    """The mechanism-0 lock feed could not be read. Distinct from "no locks" —
    treating the two the same is what silently disables the rule."""


class HFRejected(Exception):
    """Refused at ingest. Always returned to the miner SIGNED, so a refusal is
    distinguishable from censorship."""


def validate_submission(payload: dict, t0_unix: float) -> None:
    board = hf_bands_as_of(t0_unix)
    if board is None:
        raise HFRejected("hf_not_live_at_t0")
    pair = str(payload.get("trade_pair", "")).upper()
    if pair not in board:
        raise HFRejected(f"pair_not_on_hf_board:{pair}")
    tp_bps, sl_bps, horizon_s, cls = board[pair]
    if str(payload.get("direction")) not in ("LONG", "SHORT"):
        raise HFRejected("bad_direction")
    if float(payload.get("tp_bps", 0)) != tp_bps or float(payload.get("sl_bps", 0)) != sl_bps:
        raise HFRejected(f"band_mismatch:expected {tp_bps}/{sl_bps}")
    if int(payload.get("horizon_s", 0)) != horizon_s:
        raise HFRejected(f"horizon_mismatch:expected {horizon_s}")
    if str(payload.get("asset_class")) != cls:
        raise HFRejected(f"asset_class_mismatch:expected {cls}")


def check_rate(prior_ts_ms: list, t_ms: int, t0_unix: float) -> None:
    """prior_ts_ms: this hotkey's accepted HF submit times, ascending."""
    cap, gap_ms, _ = hf_rules_as_of(t0_unix)
    day = int(t_ms) // 86_400_000
    if sum(1 for x in prior_ts_ms if int(x) // 86_400_000 == day) >= cap:
        raise HFRejected(f"daily_cap:{cap}")
    if prior_ts_ms and int(t_ms) - int(prior_ts_ms[-1]) < gap_ms:
        raise HFRejected(f"min_gap:{gap_ms}ms")


# ── same-mechanism open-position gate (CONSENSUS) ────────────────────────────
def open_until_ms(direction: str, entry, tp_bps: float, sl_bps: float,
                  t0_ms: int, horizon_s: int, ticks_sorted: list) -> int:
    """The ms at which this call STOPS holding its pair.

    Earlier of the first decisive touch and the horizon wash. Deliberately the same
    walk as `grade()` — same `touch_hit`, same `MIN_TOUCH_TICKS` wick guard, same
    "ignore ticks at or before t0" bound — so a call can never be scored decisive at
    one instant and counted open past it. If the two ever diverge, the gate voids
    calls the board says were legal.

    `ticks_sorted` may be TRUNCATED (the ingest and the bot only hold ticks up to
    now). A truncated series simply has not touched anything yet, so the answer is
    the horizon end, which is the correct conservative reading of "still open": the
    caller compares against its own clock and a call cannot be past a horizon that
    has not elapsed. Once the horizon HAS elapsed the series is complete and the
    answer is final and identical for every replayer.

    A call with no entry price voids and therefore holds nothing -> t0_ms.
    """
    from .grader import touch_hit          # THE shared rule — see grade()

    t_end = int(t0_ms) + int(horizon_s) * 1000
    if entry is None or float(entry) <= 0:
        return int(t0_ms)
    up = 1 if direction == "LONG" else -1
    tp = float(entry) * (1 + up * tp_bps / 10000.0)
    sl = float(entry) * (1 - up * sl_bps / 10000.0)
    need = config.MIN_TOUCH_TICKS
    won_ct = lost_ct = 0
    for t in ticks_sorted:
        tm = int(t["t"])
        if tm <= int(t0_ms):
            continue
        if tm > t_end:
            break
        r = touch_hit(float(t["p"]), up > 0, tp, sl)
        if r == "lost":
            lost_ct += 1
            if lost_ct >= need:
                return tm
        elif r == "won":
            won_ct += 1
            if won_ct >= need:
                return tm
    return t_end


class OpenCall:
    """Live, incremental twin of `open_until_ms` — same rule, fed tick by tick.

    `open_until_ms` needs the whole series in hand, which the grader has and a
    real-time caller does not: the ingest decides in under a millisecond and the
    published tick windows are ~3 min behind (the recorder writes a window at SEAL).
    So the live path carries one of these per open call and advances it off the tick
    bus, holding two counters instead of a tick history.

    It must stay behaviourally identical to `open_until_ms`: same `touch_hit`, same
    `MIN_TOUCH_TICKS`, same exclusive-at-t0 / inclusive-at-t_end bounds. Divergence
    here does not corrupt the board — the grader is the authority — but it makes the
    ingest refuse calls the board will score, which is the worse direction.
    """
    __slots__ = ("pair", "direction", "t0_ms", "end_ms", "tp", "sl",
                 "up", "won", "lost", "closed_ms")

    def __init__(self, pair: str, direction: str, entry, tp_bps: float,
                 sl_bps: float, t0_ms: int, horizon_s: int):
        self.pair = str(pair).upper()
        self.direction = direction
        self.t0_ms = int(t0_ms)
        self.end_ms = int(t0_ms) + int(horizon_s) * 1000
        self.up = 1 if direction == "LONG" else -1
        self.won = self.lost = 0
        if entry is None or float(entry) <= 0:
            # No entry price -> the call voids and never holds the pair.
            self.tp = self.sl = 0.0
            self.closed_ms = int(t0_ms)
        else:
            self.tp = float(entry) * (1 + self.up * tp_bps / 10000.0)
            self.sl = float(entry) * (1 - self.up * sl_bps / 10000.0)
            self.closed_ms = None

    def on_tick(self, t_ms: int, price: float) -> None:
        from .grader import touch_hit
        if self.closed_ms is not None:
            return
        t_ms = int(t_ms)
        if t_ms <= self.t0_ms or t_ms > self.end_ms:
            return
        r = touch_hit(float(price), self.up > 0, self.tp, self.sl)
        if r == "lost":
            self.lost += 1
            if self.lost >= config.MIN_TOUCH_TICKS:
                self.closed_ms = t_ms
        elif r == "won":
            self.won += 1
            if self.won >= config.MIN_TOUCH_TICKS:
                self.closed_ms = t_ms

    def open_until(self) -> int:
        """The ms this call stops holding its pair — the touch, or the wash."""
        return self.end_ms if self.closed_ms is None else self.closed_ms


def check_pair_open(prior_open_until_ms: list, t_ms: int, t0_unix: float) -> None:
    """Refuse a submission whose pair the hotkey is already holding.

    `prior_open_until_ms`: for each of THIS hotkey's earlier ACCEPTED, NON-VOID calls
    on THIS pair, the value of `open_until_ms`. A voided call holds nothing, so it
    must not appear here — otherwise the first refusal chains and locks the pair for
    everything behind it.

    No-op before HF_OPEN_GATE_FROM. The 4 declared at launch was never applied, and
    a replay must not retroactively void calls that were legal when they landed.

    The refusal reason carries WHEN THE PAIR FREES, as
    `pair_open_same_mechanism:<openmax>:<free_at_ms>`. Without it the gate is
    unactionable: a miner is told the pair is held and has no way to find out for
    how long, so the only strategy available is to keep firing and keep getting
    refused. Canefis lost 13 of 38 submissions to exactly that on 2026-07-31,
    including a run of four and a run of five, and could not have avoided one of
    them with the information he had. His positions were resolving on TP/SL well
    inside the 30-min horizon, so even guessing the horizon would have been wrong
    in the expensive direction.

    APPENDED, not substituted. Everything that reads this string matches on the
    part before the first ':' (`_refusal_help`, `refusedNote`), and openmax stays
    in field 1 where it has always been.

    `free_at_ms` is an UPPER BOUND on the live path, and say so wherever it is
    shown. A holder that has already touched reports its exact touch; one that has
    not yet touched reports its horizon, because that is the latest it can hold.
    Which is the useful direction for a client scheduling a retry — retry then and
    the pair is free for certain — and the dangerous direction to present as exact,
    since a miner told "free at 14:59" would sit out the 14:35 touch that actually
    released it.
    """
    if t0_unix < HF_OPEN_GATE_FROM:
        return
    _, _, openmax = hf_rules_as_of(t0_unix)
    still_open = sorted(int(u) for u in prior_open_until_ms if int(t_ms) < int(u))
    if len(still_open) >= openmax:
        # The gate clears once enough of the holders have closed to bring the count
        # below openmax, which is the (n - openmax)-th soonest to close. At the live
        # openmax of 1 that is simply the last one standing.
        raise HFRejected("pair_open_same_mechanism:"
                         f"{openmax}:{still_open[len(still_open) - openmax]}")


# ── scoring scope ────────────────────────────────────────────────────────────
# HF tallies into its own MinerState and its own weight vector. Qualification is
# unchanged from mech 0. One constant DOES change:
#
#  * win cap — config.WIN_CAP = 20 is calibrated to ~1 call/day and would bind on
#    every active HF miner inside a week, so above it volume would stop mattering
#    in a category whose entire premise is volume.
#
# DECAY IS NOW 7 DAYS, MATCHING MECH 0 (Whit, 2026-07-31). It shipped at 48h on
# the argument that a 7-day memory is too slow to track current form where trades
# resolve in 30 minutes. What that actually bought was a much harsher cliff than
# LF: a miner who drops below the hit-rate floor loses its residual 3.5x faster,
# for a category where a single bad session can pull the floor out. HF and LF now
# fall back at the same rate, which is also what a trader moving between the two
# expects. Current form is already tracked by the reputation window and by the
# fact that a loss freezes accrual immediately — decay is the fall-back rate, not
# the form signal, and those are different jobs.
#
# NOT as-of gated (there is no as-of mechanism for decay on either mechanism; the
# LF 7-day change on 2026-07-22 shipped the same way). Decay is evaluated against
# `now`, so this does not touch any grade or any past qualified win — it changes
# what today's vector computes from them. A replay of a PAST block will not
# reproduce the weights we committed before this change; that is inherent to any
# decay-constant change and is why it belongs in one commit on master.
HF_EMISSION_DECAY_S = int(os.getenv("SN89_HF_EMISSION_DECAY_S", str(7 * 24 * 3600)))
HF_WIN_CAP = int(os.getenv("SN89_HF_WIN_CAP", "200"))
HF_QUALIFY_MIN_DECISIVE = config.QUALIFY_MIN_DECISIVE     # identical to mech 0
HF_QUALIFY_LB_FLOOR = config.QUALIFY_LB_FLOOR             # identical to mech 0

# Reputation memory is 60 DAYS on both mechanisms (Whit, 2026-07-23) -- trading more
# often must not shorten how far back your record is read.
#
# Mech 0 bounds the window by TWO things and takes whichever is tighter: 60 days OR
# the last HIT_RATE_WINDOW_TRADES=100 decisive. At HF cadence (30/day, ~20% wash =
# ~24 decisive/day) the TRADE cap binds after ~4 days, so inheriting 100 would have
# silently given HF a 4-day memory. Sized so the 60-day clock is what binds:
# 30 calls/day x 60 days = 1800 decisive at zero wash, so 2000 never binds.
#
# Consequence, and it is the statistics working rather than a loosening: the Wilson
# small-sample penalty shrinks with n, so the gate falls from ~57% observed at n=100
# to ~52% at n=1440. A trader with a true 60% rate passes at either size; the change
# only affects genuinely-marginal traders we previously excluded because we could not
# TELL, not because we had judged them. Quality differentiation lives in the tier
# ladder (55/60/70), not in the gate, which is only a noise floor.
HF_HIT_RATE_WINDOW_S = config.HIT_RATE_WINDOW_S           # 60 days, same as mech 0
HF_HIT_RATE_WINDOW_TRADES = int(os.getenv("SN89_HF_HIT_RATE_WINDOW_TRADES", "2000"))

# Within-mechanism burn cap for HF, analogous to config.MINER_EMISSION_CAP for
# mecid 0. The chain-level emission split divides emissions BETWEEN mechanisms;
# this caps how much of HF's own share reaches miners vs. burns to UID 0. Defaults
# to the mecid-0 cap so both mechanisms burn the same fraction unless set otherwise.
HF_MINER_EMISSION_CAP = float(os.getenv("SN89_HF_MINER_EMISSION_CAP",
                                        str(config.MINER_EMISSION_CAP)))
_HF_CAP_ENV = os.getenv("SN89_HF_MINER_EMISSION_CAP", "")


def hf_miner_emission_cap_as_of(t_unix: float) -> float:
    """HF's cap at `t_unix`. Follows config.MINER_EMISSION_CAP_HISTORY unless an
    explicit HF override is set (testnet), matching the module-constant default
    it replaces — HF has never diverged from mecid-0 on this."""
    if _HF_CAP_ENV:
        return float(_HF_CAP_ENV)
    return config.miner_emission_cap_as_of(t_unix)

# ── HF eligibility gate (CONSENSUS) — replaces the LF elapsed-time warmup ──────
# LF warmup is pure elapsed time: first_seen + IMMUNITY_S (8 days), regardless of
# how much the miner actually traded. At HF's 30/day cadence that gate is far too
# weak — a miner could clear the 8-decisive Wilson gate in a few HOURS of spamming
# one day and qualify. HF instead requires a real track record before ANY win
# earns (Whit, 2026-07-24):
#
#   >= HF_QUALIFY_MIN_SUBMISSIONS accepted submissions (wash/void included — the
#     count is of participation, not of decisive outcomes), AND
#   submissions on >= HF_QUALIFY_MIN_TRADING_DAYS DISTINCT UTC days.
#
# "Trading days" is the new concept: a day counts iff the miner had >= 1 accepted
# submission that UTC day. Eight distinct trading days can't be faked by one day of
# volume, and — unlike LF — eight IDLE calendar days no longer qualify anyone. The
# Wilson LB >= 0.50 edge gate (HF_QUALIFY_LB_FLOOR) still applies on top, at each
# win, over the recent decisive window. Wins before the miner becomes eligible are
# warmup and never earn; wins after do.
HF_QUALIFY_MIN_SUBMISSIONS = int(os.getenv("SN89_HF_QUALIFY_MIN_SUBMISSIONS", "50"))
HF_QUALIFY_MIN_TRADING_DAYS = int(os.getenv("SN89_HF_QUALIFY_MIN_TRADING_DAYS", "8"))


def hf_eligible_from(sub_ts_ms) -> float | None:
    """The unix time (seconds) at which a hotkey first satisfies BOTH HF volume
    gates: >= HF_QUALIFY_MIN_SUBMISSIONS accepted submissions AND submissions on
    >= HF_QUALIFY_MIN_TRADING_DAYS distinct UTC days. None until both hold.

    PURE / deterministic in the submission timestamps, so every validator replaying
    the published windows reaches the same eligibility instant. Both counters are
    monotonic in time, so the tipping submission is well-defined: it is the one that
    makes the later-satisfied of the two thresholds true.
    """
    days = set()
    for i, t_ms in enumerate(sorted(int(t) for t in sub_ts_ms), start=1):
        days.add(int(t_ms) // 86_400_000)          # UTC day index
        if i >= HF_QUALIFY_MIN_SUBMISSIONS and len(days) >= HF_QUALIFY_MIN_TRADING_DAYS:
            return t_ms / 1000.0
    return None

MECID_1 = 1

# ── self-hosted miner submission ─────────────────────────────────────────────
# Where a miner sends signed HF frames, and the ingest's published receipt key so
# the miner can verify the receipt it gets back is genuinely ours. The key set is
# also published on chain; this default is the current ingest identity.
HF_INGEST_WSS = os.getenv("SN89_HF_INGEST_WSS", "wss://hf.infinitequant.app")
HF_RECEIPT_PUBKEY = os.getenv(
    "SN89_HF_RECEIPT_PUBKEY", "5FTc1VxLMabBGqzqHjy62cDuMmRLGdMohxyhkBAUBpzfstCz")


@contextlib.contextmanager
def hf_scoring_config(now: float | None = None):
    """Run a block with `config` carrying the HF window/cap constants, restored on
    exit. Scoped with try/finally and NEVER applied at import time, because hf.py
    is imported by the ingest — a module-level mutation of config would corrupt LF
    scoring in any process that imports both.

    Anything that calls scoring.* on HF outcomes must go through this, not just
    hf_compute_weights: the website's HF board reads the same tier ladder, wash
    efficiency and per-win stamps, and computing those against the LF constants
    would put a different Value× on the page than the one the vector was built
    from. One definition, every call site.
    """
    saved = {k: getattr(config, k) for k in (
        "EMISSION_DECAY_S", "WIN_CAP", "SCORE_WINDOW_S", "IMMUNITY_S",
        "HIT_RATE_WINDOW_TRADES", "HIT_RATE_WINDOW_TRADES_V2", "MINER_EMISSION_CAP")}
    try:
        config.EMISSION_DECAY_S = HF_EMISSION_DECAY_S
        config.WIN_CAP = HF_WIN_CAP
        config.SCORE_WINDOW_S = HF_EMISSION_DECAY_S          # trailing_* (reporting) only
        # make hit_rate_window_trades_as_of return the HF cap for every t0
        config.HIT_RATE_WINDOW_TRADES = HF_HIT_RATE_WINDOW_TRADES
        config.HIT_RATE_WINDOW_TRADES_V2 = HF_HIT_RATE_WINDOW_TRADES
        config.MINER_EMISSION_CAP = hf_miner_emission_cap_as_of(
            time.time() if now is None else now)
        # The HF warmup is the eligibility INSTANT, not an elapsed span. Zero the
        # LF immunity clock and hand each miner its eligible_from as first_seen, so
        # warmup_end (= first_seen + IMMUNITY_S) collapses to exactly that instant
        # in scoring.score_inputs, scoring.qualified_wins and compute_weights'
        # immune/probation checks. All three then treat pre-eligibility wins as
        # warmup — one definition, three call sites, no drift.
        config.IMMUNITY_S = 0
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def hf_compute_weights(decisive_by_hk: dict, first_seen_by_hk: dict,
                       uid_by_hk: dict, now: float, subs_by_hk: dict,
                       graded_by_hk: dict | None = None) -> dict:
    """{uid: normalized_weight} for mecid 1, from HF-ONLY graded outcomes.

    Reuses the SAME battle-tested tally as mecid 0 (scoring.qualified_wins,
    scoring.compute_weights, scoring._qualifies) — a win still qualifies only if the
    miner had a passing Wilson bound over its recent decisive window at that win.
    HF differs in the window constants (200 win cap; decay matches mech 0 at 7 days
    since 2026-07-31), the burn cap, and — the
    load-bearing one — the WARMUP gate: LF warmup is 8 elapsed days from first_seen;
    HF replaces it with hf_eligible_from() (>= HF_QUALIFY_MIN_SUBMISSIONS accepted
    submissions across >= HF_QUALIFY_MIN_TRADING_DAYS distinct UTC days). A miner not
    yet eligible earns nothing; once eligible, wins from that instant on can earn.

    `subs_by_hk` maps hotkey -> list of accepted-submission timestamps (ms), the
    thing eligibility is computed from — the FULL accepted set (wash/void included),
    not just decisive outcomes.

    What is deliberately NOT run here (unlike mecid 0): copy detection, referrals,
    and elimination re-derivation. Signed real-time submissions have no timelock-copy
    surface, HF has no referral program, and a bad HF miner simply stops qualifying
    and decays to zero (the no-cliff design) rather than being eliminated.

    CRITICAL — decisive_by_hk must contain ONLY HF outcomes. A miner qualified on
    mecid 0 is NOT qualified here: their HF decisive count starts at zero.

    Constant overrides are scoped with try/finally and NEVER applied at import time,
    because hf.py is imported by the ingest — a module-level mutation of config would
    corrupt LF scoring in any process that imports both.
    """
    from . import scoring

    with hf_scoring_config(now):
        states = []
        for hk, decisive in decisive_by_hk.items():
            uid = uid_by_hk.get(hk)
            if uid is None:
                continue
            eligible = hf_eligible_from(subs_by_hk.get(hk, []))
            if eligible is None:
                continue                         # < 50 subs or < 8 trading days → no weight
            rep_won, rep_dec, won_all, _won_orig, _copies, tw = scoring.score_inputs(
                decisive, eligible, now)
            qwins = scoring.qualified_wins(
                decisive, eligible, habitual=False,
                graded=(graded_by_hk or {}).get(hk))
            states.append(scoring.MinerState(
                hotkey=hk, uid=uid, first_seen_unix=eligible,
                rep_wins=rep_won, rep_decisive=rep_dec, trailing_wins=won_all,
                qwins=qwins))
        return scoring.compute_weights(states, now)


def hf_compute_tallies(decisive_by_hk: dict, first_seen_by_hk: dict,
                       uid_by_hk: dict, now: float, subs_by_hk: dict,
                       graded_by_hk: dict | None = None) -> dict:
    """{hotkey: decayed qualified-win tally} over HF-only outcomes — the RAW
    earning currency behind hf_compute_weights, before any floor or cap.

    Exists for the referrer mechanism (§ referrer multicomp), which scores a
    recruiter off what their recruits are earning across every competition and
    must NOT read a weight vector to get it: compute_weights adds the immunity
    dust floor and the probation floor, and paying a recruiter for those would
    make registering idle hotkeys profitable.

    Same eligibility gate as the vector — a miner below HF_QUALIFY_MIN_SUBMISSIONS
    or HF_QUALIFY_MIN_TRADING_DAYS is absent, not zero — so the two cannot
    disagree about who is a participant. Runs inside hf_scoring_config for the
    same reason every other HF caller does: the LF constants would produce a
    different tally than the vector was built from.
    """
    from . import scoring

    with hf_scoring_config(now):
        out: dict[str, float] = {}
        for hk, decisive in decisive_by_hk.items():
            if uid_by_hk.get(hk) is None:
                continue
            eligible = hf_eligible_from(subs_by_hk.get(hk, []))
            if eligible is None:
                continue
            qwins = scoring.qualified_wins(
                decisive, eligible, habitual=False,
                graded=(graded_by_hk or {}).get(hk))
            t = scoring.decayed_qwin_tally(qwins, now)
            if t > 0:
                out[hk] = t
        return out


# ── on-chain anchor encoding (CONSENSUS) ─────────────────────────────────────
# A commitment field holds at most 128 RAW BYTES, and two 64-char hex roots alone
# are already 128 — so the two roots are bound into ONE window root and the halves
# are published in the window log. Verification: recompute both roots from the log,
# combine, compare to chain.
#
# `CommitmentOf` is also a single LATEST-WINS slot per hotkey, so the anchor chain is
# not readable from current state — it is reconstructed by scanning set_commitment
# extrinsics from the anchoring hotkey, exactly as mechanism 0 does for miners. A
# window whose anchor was overwritten before anyone observed it is unverifiable, which
# is why the anchoring hotkey must anchor and do nothing else.
# Where a replaying validator fetches published HF window logs (receipts + ticks
# + anchor) - the mecid-1 analogue of config.R2_PUBLIC_BASE for LF blobs. A window
# lives at <base>/<w>/{receipts.jsonl,ticks.jsonl,ticks.json,anchor.json}; the
# index is <base>/index.json. Makes HF weights REPRODUCIBLE by any validator.
HF_PUBLIC_BASE = os.getenv("SN89_HF_PUBLIC_BASE",
                           "https://partner.infinitequant.app/sn89/hf")

ANCHOR_PREFIX = "sn89hf:1:"
ANCHOR_MAX_BYTES = 128


def window_root(root_hex: str, tick_root_hex: str) -> str:
    """Bind what miners said to the prices they will be graded against."""
    return hashlib.blake2b(bytes.fromhex(root_hex) + bytes.fromhex(tick_root_hex),
                           digest_size=32).hexdigest()


def encode_anchor(w_start_ms: int, n: int, tick_n: int,
                  root_hex: str, tick_root_hex: str) -> str:
    s = (f"{ANCHOR_PREFIX}{int(w_start_ms) // 1000}:{int(n)}:{int(tick_n)}:"
         f"{window_root(root_hex, tick_root_hex)}")
    if len(s.encode()) > ANCHOR_MAX_BYTES:
        raise ValueError(f"anchor {len(s.encode())}B exceeds the {ANCHOR_MAX_BYTES}B field")
    return s


def decode_anchor(s: str) -> dict | None:
    if not s or not s.startswith(ANCHOR_PREFIX):
        return None
    try:
        w_s, n, tick_n, wroot = s[len(ANCHOR_PREFIX):].split(":")
        return {"w": int(w_s) * 1000, "n": int(n), "tick_n": int(tick_n),
                "window_root": wroot}
    except Exception:
        return None


def verify_anchor(onchain: str, root_hex: str, tick_root_hex: str,
                  n: int, tick_n: int) -> bool:
    """What a third party runs: recompute both roots from the published log and
    confirm the chain committed to exactly this window."""
    a = decode_anchor(onchain)
    return bool(a and a["n"] == n and a["tick_n"] == tick_n
                and a["window_root"] == window_root(root_hex, tick_root_hex))


# ── grading (CONSENSUS) ──────────────────────────────────────────────────────
def grade(receipt_pair: str, direction: str, entry: float, tp_bps: float,
          sl_bps: float, t0_ms: int, horizon_s: int, ticks_sorted: list) -> dict:
    """A level scores after config.MIN_TOUCH_TICKS ticks TOUCH it; nothing touched
    by the horizon is a WASH.

    Delegates to grader.touch_hit — the SAME function mechanism 0 uses. The two
    mechanisms differ only in bands, horizon, and which price series they are
    handed; the per-tick hit rule and the ≥MIN_TOUCH_TICKS wick guard are one
    place, so they can never drift apart. A lone reverting tick never scores.
    """
    from .grader import touch_hit          # THE shared rule — see grader.touch_hit

    if entry is None or entry <= 0:
        return {"status": "void", "reason": "no_entry_price"}
    up = 1 if direction == "LONG" else -1
    tp = entry * (1 + up * tp_bps / 10000.0)
    sl = entry * (1 - up * sl_bps / 10000.0)
    t_end = int(t0_ms) + int(horizon_s) * 1000
    # ≥2 wick guard, unconditional: HF is already tick-native with no material grade
    # history (no qualified HF miners yet), so no as-of is needed — a lone reverting
    # tick simply stops scoring. LF adopts the same guard at its touch_ticks cutover.
    need = config.MIN_TOUCH_TICKS
    won_ct = lost_ct = 0
    for t in ticks_sorted:
        tm = int(t["t"])
        if tm <= int(t0_ms):
            continue
        if tm > t_end:
            break
        px = float(t["p"])
        r = touch_hit(px, up > 0, tp, sl)
        if r == "lost":
            lost_ct += 1
            if lost_ct >= need:
                return {"status": "lost", "exit": px, "exit_ms": tm}
        elif r == "won":
            won_ct += 1
            if won_ct >= need:
                return {"status": "won", "exit": px, "exit_ms": tm}
    return {"status": "wash", "exit": None, "exit_ms": t_end}
