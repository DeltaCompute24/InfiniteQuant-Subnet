"""SN89 Markets — on-chain Up/Down prediction markets (CONSENSUS).

Every validator, and the IQ markets service, imports THIS module so the price every bet paid,
every outcome and every score come out identical on every machine. Nothing here reads IQ's
private state: the inputs are the Merkle-anchored HF window logs (where bets travel as signed
`kind == "mk.bet"` submissions) and the sealed tick windows. Design: ~/IQ/SN89-PREDICTION-
MARKETS-SPEC.md, "On-chain Markets (Whit, 2026-10-08)".

Armed by `config.MARKETS_FROM_UNIX`: mainnet 2026-10-09 00:00:00 UTC in source (the same instant
the committed COMP_WEIGHTS history moves Closers' 0.125 to Markets); testnet earlier via env.
Before the arm no market exists, and ingest refuses every `mk.bet` received earlier than one
open lead before it (config.markets_accepting_as_of).

MARKETS (phase 1: Up/Down only)
  id       "UD:<ASSET>:<window>:<start_unix>", window in MARKETS_WINDOWS ("15m", "1h"), start
           aligned to the window length in UTC. The asset must be on the HF board in force at the
           start, and a session-bound class must be open for the whole window.
  target   simple average of the marks in [start - 60 s, start] (inclusive, sealed ticks).
  outcome  UP if the average of the marks in [end - 60 s, end] is AT LEAST the target (a tie is
           Up); no tick in either minute -> VOID (every bet refunded).
  mark     bid/ask mid when both are positive, else the trade price — the testnet service's rule
           (sn89_markets/ladders/updown.py), including its float average rounded to 6 significant
           digits, so the testnet settlement and this one agree to the last digit. Ticks are taken
           window by window in ascending order, each window's ticks sorted by (t, mark).
  entries  a bet counts only if its receipt time is in
           [start - MARKETS_OPEN_LEAD_S, start - MARKETS_ENTRY_CLOSE_LEAD_S): the market opens one
           window before its start and closes before the target's averaging minute begins, so every
           bet is a pure forecast. NOTHING is accepted during the window: the LMSR moves only on
           bets, so a bet placed once the window is running could buy the side the tick feed
           already shows winning at a stale price, and emissions would pay for it.
           The ingest checks this on t_recv_ms and the replay re-checks it on the receipt's
           t_recv_us; a bet within 1 ms of a boundary can be accepted by one and dropped by the
           other, and the replay decides.

PRICING — a deterministic LMSR per market
  Bets are replayed in canonical order: (window_ms, receipt t_recv_us, submitter hotkey, seq).
  Each bet spends `dollars` on one side and receives the shares that LMSR cost buys, with
  liquidity MARKETS_LMSR_B per market. Arithmetic is decimal.Decimal in a fixed context
  (precision 40, ROUND_HALF_EVEN), shares quantized DOWN to 1e-6, so every validator gets the same
  digits regardless of platform. Total trader profit in a market is bounded by b·ln 2.
  A bet past an account's per-market dollar cap is IGNORED (not priced, not scored), in order.

SCORING
  pnl per bet: shares - dollars if its side won, -dollars if it lost, 0 if VOID.
  Per account over the trailing MARKETS_SCORE_WINDOW_S (markets whose end is in the window and
  graded): it scores only past the skill gate — at least MARKETS_MIN_RESOLVED non-void bets and a
  lower confidence bound on profit-per-dollar above zero (mean - z·sd/√n, z = MARKETS_GATE_Z).
  Score = total pnl if gated and positive, else 0.
  Account "<hk>_<n>" is a subaccount of entity hotkey <hk> and scores to it; account "<hk>" is a
  direct miner. Only the SIGNER may bet for an account, so an entity cannot bet for another.
  Weights: pro-rata of score by hotkey, MINER_EMISSION_CAP, burn the rest — as Closers does.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

from . import config, hf, sessions

KIND = "mk.bet"
GRADER_VERSION = 1
SIDES = ("UP", "DOWN")
_ID_RE = re.compile(r"^UD:([A-Z0-9]+):(15m|1h):(\d+)$")
_ACCOUNT_RE = re.compile(r"^(5[1-9A-HJ-NP-Za-km-z]{46,47})(?:_(\d{1,9}))?$")
CTX = Context(prec=40, rounding=ROUND_HALF_EVEN)
SHARE_Q = Decimal("0.000001")
CENT = Decimal("0.01")


class MarketError(ValueError):
    pass


# ── market definitions ───────────────────────────────────────────────────────
def market_id(asset: str, window: str, start: int) -> str:
    return f"UD:{asset.upper()}:{window}:{int(start)}"


def parse_market_id(mid: str) -> tuple[str, str, int, int]:
    """-> (asset, window, start_unix, end_unix). Raises MarketError on a malformed id."""
    m = _ID_RE.match(str(mid or ""))
    if not m:
        raise MarketError(f"bad_market_id:{mid}")
    asset, window, start = m.group(1), m.group(2), int(m.group(3))
    secs = config.MARKETS_WINDOWS[window]
    if start % secs:
        raise MarketError(f"market_not_aligned:{mid}")
    return asset, window, start, start + secs


def market_exists(mid: str) -> bool:
    """True when the id names a real market under the rules in force at its start."""
    try:
        asset, window, start, end = parse_market_id(mid)
    except MarketError:
        return False
    if not config.markets_active_as_of(start):
        return False
    board = hf.hf_bands_as_of(start) or {}
    row = board.get(asset)
    if row is None:
        return False
    cls = row[3]
    if cls in sessions.SESSION_BOUND_CLASSES:
        secs = end - start
        if sessions.open_seconds(start, secs / 3600.0) < secs:
            return False
    return True


def entry_interval(mid: str) -> tuple[int, int]:
    """[open, close) in unix seconds: when bets on this market count."""
    _a, window, start, _end = parse_market_id(mid)
    lead = config.MARKETS_OPEN_LEAD_S or config.MARKETS_WINDOWS[window]
    return start - lead, start - config.MARKETS_ENTRY_CLOSE_LEAD_S


def markets_for(start: int, window: str) -> list[str]:
    """Every market that opens at `start` for `window` (the rule a front end lists from)."""
    board = hf.hf_bands_as_of(start) or {}
    out = [market_id(a, window, start) for a in sorted(board)]
    return [m for m in out if market_exists(m)]


# ── prices and outcomes (match sn89_markets/ladders/updown.py exactly) ───────
def round_px(x: float) -> float:
    if x <= 0:
        return x
    return round(x, 5 - int(math.floor(math.log10(abs(x)))))


def tick_mark(row: dict) -> float | None:
    b, k, p = row.get("b"), row.get("k"), row.get("p")
    if b and k and b > 0 and k > 0:
        return (b + k) / 2
    return float(p) if p and p > 0 else None


def average_marks(rows: list[dict], t_from_ms: int, t_to_ms: int,
                  window_ms: int = 180_000) -> float | None:
    """Average of marks with t in [t_from_ms, t_to_ms], ordered window by window and by (t, mark)
    inside each window — the updown.py summation order."""
    by_w: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        t = int(r["t"])
        if t < t_from_ms or t > t_to_ms:
            continue
        m = tick_mark(r)
        if m is None:
            continue
        by_w.setdefault(t // window_ms, []).append((t / 1000, float(m)))
    marks: list[float] = []
    for w in sorted(by_w):
        marks.extend(m for _, m in sorted(by_w[w]))
    return round_px(sum(marks) / len(marks)) if marks else None


def resolve(rows: list[dict], start: int, end: int) -> tuple[str, float | None, float | None]:
    """-> (outcome UP|DOWN|VOID, target, settle) from one asset's sealed tick rows."""
    avg = config.MARKETS_AVG_S
    target = average_marks(rows, (start - avg) * 1000, start * 1000)
    settle = average_marks(rows, (end - avg) * 1000, end * 1000)
    if target is None or settle is None:
        return "VOID", target, settle
    return ("UP" if settle >= target else "DOWN"), target, settle


