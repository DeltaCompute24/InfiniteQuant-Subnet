"""Closers — the exit-timing competition (competition key "closers").

Miners watch the network's OPEN positions (published as a feed) and submit
HOLD or CLOSE on a position at any moment they choose. The call is graded
CLOSERS_HORIZON_S later against the actual move:

    fav      = direction_sign × (p1 − p0) / p0     (positive = position improved)
    raw      = +fav/σ_pair  for HOLD,  −fav/σ_pair  for CLOSE
    score    = winsorize(raw, ±CLOSERS_WINSOR_Z) − baseline(asset_class)

Design decisions carried in from the 2026-07-31 gaming analysis — each one
closes a measured exploit, do not relax them casually:

  * MAGNITUDE scoring, not binary win-count. Net-win-count is a random walk
    whose spread grows √N, so ranking on it pays submission volume, not skill
    (a 20k-flip spammer beats a 60%-accurate selective miner ~half of weeks).
    Magnitude also carries ~3× the information per call, which is what makes a
    30/day cap statistically workable at all.
  * VOL-NORMALIZED per pair. Realized 1h σ spans 16× across the universe
    (USDCAD 5.3 bps → ZEC 86 bps, measured 2026-07-31 off the marks corpus);
    raw-bps scoring would rank asset selection, not exit judgment.
  * WINSORIZED at ±3σ. Magnitude scores are heavier-tailed than a binary walk,
    which would make best-of-N sybil keys stronger; clipping removes the
    jackpot while keeping essentially all the signal.
  * BASELINE-SUBTRACTED per asset class. Being in a position carries drift
    whose SIGN differs by class (crypto 49.2% favors-the-position vs FX 51.5%)
    and by grading unit (crypto mean +0.0155σ but median −0.0095σ), so any
    no-information button is free until the measured drift is subtracted.
    Constants ship at 0 until re-measured on a corpus that covers the
    HL/USDC half of the book (marks gap, 2026-07-31).
  * DAILY CAP + a MIN-CALLS gate on ranking. The cap bounds the variance a
    zero-skill account can buy; the min-N keeps a 2-lucky-calls key off the
    board. Score is a decayed SUM (not a rate), so under the cap volume can
    only help a miner who is actually right more than wrong.

Submissions ride the EXIST HF ingest verbatim — same signed frame, same
countersigned receipt, same Merkle-anchored windows, same tick feed — with
`payload.kind == "closers"`. Nothing about HF's latency or the owner-key path
changes; a closers entry in a window log is just one more receipt line that HF
grading skips and this module picks up. Works identically for hosted miners
(multiplexer signs and submits for the tenant) and self-hosted (they sign and
hit the ingest themselves), because the ingest never distinguishes the two.

The POSITIONS FEED is the one new input: a JSON document of the network's open
positions, published by us (env SN89_CLOSERS_POSITIONS_URL). The ingest
validates a submission against the feed at accept time (position exists, pair
and direction match) and embeds the position's direction in the accepted
payload — so GRADING never needs the feed and stays replayable from the
published windows + ticks alone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request

from . import config, hf

# ── consensus constants (env-tunable so testnet grades in minutes) ───────────
CLOSERS_HORIZON_S = int(os.getenv("SN89_CLOSERS_HORIZON_S", "3600"))
CLOSERS_WINDOW_S = int(os.getenv("SN89_CLOSERS_WINDOW_S", str(60 * 24 * 3600)))
CLOSERS_MAX_PER_DAY = int(os.getenv("SN89_CLOSERS_MAX_PER_DAY", "30"))
CLOSERS_MIN_GAP_MS = int(os.getenv("SN89_CLOSERS_MIN_GAP_MS", "10000"))
CLOSERS_MIN_CALLS = int(os.getenv("SN89_CLOSERS_MIN_CALLS", "10"))
CLOSERS_WINSOR_Z = float(os.getenv("SN89_CLOSERS_WINSOR_Z", "3.0"))
# Eligibility: on mainnet a closers entrant must already be a qualified HF or LF
# miner (the sybil gate from the design review — best-of-N throwaway keys are
# the cheapest attack on any leaderboard). Testnet flips this off so fresh test
# miners can enter immediately.
CLOSERS_REQUIRE_QUALIFIED = os.getenv("SN89_CLOSERS_REQUIRE_QUALIFIED", "1") == "1"
GRADE_SETTLE_S = int(os.getenv("SN89_CLOSERS_GRADE_SETTLE_S", "30"))
GRADE_ABANDON_S = int(os.getenv("SN89_CLOSERS_GRADE_ABANDON_S", str(6 * 3600)))
GRADER_VERSION = 1

# Where the ingest reads the open-positions feed. Grading never touches this.
POSITIONS_URL = os.getenv("SN89_CLOSERS_POSITIONS_URL", "")
POSITIONS_REFRESH_S = int(os.getenv("SN89_CLOSERS_POSITIONS_REFRESH_S", "5"))

# ── σ normalizer, bps per 1h, measured 2026-07-31 (30d marks corpus, 15-min
# grid, ~2,800 obs/pair). A pair not in the table falls back by asset class —
# wrong σ only mis-scales that pair's scores, it cannot flip their sign.
SIGMA_1H_BPS = {
    "ZECUSD": 86.4, "HYPEUSD": 66.2, "TAOUSD": 62.9, "XAGUSD": 51.9,
    "ETHUSD": 48.1, "SOLUSD": 47.1, "XRPUSD": 43.5, "BTCUSD": 35.3,
    "XAUUSD": 26.8, "USDJPY": 13.2, "GBPJPY": 11.6, "NZDUSD": 10.3,
    "USDCHF": 9.1, "AUDUSD": 9.0, "GBPUSD": 7.4, "EURUSD": 6.5,
    "AUDNZD": 5.7, "GBPCAD": 5.7, "USDCAD": 5.3,
}
SIGMA_FALLBACK_BPS = {"crypto": 45.0, "forex": 9.0, "metals": 35.0, "equities": 20.0}

# Baseline drift to subtract, in σ units, per (asset_class, action). Ships at 0:
# the 2026-07-31 measurement was directionally clear but ran on 66 gradeable
# positions with 167× pseudo-replication and no HL/USDC coverage — not constants
# to wire in. Re-measure once the marks corpus carries the USDC book.
BASELINE_Z = {}


def sigma_bps(pair: str, asset_class: str = "") -> float:
    v = SIGMA_1H_BPS.get(str(pair).upper())
    if v:
        return v
    return SIGMA_FALLBACK_BPS.get(asset_class or "", 30.0)


def baseline_z(asset_class: str, action: str) -> float:
    return float(BASELINE_Z.get((asset_class, action), 0.0))


# ── ingest-side validation ───────────────────────────────────────────────────
class ClosersRejected(hf.HFRejected):
    """Refused at ingest — signed, like every HF refusal."""


_positions_cache: dict = {"at": 0.0, "by_id": {}}


def fetch_positions(url: str = "", now: float | None = None) -> dict[str, dict]:
    """{position_id: {trade_pair, direction, ...}} from the published feed,
    cached POSITIONS_REFRESH_S. Raises on a dead feed — the ingest must REFUSE
    (signed, retryable) rather than accept a submission it cannot check."""
    now = time.time() if now is None else now
    url = url or POSITIONS_URL
    if not url:
        raise ClosersRejected("positions_feed_unconfigured")
    if now - _positions_cache["at"] <= POSITIONS_REFRESH_S and _positions_cache["by_id"]:
        return _positions_cache["by_id"]
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            doc = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise ClosersRejected(f"positions_feed_unreachable:{type(e).__name__}") from e
    by_id = {str(p["id"]): p for p in (doc.get("positions") or []) if p.get("id")}
    _positions_cache.update(at=now, by_id=by_id)
    return by_id


def validate_submission(payload: dict, t0_unix: float,
                        positions: dict[str, dict] | None = None) -> None:
    """Ingest-time check for a closers payload. `positions` injectable for tests;
    None → fetch the live feed. On success the payload is COMPLETE for grading
    (pair + position direction + action) — grading never re-reads the feed."""
    action = str(payload.get("action", "")).upper()
    if action not in ("HOLD", "CLOSE"):
        raise ClosersRejected("bad_action")
    pid = str(payload.get("position_id", ""))
    if not pid:
        raise ClosersRejected("missing_position_id")
    pos = (positions if positions is not None else fetch_positions(now=t0_unix)).get(pid)
    if pos is None:
        raise ClosersRejected(f"unknown_position:{pid}")
    pair = str(payload.get("trade_pair", "")).upper()
    if pair != str(pos.get("trade_pair", "")).upper():
        raise ClosersRejected(f"pair_mismatch:{pair}")
    if str(payload.get("direction", "")).upper() != str(pos.get("direction", "")).upper():
        raise ClosersRejected("direction_mismatch")


def check_rate(prior_ts_ms: list, t_ms: int) -> None:
    """Closers rate rules: CLOSERS_MAX_PER_DAY per UTC day + a short min gap.
    Same shape as hf.check_rate, separate constants — the 30/day cap is the
    anti-variance-farming bound from the design review, not a UX limit."""
    day = int(t_ms) // 86_400_000
    if sum(1 for x in prior_ts_ms if int(x) // 86_400_000 == day) >= CLOSERS_MAX_PER_DAY:
        raise ClosersRejected(f"daily_cap:{CLOSERS_MAX_PER_DAY}")
    if prior_ts_ms and int(t_ms) - int(prior_ts_ms[-1]) < CLOSERS_MIN_GAP_MS:
        raise ClosersRejected(f"min_gap:{CLOSERS_MIN_GAP_MS}ms")


# ── scoring ──────────────────────────────────────────────────────────────────
def call_score(direction: str, action: str, p0: float, p1: float,
               pair: str, asset_class: str = "") -> float:
    """One graded call's score, in σ units. Symmetric in HOLD/CLOSE by
    construction — neither button is free (the flat premium idea from the
    design discussion turns a fair coin into +EV; a proper scoring rule can't)."""
    if not p0 or p0 <= 0 or p1 is None:
        raise ValueError("bad prices")
    d = 1.0 if str(direction).upper() == "LONG" else -1.0
    fav = d * (float(p1) - float(p0)) / float(p0)
    z = fav / (sigma_bps(pair, asset_class) * 1e-4)
    raw = z if str(action).upper() == "HOLD" else -z
    w = CLOSERS_WINSOR_Z
    return max(-w, min(w, raw)) - baseline_z(asset_class, str(action).upper())


# ── grade cache (mirrors hf_grade's window→pending→graded pipeline) ──────────
def _db(cache_dir: str) -> sqlite3.Connection:
    os.makedirs(cache_dir, exist_ok=True)
    c = sqlite3.connect(os.path.join(cache_dir, "closers_grades.db"), timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS windows_seen (w INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS pending ("
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, "
              "direction TEXT, action TEXT, asset_class TEXT, end_ms INTEGER, "
              "pid TEXT DEFAULT '')")
    try:  # pre-existing caches: add the position-id column in place
        c.execute("ALTER TABLE pending ADD COLUMN pid TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS grades ("
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, "
              "action TEXT, score REAL, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = c.execute("SELECT v FROM meta WHERE k='grader_version'").fetchone()
    if (int(row[0]) if row else 0) != GRADER_VERSION:
        c.execute("DELETE FROM grades")
        c.execute("DELETE FROM pending")
        c.execute("DELETE FROM windows_seen")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('grader_version',?)",
                  (str(GRADER_VERSION),))
        c.commit()
    return c


def sync_and_grade(base: str, cache_dir: str, now: float,
                   on_new=None) -> None:
    """Pull closers receipts out of the published HF windows into `pending`,
    then score every call whose horizon has settled. Incremental and
    replayable: inputs are the anchored window logs + the anchored tick feed,
    both public. Deliberately reuses hf_grade's fetch/cache machinery — one
    window sync serves both competitions.

    on_new(entry): optional observer fired once per NEWLY-SEEN submission
    (full receipt entry: submit + countersigned receipt). Side-effect hook for
    the caller (the validator notifies the operator channel); grading itself
    stays pure — the callback never influences what is journaled or scored."""
    from . import hf_grade  # late import: hf_grade imports hf, avoid cycles

    now_ms = int(now * 1000)
    db = _db(cache_dir)
    tick_dir = os.path.join(cache_dir, "ticks")

    seen = {r[0] for r in db.execute("SELECT w FROM windows_seen")}
    graded = {r[0] for r in db.execute("SELECT key FROM grades")}
    for w in hf_grade._index(base):
        if w in seen:
            continue
        txt = hf_grade._fetch_text(f"{base.rstrip('/')}/{w}/receipts.jsonl")
        if txt is None:
            continue                        # not published yet — retry next cycle
        for line in txt.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
            p = sub.get("payload") or {}
            if str(p.get("kind", "")) != "closers":
                continue                    # HF entries belong to hf_grade
            hk, seq, t0_ms = sub.get("hk"), sub.get("seq"), rcpt.get("grid_t0_ms")
            key = f"{hk}:{seq}"
            if not hk or not t0_ms or key in graded:
                continue
            db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?,?,?,?)",
                       (key, hk, int(t0_ms), str(p.get("trade_pair", "")).upper(),
                        str(p.get("direction", "")).upper(),
                        str(p.get("action", "")).upper(),
                        str(p.get("asset_class", "")),
                        int(t0_ms) + CLOSERS_HORIZON_S * 1000,
                        str(p.get("position_id", ""))))
            if on_new is not None:
                try:
                    on_new(e)
                except Exception:  # noqa: BLE001 — observer must never break sync
                    pass
        db.execute("INSERT OR IGNORE INTO windows_seen VALUES (?)", (w,))
    db.commit()

    due = db.execute("SELECT key, hk, t0_ms, pair, direction, action, asset_class, "
                     "end_ms FROM pending WHERE end_ms <= ? ORDER BY end_ms, key",
                     (now_ms - GRADE_SETTLE_S * 1000,)).fetchall()
    for key, hk, t0_ms, pair, direction, action, acls, end_ms in due:
        ticks, missing = hf_grade._ticks_for(base, tick_dir, pair,
                                             int(t0_ms), int(end_ms))
        if missing and now_ms < int(end_ms) + GRADE_ABANDON_S * 1000:
            continue                        # never score a hole — wait or abandon
        p0 = hf.price_at(ticks, int(t0_ms))
        p1 = hf.price_at(ticks, int(end_ms))
        if p0 is None or p1 is None:
            # No resolvable price even after abandon: void, score 0. Same
            # deadline discipline as HF — a call can never wedge pending forever.
            if now_ms >= int(end_ms) + GRADE_ABANDON_S * 1000:
                db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?,?)",
                           (key, hk, int(t0_ms), pair, action, 0.0, "void"))
                db.execute("DELETE FROM pending WHERE key=?", (key,))
            continue
        s = call_score(direction, action, p0, p1, pair, acls)
        db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?,?)",
                   (key, hk, int(t0_ms), pair, action, float(s), "graded"))
        db.execute("DELETE FROM pending WHERE key=?", (key,))
    db.commit()
    db.close()


