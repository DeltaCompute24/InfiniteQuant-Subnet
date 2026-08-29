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
    # Added 2026-08-12 for the V3 listing. CONSERVATIVE (upper-bound) estimates, not
    # direct measurements: measuring our anchored corpus reproduces the eight values
    # above only to within -100%/+297%, so a raw number from it does not belong in this
    # table. Instead all crypto pairs were measured on ONE window and calibrated against
    # SOLUSD/XRPUSD (the thinnest incumbents, hence the honest comparators), taking the
    # UPPER end of the factor range. Erring wide is the safe direction here: it makes
    # every band_spread_ratio a floor rather than a best case.
    "TAOUSD": 3.80, "HYPEUSD": 3.00,
    # Added 2026-08-12 for the v4 FX narrowing. This one IS a direct measurement, not an
    # estimate: FX carries real top-of-book (bid + ask) in the anchored corpus, unlike the
    # trades-only crypto rows above. p75 over 14d of sealed windows with the rollover
    # excluded (p50 0.570), taking the upper quartile because the whole point of the gate
    # is that the listing survives a bad spread day and not merely a median one.
    "AUDUSD": 0.85,
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

# v3 — lists TAOUSD and HYPEUSD, both at the 7200s FX-style horizon.
#
# TAOUSD does NOT fit at 1800s and this is the load-bearing detail: its 14%-target band
# is 25.8 bps, clearing 8x spread needs 30.4 bps, and 30.4 washes 22.4% — past the 7pp
# deadband around the target. No 30-minute band satisfies both gates, which is the spread
# gate correctly saying the pair is too spread-heavy for that clock. The board's own
# precedent is the FX majors, on 7200s for exactly this reason.
#
# At 7200s, on the 45d window, both hit the target and clear with room:
#   TAOUSD   53.1 bps  wash 14.01%  14.0x spread
#   HYPEUSD  62.6 bps  wash 13.98%  20.9x spread
#
# HYPEUSD would also clear at 1800s (28.3 bps, 9.4x) but its spread is estimated from 61
# anchored ticks, and 9.4x is too near the 8.0x gate to rest on an estimate. Revisit once
# it has a measured p50.
HF_BOARD_V3 = dict(
    HF_BOARD_V2,
    TAOUSD=(53.1, 53.1, 7200, "crypto"),
    HYPEUSD=(62.6, 62.6, 7200, "crypto"),
)
HF_V3_FROM = int(os.getenv("SN89_HF_V3_FROM", "1786500000"))   # 2026-08-12T02:00:00Z

# v4 — the FX narrowing, mirroring the LF fxmacro3-20260813 entry. EURUSD, GBPUSD and
# USDJPY leave the board; AUDUSD joins. The LF board keeps three forex pairs and HF keeps
# ONE, and that asymmetry is the spread gate doing its job rather than an oversight.
#
# Solved on the same 45d calendar window as LF, 14% structural-wash target, against
# spreads measured over 14d of SEALED anchored windows with the FX rollover (20:55-21:20
# UTC) excluded. The three departing pairs are the control: the solver returns 5.00 / 5.20
# / 4.14 against a listed 5.0 / 6.0 / 4.0, and the measured spread p50 reproduces the
# recorded table at 0.82x / 0.89x / 1.28x, so its answer for AUDUSD is trustworthy.
#
#   AUDUSD   8.4 bps @ 7200s   wash 14.0%   9.9x spread   <- listed
#   USDCAD   4.7 bps @ 7200s   wash 14.0%   7.3x spread   <- HELD, under the 8.0x gate
#   USDCHF   7.5 bps @ 7200s   wash 14.0%   7.6x spread   <- HELD, under the 8.0x gate
#
# USDCAD and USDCHF clear only on a LONGER clock (4h: 10.1x and 10.0x), which is the
# TAOUSD-v3 remedy and was the tempting one. It was declined: those ratios hold on the
# spread p50 and both pairs fail at EVERY HF-length horizon on the p75, and USDCHF's
# spread p90 is 4.94 bps against a 0.99 p50 — a tail fat enough that the gate is right to
# refuse it. A pair listed on the strength of the estimator you happened to pick is a pair
# whose outcomes are microstructure. Revisit if a dedicated pass gives either a defensible
# p50; until then they are LF-only, and LF is where their 12h horizon suits them anyway.
HF_BOARD_V4 = {k: v for k, v in HF_BOARD_V3.items()
               if k not in ("EURUSD", "GBPUSD", "USDJPY")}