# ── bets ─────────────────────────────────────────────────────────────────────
def parse_account(account: str) -> tuple[str, int | None]:
    m = _ACCOUNT_RE.match(str(account or ""))
    if not m:
        raise MarketError(f"bad_account:{account}")
    return m.group(1), (int(m.group(2)) if m.group(2) is not None else None)


def parse_dollars(v) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise MarketError(f"bad_dollars:{v}")
    if not d.is_finite() or d != d.quantize(CENT):
        raise MarketError(f"bad_dollars:{v}")
    if d < config.MARKETS_MIN_BET or d > config.MARKETS_MAX_BET:
        raise MarketError(f"dollars_out_of_range:{d}")
    return d


def validate_bet(payload: dict, signer_hk: str, t_unix: float) -> None:
    """Ingest-time validity. Raises hf.HFRejected so the miner gets a SIGNED refusal."""
    if not config.markets_accepting_as_of(t_unix):
        raise hf.HFRejected("markets_not_live")
    try:
        mid = str(payload.get("market_id", ""))
        asset, _w, start, end = parse_market_id(mid)
        if not market_exists(mid):
            raise MarketError(f"no_such_market:{mid}")
        if str(payload.get("trade_pair", "")).upper() != asset:
            raise MarketError(f"trade_pair_mismatch:expected {asset}")
        if str(payload.get("side", "")) not in SIDES:
            raise MarketError("bad_side")
        parse_dollars(payload.get("dollars"))
        owner, _n = parse_account(payload.get("account"))
        if owner != signer_hk:
            raise MarketError("account_not_signer")
    except MarketError as e:
        raise hf.HFRejected(str(e))
    opens, closes = entry_interval(mid)
    if t_unix < opens:
        raise hf.HFRejected("market_not_open")
    if t_unix >= closes:
        raise hf.HFRejected("market_closed")