# ── weights ──────────────────────────────────────────────────────────────────
def closers_weights(uid_by_hk: dict, now: float | None = None,
                    base: str | None = None, cache_dir: str | None = None,
                    qualified_hks: set | None = None) -> dict:
    """{uid: normalized_weight} for the closers competition — the analogue of
    replay.weights_from_journal (LF) and hf_grade.mecid1_weights (HF).

    Ranking basis: SUM of winsorized, vol-normalized, baseline-subtracted call
    scores over the trailing CLOSERS_WINDOW_S. A negative sum earns nothing.
    Min-N: fewer than CLOSERS_MIN_CALLS graded calls in the window earns
    nothing (a 2-call lucky key must keep proving it before it ranks).
    qualified_hks: the HF/LF-qualified set when CLOSERS_REQUIRE_QUALIFIED.
    Burn + the miner emission cap behave exactly as the other competitions."""
    now = time.time() if now is None else now
    base = base or hf.HF_PUBLIC_BASE
    cache_dir = cache_dir or os.path.expanduser(
        os.getenv("SN89_CLOSERS_GRADE_CACHE", "~/.sn89/closers-grade"))
    sync_and_grade(base, cache_dir, now)

    since_ms = int((now - CLOSERS_WINDOW_S) * 1000)
    db = _db(cache_dir)
    per_hk: dict[str, list[float]] = {}
    for hk, score, status in db.execute(
            "SELECT hk, score, status FROM grades WHERE t0_ms >= ?", (since_ms,)):
        if status == "graded":
            per_hk.setdefault(hk, []).append(float(score))
    db.close()

    weights: dict[int, float] = {}
    scores: dict[int, float] = {}
    for hk, ss in per_hk.items():
        uid = uid_by_hk.get(hk)
        if uid is None:
            continue
        if CLOSERS_REQUIRE_QUALIFIED and hk not in (qualified_hks or set()):
            continue
        if len(ss) < CLOSERS_MIN_CALLS:
            continue
        total = sum(ss)
        if total > 0:
            scores[uid] = total

    pool = sum(scores.values())
    cap = config.MINER_EMISSION_CAP
    if pool > 0:
        for uid, s in scores.items():
            weights[uid] = cap * (s / pool)
    weights[config.BURN_UID] = weights.get(config.BURN_UID, 0.0) + \
        max(0.0, 1.0 - sum(weights.values()))
    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()}