HF_BOARD_V4["AUDUSD"] = (8.4, 8.4, 7200, "forex")
HF_V4_FROM = int(os.getenv("SN89_HF_V4_FROM", "1786579200"))   # 2026-08-13T00:00:00Z

HF_BANDS_HISTORY = (
    (HF_LAUNCH_FROM, HF_BOARD_V1),
    (HF_V2_FROM, HF_BOARD_V2),
    (HF_V3_FROM, HF_BOARD_V3),
    (HF_V4_FROM, HF_BOARD_V4),
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


def next_submit_seq(path: str) -> int:
    """The per-hotkey submission counter, floored at wall clock (MICROSECONDS).

    The ingest's replay gate is `seq <= last_seq[hotkey] -> stale_seq`, and it keeps
    ONE scalar per hotkey. That is fine for one signer and a permanent lockout for two,
    because nothing reconciles their counters. A hotkey really can have two: the trader's
    self-hosted `neurons/miner.py` AND our dashboard path (`scripts/limit_watcher.py`),
    which signs on their behalf. Before 2026-08-11 those seeded from `0` and from
    `int(time.time())` respectively, so the instant a trader used the dashboard, `last_seq`
    jumped to ~1.79e9 and every submission from their own miner was refused FOREVER — the
    client would need ~1.78 billion submissions to climb back. Measured: 9 hotkeys, 45
    rejections, and every one of them had used both paths.

    Flooring at wall clock is what makes independent signers safe: each one lands near
    `now` regardless of how many calls it has made, so neither can strand the other.
    `max(prev + 1, ...)` keeps it strictly increasing even if the clock steps backwards,
    which is the property the ingest actually requires.

    MICROSECONDS, not seconds, so two signers collide only inside the same microsecond
    instead of the same second. A collision is now a transient that clears on the next
    submission rather than a wedge.

    It also self-heals both sides on first use after this ships: a stale small counter and
    a web counter days behind wall clock both jump to `now`. Fixing only one signer would
    simply have moved the lockout to the other — the web counters were ~9.5 days behind
    `now` when this was found, so a client seeded at `now` would have locked out the
    dashboard instead.

    `seq` is formatted as a decimal string into the signing bytes with no width bound
    (`submit_signing_bytes`), so the larger magnitude changes nothing on the wire.
    """
    prev = -1
    try:
        with open(path, encoding="utf-8") as fh:
            prev = int(json.load(fh).get("seq", -1))
    except (OSError, ValueError, TypeError, AttributeError):
        pass                    # missing or corrupt -> the wall-clock floor covers us
    seq = max(prev + 1, int(time.time() * 1_000_000))
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # Atomic: a torn write used to leave a truncated file, and the old readers treated
    # that as "start from 0" — which is exactly the lockout this function exists to stop.
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seq": seq}, fh)
    os.replace(tmp, path)
    return seq


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
    if str(payload.get("asset_class")) != cls:
        raise HFRejected(f"asset_class_mismatch:expected {cls}")

    if config.custom_bands_enforced_as_of(t0_unix):
        # CUSTOM SIZING. The board stops being an equality test and becomes an
        # envelope. What the miner may not do is pick a shape whose outcome is
        # microstructure rather than opinion, which is the SAME spread test that
        # earns a pair its board slot -- reused here per-call instead of per-pair.
        _validate_custom_band(payload, pair)
        return

    if float(payload.get("tp_bps", 0)) != tp_bps or float(payload.get("sl_bps", 0)) != sl_bps:
        raise HFRejected(f"band_mismatch:expected {tp_bps}/{sl_bps}")
    if int(payload.get("horizon_s", 0)) != horizon_s:
        raise HFRejected(f"horizon_mismatch:expected {horizon_s}")


def _validate_custom_band(payload: dict, pair: str) -> None:
    """Envelope check for a miner-declared band. Pure; raises HFRejected."""
    try:
        tp = float(payload.get("tp_bps", 0))
        sl = float(payload.get("sl_bps", 0))
        hz = int(payload.get("horizon_s", 0))
    except (TypeError, ValueError):
        raise HFRejected("bad_band_payload")

    # Symmetric only, for v1. An asymmetric band is strictly more expressive and
    # the first-passage maths still closes, but a no-view miner's win probability
    # stops being 1/2 and the payout would have to price the skew as well. Until
    # it does, an asymmetric band would be mispriced rather than merely unsupported.
    if tp != sl:
        raise HFRejected(f"band_not_symmetric:{tp}/{sl}")
    if not (0 < tp <= config.HF_CUSTOM_MAX_BAND_BPS):
        raise HFRejected(f"band_out_of_range:{tp}")
    if not (config.HF_CUSTOM_MIN_HORIZON_S <= hz <= config.HF_CUSTOM_MAX_HORIZON_S):
        raise HFRejected(f"horizon_out_of_range:{hz}")

    spread = HF_TYPICAL_SPREAD_BPS.get(pair)
    if spread is None:
        # No measured spread means no floor can be applied, and a band that cannot
        # be floored is a band whose outcome we cannot vouch for. Refuse rather
        # than wave it through -- the quiet direction here is the wrong one.
        raise HFRejected(f"no_spread_for_pair:{pair}")
    floor = spread * MIN_BAND_SPREAD_RATIO
    if tp < floor:
        raise HFRejected(f"band_under_spread_floor:{tp}<{floor:.2f}")


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
    for i, t_ms in enumerate(sorted(_sub_ts(s) for s in sub_ts_ms), start=1):
        days.add(int(t_ms) // 86_400_000)          # UTC day index
        if i >= HF_QUALIFY_MIN_SUBMISSIONS and len(days) >= HF_QUALIFY_MIN_TRADING_DAYS:
            return t_ms / 1000.0
    return None


def _sub_ts(s) -> int:
    """t0_ms out of a submission record, which comes in two shapes.

    Since the diversity gate (below) a record is `(t0_ms, pair, direction)`; before
    it, and in every caller that only needs the clock, it is a bare t0_ms. Both are
    accepted so a producer that has no direction to give (an old grade cache mid
    rebuild) still computes the SAME eligibility instant as one that does — the
    volume gate never depended on pair or direction and must not start now.
    """
    return int(s[0] if isinstance(s, (tuple, list)) else s)


# ── HF submission-diversity gate (CONSENSUS) ─────────────────────────────────
# A hotkey that submits the same direction on the same handful of pairs forever is
# not forecasting, it is holding an opinion and being paid per hour for restating
# it. On a 30/day cadence that is the cheapest loop on the mechanism: a cron that
# fires LONG BTCUSD every hour rides any up-tape into a passing hit rate without
# ever taking the other side of anything.
#
# It cannot be caught by the edge gate. The Wilson floor asks whether a miner wins,
# and in a trending tape a frozen miner does win — measured 2026-08-12, the two
# largest offenders sat at ranks 1 and 2 of the board on 65.4% and 64.9% hit rate,
# together holding 38% of the HF pool, across 378 and 300 submissions of which ZERO
# were SHORT. So diversity is a separate axis from accuracy and needs its own gate.
#
# The rule, deliberately loose (Whit, 2026-08-12): a miner must take BOTH sides some
# of the time, and how much "some" is scales with how narrow its universe is. A
# miner covering the whole board can be lopsided — that is a house view across many
# instruments, and it is still eight independent decisions. A miner on two pairs
# that has never once gone the other way has made one decision and repeated it.
#
#   distinct pairs   minimum minority-direction share
#   <= 2             HF_DIVERSITY_FLOOR_NARROW   (0.20)
#   3-4              HF_DIVERSITY_FLOOR_MID      (0.12)
#   5-6              HF_DIVERSITY_FLOOR_WIDE     (0.06)
#   >= 7             HF_DIVERSITY_FLOOR_BROAD    (0.03)
#
# minority-direction share = SUM OVER PAIRS of min(#LONG_p, #SHORT_p), divided by n,
# over accepted submissions in the trailing HF_DIVERSITY_WINDOW_S. Below
# HF_DIVERSITY_MIN_SUBS in that window the gate does not apply at all — a low-volume
# miner has not had the chance to be two-sided, and the ratio is noise at small n.
#
# ── Why the min() is INSIDE the pair (2026-08-19) ────────────────────────────
# It was `min(SUM LONG, SUM SHORT) / n` — one ratio over the whole hotkey — and that
# let the minimum be satisfied on a pair the miner does not trade. Measured over 30
# days on the coldkey 5DyPn97u… cluster (four hotkeys, 43.75% of the HF pool):
#
#   BTCUSD  951 calls    0 short     SOLUSD  592 calls    4 short
#   XRPUSD   64 calls   55 short     XAU/TAO/HYPE  8 calls each, exactly 4 short
#
# 1,543 of 1,696 calls carried four short positions between them, while a 64-call
# XRP book ran 86% short and three padding pairs were split precisely half and half.
# The hotkey read 4.25% and cleared its floor; the money was one-directional.
#
# Summing min() per pair prices that correctly: a pair contributes only the
# two-sidedness it actually has, so the cluster reads 0.0-0.5% instead of 4.0-4.2%.
# It also disarms the breadth arbitrage without a second constant — a 1-call padding
# pair now contributes min(0,1) = 0 to the numerator while still adding 1 to n, so
# buying pair-count to reach a lower floor is a small NET COST. To lift a 500-call
# book to the 3% floor a miner must place ~15 minority calls inside pairs it really
# trades, which is the exposure the gate is asking for and cannot be faked cheaply.
#
# CALIBRATION — and why calibration is no longer the argument. The original note
# read: "six keys sit at 0.0-4.4% and twenty-five at 15-50%, nothing in between, so
# the gate is insensitive to where exactly it is set." That was true on 2026-08-12
# and FALSE by 2026-08-19: the empty band filled from below the moment the gate
# armed and its constants were public. A published threshold stops being a
# classifier and becomes a target, so the rule has to be structurally sound rather
# than well-placed. Under the per-pair sum the picture is (2026-08-19, 40 hotkeys
# with >= 20 subs) four keys at 0.0-0.5% and the rest at 17.1-46.0% — a wider gap
# than before, but the reason to trust it is the paragraph above, not the gap.
#
# Failing is REVERSIBLE and carries no elimination — weight is zero for as long as
# the trailing window is one-sided and returns by itself once the miner starts
# taking the other side. That matches HF's no-cliff design: a bad HF miner decays to
# zero rather than being cut. It is also why the window is trailing and not
# all-time: a miner that reforms should be able to earn again without re-registering.
HF_DIVERSITY_ENABLED = os.getenv("SN89_HF_DIVERSITY_ENABLED", "1") == "1"
HF_DIVERSITY_WINDOW_S = int(os.getenv("SN89_HF_DIVERSITY_WINDOW_S", str(30 * 86400)))
HF_DIVERSITY_MIN_SUBS = int(os.getenv("SN89_HF_DIVERSITY_MIN_SUBS", "40"))
HF_DIVERSITY_FLOOR_NARROW = float(os.getenv("SN89_HF_DIVERSITY_FLOOR_NARROW", "0.20"))
HF_DIVERSITY_FLOOR_MID = float(os.getenv("SN89_HF_DIVERSITY_FLOOR_MID", "0.12"))
HF_DIVERSITY_FLOOR_WIDE = float(os.getenv("SN89_HF_DIVERSITY_FLOOR_WIDE", "0.06"))
HF_DIVERSITY_FLOOR_BROAD = float(os.getenv("SN89_HF_DIVERSITY_FLOOR_BROAD", "0.03"))


def hf_diversity_floor(n_pairs: int) -> float:
    """The minimum minority-direction share demanded of a miner covering `n_pairs`
    distinct pairs. Monotonically non-increasing in breadth."""
    if n_pairs <= 2:
        return HF_DIVERSITY_FLOOR_NARROW
    if n_pairs <= 4:
        return HF_DIVERSITY_FLOOR_MID
    if n_pairs <= 6:
        return HF_DIVERSITY_FLOOR_WIDE
    return HF_DIVERSITY_FLOOR_BROAD


def hf_diversity(subs, now: float) -> dict:
    """Diversity verdict for ONE hotkey over the trailing window.

    `subs` is the accepted-submission list — `(t0_ms, pair, direction)` records. The
    returned dict is the whole audit trail, so the validator log, the public board
    and the watcher all quote identical numbers rather than three reconstructions:

        {"n", "pairs", "long", "short", "minority", "share", "floor",
         "by_pair", "applies", "ok"}

    `share` is `minority / n` where `minority` sums min(long, short) WITHIN each
    pair — see the rule note above for why the min() sits inside the pair. `by_pair`
    carries the per-pair (long, short) counts so an operator can see which book
    supplied the two-sidedness without recounting from the feed.

    PURE / deterministic in the submissions and `now`, like every other consensus
    function here — `now` enters only as the trailing-window edge, and validators
    already agree on the weight-cycle clock to well inside a 30-day window.

    `n` counts only records that actually carry a direction. Records that do not —
    the legacy bare-timestamp shape, or a grade-cache row written before the
    `direction` column existed — are skipped entirely rather than counted as
    neither side, because counting them would DEPRESS an honest miner's share and
    fail it on nothing but the age of a cache. A half-migrated validator therefore
    measures a smaller window, never a wronger one, and once `n` falls below
    HF_DIVERSITY_MIN_SUBS the gate reports `applies=False` and abstains. (This is
    not a way out for a miner: direction is a required, signed payload field that
    `validate_submission` rejects a submission without, so an untyped record can
    only ever be OUR bookkeeping, never a miner's choice.)
    """
    cutoff_ms = (now - HF_DIVERSITY_WINDOW_S) * 1000.0
    by_pair: dict = {}
    longs, shorts = 0, 0
    for s in subs or ():
        if not isinstance(s, (tuple, list)) or len(s) < 3:
            continue
        t_ms, pair, direction = s[0], s[1], s[2]
        if int(t_ms) < cutoff_ms:
            continue
        d = str(direction or "").upper()
        if d == "LONG":
            longs += 1
        elif d == "SHORT":
            shorts += 1
        else:
            continue                              # untyped: not a measurement
        # Records carrying no pair keep their own bucket: they count toward `n`
        # and toward the minority sum exactly as they did before this became a
        # per-pair measure, but they never contribute BREADTH. Preserving that
        # split matters because the pair field is required and signed, so an
        # unpaired record can only ever be our own bookkeeping.
        lp = by_pair.setdefault(str(pair) if pair else "", [0, 0])
        lp[0 if d == "LONG" else 1] += 1
    n = longs + shorts
    # THE per-pair sum. min() is applied INSIDE each pair and then pooled, so a
    # pair can only donate the two-sidedness it actually has. See the note above.
    minority = sum(min(l, s) for l, s in by_pair.values())
    pairs = {p for p in by_pair if p}
    share = (minority / n) if n else 0.0
    floor = hf_diversity_floor(len(pairs))
    applies = HF_DIVERSITY_ENABLED and n >= HF_DIVERSITY_MIN_SUBS
    return {"n": n, "pairs": len(pairs), "long": longs, "short": shorts,
            "minority": minority, "share": share, "floor": floor,
            "by_pair": {p: tuple(v) for p, v in sorted(by_pair.items())},
            "applies": applies, "ok": (not applies) or share >= floor}


def hf_diversity_ok(subs, now: float) -> bool:
    """`hf_diversity(...)["ok"]` — the gate as the weight path consumes it."""
    return bool(hf_diversity(subs, now)["ok"])

from .integrity import integrity_ok  # noqa: E402

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

    `subs_by_hk` maps hotkey -> list of accepted submissions as
    `(t0_ms, pair, direction)`, the thing eligibility is computed from — the FULL
    accepted set (wash/void included), not just decisive outcomes. It carries pair
    and direction because the diversity gate reads them; the volume gate reads only
    the timestamp and still accepts a bare-timestamp list (see `_sub_ts`).

    TWO gates run over it, and a miner must clear both to hold any weight:
    `hf_eligible_from` (has it participated enough) and `hf_diversity_ok` (does it
    take both sides, at a threshold scaled to how many pairs it trades).

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
            subs = subs_by_hk.get(hk, [])
            eligible = hf_eligible_from(subs)
            if eligible is None:
                continue                         # < 50 subs or < 8 trading days → no weight
            if not hf_diversity_ok(subs, now):
                continue                         # one-sided on a narrow universe → no weight
            # Owner-hosted integrity verdict. ADDITIVE to the gate above, deliberately:
            # the published floor stays exactly where it is, so a miner tuning to clear it
            # keeps spending calls on a threshold that no longer saves them, while the
            # detection that actually catches a coordinated cohort is not readable here.
            # See sn89_signals/integrity.py for why the rule is not in this repo.
            if not integrity_ok(hk, now):
                continue                         # flagged by the integrity service
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

    Same two gates as the vector — a miner below HF_QUALIFY_MIN_SUBMISSIONS or
    HF_QUALIFY_MIN_TRADING_DAYS, or failing the diversity floor, is absent rather
    than zero — so the two cannot disagree about who is a participant. The
    diversity gate belongs here for the same reason the eligibility gate does:
    without it a recruiter would be paid for recruits running an always-long cron,
    which is exactly the behaviour the gate exists to stop being profitable. Runs inside hf_scoring_config for the
    same reason every other HF caller does: the LF constants would produce a
    different tally than the vector was built from.
    """
    from . import scoring

    with hf_scoring_config(now):
        out: dict[str, float] = {}
        for hk, decisive in decisive_by_hk.items():
            if uid_by_hk.get(hk) is None:
                continue
            subs = subs_by_hk.get(hk, [])
            eligible = hf_eligible_from(subs)
            if eligible is None:
                continue
            if not hf_diversity_ok(subs, now):
                continue
            if not integrity_ok(hk, now):
                continue                         # flagged by the integrity service
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