def check_rate(prior_ts_ms: list, t_ms: int) -> None:
    """Per ACCOUNT (an entity has many). prior_ts_ms: that account's accepted bet times, ascending."""
    day = int(t_ms) // 86_400_000
    if sum(1 for x in prior_ts_ms if int(x) // 86_400_000 == day) >= config.MARKETS_DAILY_BETS:
        raise hf.HFRejected(f"daily_cap:{config.MARKETS_DAILY_BETS}")


def order_key(window_ms: int, receipt: dict, submit: dict) -> tuple:
    """Canonical replay order. t_recv_us is signed into the published receipt by the ingest,
    so any replayer sees the same value; hotkey then seq break an exact tie."""
    return (int(window_ms), int(receipt.get("t_recv_us") or 0), str(submit.get("hk", "")),
            int(submit.get("seq") or 0))


# ── LMSR ─────────────────────────────────────────────────────────────────────
def _cost(qy: Decimal, qn: Decimal, b: Decimal) -> Decimal:
    return CTX.multiply(b, CTX.ln(CTX.add(CTX.exp(CTX.divide(qy, b)), CTX.exp(CTX.divide(qn, b)))))


def price_up(qy: Decimal, qn: Decimal, b: Decimal) -> Decimal:
    ey, en = CTX.exp(CTX.divide(qy, b)), CTX.exp(CTX.divide(qn, b))
    return CTX.divide(ey, CTX.add(ey, en))


def shares_for(dollars: Decimal, side: str, qy: Decimal, qn: Decimal, b: Decimal) -> Decimal:
    """Shares of `side` that exactly `dollars` of LMSR cost buys, quantized DOWN to 1e-6.
    Closed form: e^{q_side'/b} = e^{D/b}(e^{qy/b} + e^{qn/b}) - e^{q_other/b}."""
    q_s, q_o = (qy, qn) if side == "UP" else (qn, qy)
    es = CTX.exp(CTX.divide(q_s, b))
    eo = CTX.exp(CTX.divide(q_o, b))
    target = CTX.subtract(CTX.multiply(CTX.exp(CTX.divide(dollars, b)), CTX.add(es, eo)), eo)
    new_qs = CTX.multiply(b, CTX.ln(target))
    return CTX.subtract(new_qs, q_s).quantize(SHARE_Q, rounding=ROUND_DOWN)


def replay(bets: list[dict], b: Decimal | None = None) -> list[dict]:
    """Price one market's bets in canonical order. Each bet: {key, account, side, dollars(Decimal),
    order(tuple)}. Returns the bets in order with `shares` and `status` ("ok" or "ignored:<why>")."""
    b = Decimal(b if b is not None else config.MARKETS_LMSR_B)
    qy = qn = Decimal(0)
    spent: dict[str, Decimal] = {}
    out = []
    for bet in sorted(bets, key=lambda x: x["order"]):
        d = bet["dollars"]
        acct = bet["account"]
        if spent.get(acct, Decimal(0)) + d > config.MARKETS_MAX_PER_MARKET:
            out.append({**bet, "shares": Decimal(0), "status": "ignored:per_market_cap"})
            continue
        sh = shares_for(d, bet["side"], qy, qn, b)
        if bet["side"] == "UP":
            qy += sh
        else:
            qn += sh
        spent[acct] = spent.get(acct, Decimal(0)) + d
        out.append({**bet, "shares": sh, "status": "ok"})
    return out


def bet_pnl(side: str, shares: Decimal, dollars: Decimal, outcome: str) -> Decimal:
    if outcome == "VOID":
        return Decimal(0)
    return (shares - dollars) if side == outcome else -dollars


# ── scoring ──────────────────────────────────────────────────────────────────
def account_owner(account: str) -> str:
    return parse_account(account)[0]


def score_accounts(results: list[dict]) -> dict[str, float]:
    """results: graded, non-ignored bets in the scoring window, each {account, dollars, pnl,
    outcome}. -> {owner hotkey: score >= 0}, after the per-account skill gate."""
    per: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for r in results:
        if r["outcome"] == "VOID":
            continue
        per.setdefault(r["account"], []).append((r["pnl"], r["dollars"]))
    by_owner: dict[str, float] = {}
    for acct, rows in per.items():
        n = len(rows)
        if n < config.MARKETS_MIN_RESOLVED or n < 2:
            continue
        rs = [float(p / d) for p, d in rows]
        mean = sum(rs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1))
        lb = mean - config.MARKETS_GATE_Z * sd / math.sqrt(n)
        total = float(sum(p for p, _ in rows))
        if lb <= 0 or total <= 0:
            continue
        owner = account_owner(acct)
        by_owner[owner] = by_owner.get(owner, 0.0) + total
    return by_owner


def weights_from_scores(scores: dict[str, float], uid_by_hk: dict, now: float) -> dict[int, float]:
    weights: dict[int, float] = {}
    s = {uid_by_hk[h]: v for h, v in scores.items() if h in uid_by_hk and v > 0}
    pool = sum(s.values())
    cap = config.miner_emission_cap_as_of(now)
    if pool > 0:
        for uid, v in s.items():
            weights[uid] = cap * (v / pool)
    weights[config.BURN_UID] = weights.get(config.BURN_UID, 0.0) + max(0.0, 1.0 - sum(weights.values()))
    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()}


# ── the validator path: published windows -> graded markets -> weights ──────
def _db(cache_dir: str) -> sqlite3.Connection:
    os.makedirs(cache_dir, exist_ok=True)
    c = sqlite3.connect(os.path.join(cache_dir, "markets.db"), timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS windows_seen (w INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS bets (key TEXT PRIMARY KEY, market_id TEXT, account TEXT, "
              "side TEXT, dollars TEXT, w INTEGER, t_recv_us INTEGER, hk TEXT, seq INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS bets_market ON bets(market_id)")
    c.execute("CREATE TABLE IF NOT EXISTS outcomes (market_id TEXT PRIMARY KEY, outcome TEXT, "
              "target REAL, settle REAL, end_ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, market_id TEXT, account TEXT, "
              "dollars TEXT, shares TEXT, pnl TEXT, status TEXT, outcome TEXT, end_ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = c.execute("SELECT v FROM meta WHERE k='grader_version'").fetchone()
    if (int(row[0]) if row else 0) != GRADER_VERSION:
        for t in ("windows_seen", "bets", "outcomes", "results"):
            c.execute(f"DELETE FROM {t}")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('grader_version', ?)", (str(GRADER_VERSION),))
        c.commit()
    return c


def ingest_entries(db: sqlite3.Connection, w: int, entries: list[dict]) -> int:
    """Store the window's valid mk.bet entries. Validity is re-checked here off the receipt time,
    so a bet the ingest should have refused can never be priced."""
    n = 0
    for e in entries:
        sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
        p = sub.get("payload") or {}
        if str(p.get("kind", "")) != KIND:
            continue
        hk, seq = sub.get("hk"), sub.get("seq")
        t_us = rcpt.get("t_recv_us")
        if not hk or seq is None or not t_us:
            continue
        try:
            validate_bet(p, hk, int(t_us) / 1e6)
        except hf.HFRejected:
            continue
        db.execute("INSERT OR IGNORE INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                   (f"{hk}:{seq}", str(p["market_id"]), str(p["account"]), str(p["side"]),
                    str(parse_dollars(p["dollars"])), int(w), int(t_us), hk, int(seq)))
        n += 1
    return n


def grade_market(db: sqlite3.Connection, mid: str, rows: list[dict]) -> str:
    asset, _w, start, end = parse_market_id(mid)
    outcome, target, settle = resolve(rows, start, end)
    opens, closes = entry_interval(mid)
    bets, outside = [], []
    for k, a, s, d, w, t, h, q in db.execute(
            "SELECT key, account, side, dollars, w, t_recv_us, hk, seq FROM bets WHERE market_id=?", (mid,)):
        bet = {"key": k, "account": a, "side": s, "dollars": Decimal(d), "order": (w, t, h, q)}
        # The replay decides the entry window, whatever the ingest accepted.
        (bets if opens * 1_000_000 <= int(t) < closes * 1_000_000 else outside).append(bet)
    for r in replay(bets) + [{**b, "shares": Decimal(0), "status": "ignored:outside_entry_window"}
                             for b in outside]:
        pnl = bet_pnl(r["side"], r["shares"], r["dollars"], outcome) if r["status"] == "ok" else Decimal(0)
        db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                   (r["key"], mid, r["account"], str(r["dollars"]), str(r["shares"]), str(pnl),
                    r["status"], outcome, end))
    db.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)", (mid, outcome, target, settle, end))
    return outcome


def sync_and_grade(base: str, cache_dir: str, now: float) -> None:
    """Pull mk.bet receipts from the published windows, then grade every market whose receipts
    and ticks are complete. Incremental; a from-scratch rebuild gives the same results."""
    from . import hf_grade

    db = _db(cache_dir)
    tick_dir = os.path.join(cache_dir, "ticks")
    seen = {r[0] for r in db.execute("SELECT w FROM windows_seen")}
    index = hf_grade._index(base)
    for w in index:
        if w in seen:
            continue
        txt = hf_grade._fetch_text(f"{base.rstrip('/')}/{w}/receipts.jsonl")
        if txt is None:
            continue
        entries = []
        for line in txt.splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        ingest_entries(db, w, entries)
        db.execute("INSERT OR IGNORE INTO windows_seen VALUES (?)", (w,))
    db.commit()
    published_through = (max(index) + hf_grade.WINDOW_MS) // 1000 if index else 0
    now_s = int(now)
    graded = {r[0] for r in db.execute("SELECT market_id FROM outcomes")}
    for (mid,) in db.execute("SELECT DISTINCT market_id FROM bets").fetchall():
        if mid in graded:
            continue
        asset, _w, start, end = parse_market_id(mid)
        # every bet is received before start - close lead; grade once windows through `end` are published
        if now_s < end + config.MARKETS_GRADE_SETTLE_S or published_through < end:
            continue
        rows, missing = hf_grade._ticks_for(base, tick_dir, asset,
                                            (start - config.MARKETS_AVG_S) * 1000, end * 1000)
        if missing and now_s < end + config.MARKETS_GRADE_ABANDON_S:
            continue                       # never grade a hole: wait, then void
        grade_market(db, mid, rows)
    db.commit()
    db.close()


def window_results(cache_dir: str, now: float) -> list[dict]:
    db = _db(cache_dir)
    lo = int(now) - config.MARKETS_SCORE_WINDOW_S
    out = [{"account": a, "dollars": Decimal(d), "pnl": Decimal(p), "outcome": o}
           for a, d, p, o, s in db.execute(
               "SELECT account, dollars, pnl, outcome, status FROM results WHERE end_ts > ? AND end_ts <= ?",
               (lo, int(now))) if s == "ok"]
    db.close()
    return out


def markets_tallies(now: float | None = None, base: str | None = None,
                    cache_dir: str | None = None) -> dict[str, float]:
    now = time.time() if now is None else now
    base = base or hf.HF_PUBLIC_BASE
    cache_dir = cache_dir or os.path.expanduser(os.getenv("SN89_MARKETS_GRADE_CACHE", "~/.sn89/markets-grade"))
    sync_and_grade(base, cache_dir, now)
    return score_accounts(window_results(cache_dir, now))


def markets_weights(uid_by_hk: dict, now: float | None = None, base: str | None = None,
                    cache_dir: str | None = None) -> dict[int, float]:
    """{uid: weight} for the markets competition — the analogue of closers.closers_weights."""
    now = time.time() if now is None else now
    return weights_from_scores(markets_tallies(now, base, cache_dir), uid_by_hk, now)
