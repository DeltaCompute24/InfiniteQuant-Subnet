"""SN89 Agent competition v2: agent BOOKS scored as weekly high-water-mark payouts.

STAGED, NOT LIVE. Nothing in the validator, the miner, the multiplexer or competitions.py
imports this module. It is a pure replay:

    (target receipts, ticks, rules_as_of) -> per-book equity series -> weekly payouts

Deterministic: no network, no wall clock, no globals mutated, stdlib only. Every input is
an argument, so two validators fed the same receipts and the same anchored ticks compute
the same numbers.

THE BOOK
--------
A hosted algo emits a receipt `{account, seq, t_recv_ms, targets: {PAIR: signed_lev}}`.
A pair omitted from `targets` HOLDS (its quantity is untouched, so its leverage drifts with
price, as a venue position would). A target of 0 closes the pair. The book starts at equity
1.0 and every money figure below is in units of that starting equity.

ACCEPTANCE (at t_recv_ms, before any price is known)
  * every pair must be in the caller-supplied universe and not in an excluded class (FX);
  * every |target| <= per_pair_max_lev (0.25), else the WHOLE receipt is refused;
  * gross Σ|declared target| after merging the receipt into the book's declared targets
    <= gross_max_lev (1.0), else the WHOLE receipt is refused;
  * at most max_changes_per_day (48) accepted receipts that change at least one declared
    target per UTC day; a receipt over the cap is refused whole. A receipt that changes
    nothing is a no-op: neither refused nor counted.
  Refusing is chosen over clamping or scaling. A scaled vector is a position the algo
  never asked for, and its author cannot tell from its own fills that it was resized; a
  refusal is loud, leaves the previous book intact, and is the same rule a venue applies
  (PTN refuses an order over its leverage cap, it does not trim it).

  Gross is measured on DECLARED targets (the last accepted target per pair), not on the
  drifted mark-to-market leverage. That makes acceptance a function of the receipt stream
  alone, so it can be decided at ingest before any tick exists. Drift can take realised
  gross slightly above the cap between rebalances; the cap is on what the algo asks for.

FILL
  A pair's pending target fills at the FIRST tick of that pair with t >= t_recv_ms + 3000.
  The book is resized to `target × equity / mid` (equity marked at the latest mids of every
  pair at that tick), buying at the ask (`k`) and selling at the bid (`b`). Published SN89
  tick shards carry `b`, `k` and `p` on every row (Hyperliquid for crypto/metals/equities,
  Polygon quotes for FX), so the bid/ask path is the normal one. A tick without a usable
  quote (missing, non-positive, or crossed) falls back to `p ± half_spread_bps[pair]`.
  If two accepted receipts for one pair are both eligible at the same tick, the later one
  wins and the pair fills once.

FEES AND CARRY
  fee = fee_bps[class] / 1e4 × |Δqty| × fill_price, charged at the fill (= |Δlev| × equity).
  Crypto is 2.9 bps per leg (PTN). Other classes default to the crypto rate until pinned
  from PTN source (FEE_TODO). Carry is not modelled in v1; `carry_fn` is the hook.

EQUITY
  balance = 1 + realised P&L − fees − carry; unrealised = Σ qty × (mid − avg_entry);
  equity = balance + unrealised. Spread is paid through the fill price, so it lands in
  realised P&L on exit and in unrealised (marked at mid) while open.

ELIMINATION
  At every UTC day end, if EOD equity < (1 − 0.08) × max(1.0, all prior EOD equities) the
  book is eliminated at that instant: everything closes at the day-end mid (fee charged),
  no further fills, and no payout for the week in which it happened or any later week.
  Exactly an 8% drawdown survives (the rule is strict `<`).

ACTIVITY
  A week (Monday 00:00 UTC to the next Monday) is ACTIVE when the book had at least one
  FILL in >= 4 distinct 12-hour buckets (00:00–12:00, 12:00–24:00 UTC; 14 per week). A
  refused or no-op receipt is not a fill. A tick at exactly 12:00:00.000 is in the second
  bucket.

WEEKLY PAYOUT (SN8 weekly-HWM)
  week_gain = max(0, eow_X − prev_hwm + min(0, eow_unrealised)),  hwm = max(prev_hwm, eow_X)
  paid = week_gain if active else 0. HWM starts at 1.0 and updates on EVERY week, active or
  not, so sitting out a losing week cannot preserve an old high.
  X is `hwm_basis`: "balance" (default) or "equity". SN8 runs this formula on the BALANCE
  (realised) — see ~/IQ/SN8-WEEKLY-HWM-PAYOUT.md — which is why the min(0, unrealised) term
  exists: it charges open losses against realised gains. On EQUITY the open loss would be
  counted twice (it is already inside equity) and an open gain would be paid and lift the
  HWM before it is ever realised.

POOL
  weekly_weights() pays each ACCOUNT its paid gain / Σ paid over every account of every
  eligible entity. An entity's weight is the sum of its accounts' shares; one account's
  loss never nets against another's gain (the gain floor is per account, like SN8 entity
  subaccounts). Σ paid == 0 -> {} and the share burns. An entity whose bond is below
  ENTITY_MIN_ALPHA at the weekly snapshot is paid nothing that week.
"""
from __future__ import annotations

import hashlib
import math
from types import MappingProxyType

from . import hf

# ── rules (CONSENSUS) ────────────────────────────────────────────────────────
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
BUCKET_MS = 12 * 3_600_000
# 1970-01-01 was a Thursday; the first Monday 00:00 UTC is 4 days later.
_MONDAY_OFFSET_MS = 4 * DAY_MS

CRYPTO_FEE_BPS = 2.9
# TODO pin from PTN source: per-class fee for equities / indices / commodities / metals.
FEE_TODO = CRYPTO_FEE_BPS

ENTITY_MIN_ALPHA = 10_000.0
ENTITY_COMMIT_PREFIX = "sn89entity:"

RULES_V1 = MappingProxyType({
    "v": 1,
    "fill_delay_ms": 3000,
    "per_pair_max_lev": 0.25,
    "gross_max_lev": 1.0,
    "max_changes_per_day": 48,
    "excluded_classes": frozenset({"forex"}),
    "fee_bps": MappingProxyType({"crypto": CRYPTO_FEE_BPS}),
    "fee_bps_default": FEE_TODO,
    "half_spread_bps_default": 0.0,
    "elim_dd": 0.08,
    "elim_eps": 1e-12,          # float guard so an exact 8.00% lands on the survive side
    "active_min_buckets": 4,
    "hwm_basis": "balance",
    "entity_min_alpha": ENTITY_MIN_ALPHA,
})

BOOK_RULES_HISTORY: tuple = ((0, RULES_V1),)


def book_rules_as_of(t_unix: float):
    """The rule set in force at t (same as-of shape as the band and share histories)."""
    out = BOOK_RULES_HISTORY[0][1]
    for eff, rules in BOOK_RULES_HISTORY:
        if t_unix >= eff:
            out = rules
    return out


# ── time helpers ─────────────────────────────────────────────────────────────
def week_start_ms(t_ms: int) -> int:
    """Monday 00:00 UTC at or before t."""
    return ((int(t_ms) - _MONDAY_OFFSET_MS) // WEEK_MS) * WEEK_MS + _MONDAY_OFFSET_MS


def day_start_ms(t_ms: int) -> int:
    return (int(t_ms) // DAY_MS) * DAY_MS


def bucket_of(t_ms: int) -> int:
    return int(t_ms) // BUCKET_MS


# ── receipts ─────────────────────────────────────────────────────────────────
AGENT_RECEIPT_DOMAIN = b"SN89-AGENT-RCPT-v1|"


def receipt_signing_bytes(receipt: dict) -> bytes:
    return (AGENT_RECEIPT_DOMAIN
            + f"{receipt['account']}|{int(receipt['seq'])}|{int(receipt['t_recv_ms'])}|".encode()
            + hf.canonical_json(receipt.get("targets") or {}).encode())


def receipt_order_key(r: dict):
    return (int(r["t_recv_ms"]), str(r["account"]), int(r["seq"]))


def verify_receipt(receipt: dict, verify_fn) -> bool:
    """`verify_fn(message: bytes, signature_hex: str) -> bool` checks the entity hotkey's
    signature (injected, so this module stays free of chain/crypto deps)."""
    sig = receipt.get("sig")
    if not sig:
        return False
    try:
        return bool(verify_fn(receipt_signing_bytes(receipt), str(sig)))
    except Exception:  # noqa: BLE001 — a verifier that raises is a failed verification
        return False


def receipts_root(receipts: list) -> str:
    """Merkle root over the receipts' signing bytes in (t_recv_ms, account, seq) order; the
    bytes themselves break a tie (two different receipts under one key), so the root never
    depends on input order."""
    leaves = sorted((receipt_order_key(r), receipt_signing_bytes(r)) for r in receipts)
    return hf.merkle_root([hf.leaf(b) for _k, b in leaves])


# ── entities ─────────────────────────────────────────────────────────────────
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def _b58decode(s: str) -> bytes | None:
    n = 0
    for c in s:
        i = _B58_IDX.get(c)
        if i is None:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_ss58(addr: str, prefix: int = 42) -> bool:
    """Strict generic-substrate SS58 check: base58, 35 bytes (1 prefix + 32 key + 2
    checksum), the expected prefix, and the blake2b-512 'SS58PRE' checksum."""
    if not isinstance(addr, str) or not 47 <= len(addr) <= 48:
        return False
    raw = _b58decode(addr)
    if raw is None or len(raw) != 35 or raw[0] != prefix:
        return False
    chk = hashlib.blake2b(b"SS58PRE" + raw[:33], digest_size=64).digest()[:2]
    return chk == raw[33:]


def parse_entity_commit(s) -> str | None:
    """`sn89entity:<bond_coldkey_ss58>` -> the coldkey, or None if malformed."""
    if not isinstance(s, str) or not s.startswith(ENTITY_COMMIT_PREFIX):
        return None
    ck = s[len(ENTITY_COMMIT_PREFIX):]
    return ck if is_ss58(ck) else None


def entity_eligible(bond_alpha, rules=None) -> bool:
    rules = rules or RULES_V1
    try:
        a = float(bond_alpha)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and a >= float(rules["entity_min_alpha"])


# ── replay ───────────────────────────────────────────────────────────────────
def _sorted_ticks(ticks) -> list:
    return sorted(ticks, key=hf.tick_order_key)


def _quote(tick: dict, half_spread_bps: float):
    """(bid, ask, mid) for a tick. Real quote when it is usable, else mid ± half-spread."""
    p = float(tick["p"])
    b, k = tick.get("b"), tick.get("k")
    if b is not None and k is not None:
        b, k = float(b), float(k)
        if b > 0 and k > 0 and b <= k:
            return b, k, p
    h = p * float(half_spread_bps) / 1e4
    return p - h, p + h, p


class _Book:
    __slots__ = ("qty", "avg", "realised", "fees", "carry", "marks")

    def __init__(self):
        self.qty: dict = {}
        self.avg: dict = {}
        self.realised = 0.0
        self.fees = 0.0
        self.carry = 0.0
        self.marks: dict = {}

    def balance(self) -> float:
        return 1.0 + self.realised - self.fees - self.carry

    def unrealised(self) -> float:
        u = 0.0
        for pair in sorted(self.qty):
            q = self.qty[pair]
            if q:
                u += q * (self.marks[pair] - self.avg[pair])
        return u

    def equity(self) -> float:
        return self.balance() + self.unrealised()

    def trade(self, pair: str, dq: float, px: float, fee_rate: float) -> None:
        if dq == 0:
            return
        q = self.qty.get(pair, 0.0)
        a = self.avg.get(pair, 0.0)
        self.fees += fee_rate * abs(dq) * px
        if q == 0 or (q > 0) == (dq > 0):
            nq = q + dq
            self.avg[pair] = (q * a + dq * px) / nq
            self.qty[pair] = nq
            return
        close = min(abs(dq), abs(q))
        sign = 1.0 if q > 0 else -1.0
        self.realised += close * sign * (px - a)
        rest = abs(dq) - close
        if rest > 0:                                  # flip: remainder opens at px
            self.qty[pair] = -sign * rest
            self.avg[pair] = px
        else:
            nq = q + dq
            if abs(close - abs(q)) == 0 or nq == 0:
                self.qty[pair] = 0.0
                self.avg[pair] = 0.0
            else:
                self.qty[pair] = nq


def _check_receipt(r: dict, declared: dict, universe: dict, rules) -> str | None:
    targets = r.get("targets")
    if not isinstance(targets, dict):
        return "bad_targets"
    excl = rules["excluded_classes"]
    cap = float(rules["per_pair_max_lev"])
    merged = dict(declared)
    for pair, lev in targets.items():
        cls = universe.get(pair)
        if cls is None:
            return f"unknown_pair:{pair}"
        if cls in excl:
            return f"excluded_class:{pair}"
        if isinstance(lev, bool) or not isinstance(lev, (int, float)):
            return f"bad_lev:{pair}"
        lev = float(lev)
        if not math.isfinite(lev):
            return f"bad_lev:{pair}"
        if abs(lev) > cap:
            return f"pair_cap:{pair}"
        merged[pair] = lev
    if sum(abs(v) for v in merged.values()) > float(rules["gross_max_lev"]) + 1e-12:
        return "gross_cap"
    return None


def replay_book(account: str, receipts: list, ticks: list, universe: dict, *,
                start_ms: int, end_ms: int, rules=None, half_spread_bps: dict | None = None,
                carry_fn=None, ticks_sorted: bool = False) -> dict:
    """Replay one book over [start_ms, end_ms].

    receipts: this account's receipts (others are ignored).
    ticks: tick dicts {a, t, p[, b, k]} for any pairs, any order (pass ticks_sorted=True to
           skip the sort when the caller already ordered them by hf.tick_order_key).
    universe: {PAIR: asset_class}.
    carry_fn(pair, signed_notional, day_end_ms) -> cost in equity units, called at each
           day end for each open pair (v1: None, carry not modelled).
    Returns a dict with fills, refusals, no-ops, daily EOD rows, weekly rows, the equity
    series (one point per fill and per day end) and eliminated_at_ms.
    """
    rules = rules or book_rules_as_of(start_ms / 1000)
    hs = half_spread_bps or {}
    hs_default = float(rules["half_spread_bps_default"])
    fee_tbl = rules["fee_bps"]
    fee_default = float(rules["fee_bps_default"])
    delay = int(rules["fill_delay_ms"])
    max_changes = int(rules["max_changes_per_day"])
    elim_mult = 1.0 - float(rules["elim_dd"])
    eps = float(rules["elim_eps"])
    basis = rules["hwm_basis"]
    if basis not in ("balance", "equity"):
        raise ValueError(f"hwm_basis {basis!r}")

    rs = sorted((r for r in receipts if r.get("account") == account), key=receipt_order_key)
    tk = ticks if ticks_sorted else _sorted_ticks(ticks)

    book = _Book()
    declared: dict = {}
    pending: dict = {}           # pair -> list[(eligible_ms, target, seq)]
    changes_by_day: dict = {}
    last_seq = None
    fills, refused, noops, daily, weeks, series = [], [], [], [], [], []
    ath = 1.0
    hwm = 1.0
    week_fill_buckets: set = set()
    eliminated_at = None
    elim_week = None

    def fee_rate(pair):
        return float(fee_tbl.get(universe.get(pair), fee_default)) / 1e4

    def close_all(t_ms):
        for pair in sorted(book.qty):
            q = book.qty[pair]
            if q:
                book.trade(pair, -q, book.marks[pair], fee_rate(pair))
        pending.clear()

    def boundary(b_ms):
        nonlocal ath, hwm, eliminated_at, elim_week, week_fill_buckets
        if eliminated_at is None:
            if carry_fn is not None:
                for pair in sorted(book.qty):
                    q = book.qty[pair]
                    if q:
                        book.carry += float(carry_fn(pair, q * book.marks[pair], b_ms))
            eod = book.equity()
            elim = eod < ath * elim_mult - eps
            daily.append({"t_ms": b_ms, "eod_equity": eod, "ath": ath, "eliminated": elim})
            series.append((b_ms, eod))
            if elim:
                close_all(b_ms)
                eliminated_at = b_ms
                elim_week = week_start_ms(b_ms - 1)
            else:
                ath = max(ath, eod)
        if (b_ms - _MONDAY_OFFSET_MS) % WEEK_MS == 0:
            ws = b_ms - WEEK_MS
            bal = book.balance()
            unr = book.unrealised()
            x = bal + unr if basis == "equity" else bal
            gain = max(0.0, x - hwm + min(0.0, unr))
            active = len(week_fill_buckets) >= int(rules["active_min_buckets"])
            dead = eliminated_at is not None and (elim_week is None or ws >= elim_week)
            paid = gain if (active and not dead) else 0.0
            weeks.append({"week_start_ms": ws, "eow_balance": bal, "eow_unrealised": unr,
                          "eow_equity": bal + unr, "prev_hwm": hwm, "gain": gain,
                          "active": active, "buckets": len(week_fill_buckets),
                          "eliminated": dead, "partial": ws < start_ms, "paid": paid})
            hwm = max(hwm, x)
            week_fill_buckets = set()

    def accept(r):
        nonlocal last_seq
        if eliminated_at is not None:
            refused.append((r, "eliminated"))
            return
        seq = int(r["seq"])
        if last_seq is not None and seq <= last_seq:
            refused.append((r, "stale_seq"))
            return
        reason = _check_receipt(r, declared, universe, rules)
        if reason:
            refused.append((r, reason))
            return
        changed = {p: float(v) for p, v in r["targets"].items()
                   if float(v) != declared.get(p, 0.0)}
        last_seq = seq
        if not changed:
            noops.append(r)
            return
        day = day_start_ms(r["t_recv_ms"])
        if changes_by_day.get(day, 0) >= max_changes:
            refused.append((r, "day_cap"))
            return
        changes_by_day[day] = changes_by_day.get(day, 0) + 1
        for p, v in changed.items():
            if v == 0.0:
                declared.pop(p, None)
            else:
                declared[p] = v
            pending.setdefault(p, []).append((int(r["t_recv_ms"]) + delay, v, seq))

    def on_tick(t):
        pair = t["a"]
        if pair not in universe:
            return
        tms = int(t["t"])
        bid, ask, mid = _quote(t, hs.get(pair, hs_default))
        book.marks[pair] = mid
        if eliminated_at is not None:
            return
        q = pending.get(pair)
        if not q or q[0][0] > tms:
            return
        target = None
        while q and q[0][0] <= tms:
            target = q.pop(0)[1]
        if not q:
            pending.pop(pair, None)
        eq = book.equity()
        cur = book.qty.get(pair, 0.0)
        desired = 0.0 if target == 0.0 else target * eq / mid
        dq = desired - cur
        if dq == 0:
            return
        px = ask if dq > 0 else bid
        book.trade(pair, dq, px, fee_rate(pair))
        if target == 0.0:
            book.qty[pair] = 0.0
        week_fill_buckets.add(bucket_of(tms))
        e_after = book.equity()
        fills.append({"t_ms": tms, "pair": pair, "dq": dq, "px": px, "target": target,
                      "equity": e_after})
        series.append((tms, e_after))

    next_b = day_start_ms(start_ms) + DAY_MS
    ri = 0
    for t in tk:
        tms = int(t["t"])
        if tms > end_ms:
            break
        while next_b <= tms and next_b <= end_ms:
            while ri < len(rs) and int(rs[ri]["t_recv_ms"]) < next_b:
                accept(rs[ri])
                ri += 1
            boundary(next_b)
            next_b += DAY_MS
        while ri < len(rs) and int(rs[ri]["t_recv_ms"]) <= tms:
            accept(rs[ri])
            ri += 1
        if tms >= start_ms:
            on_tick(t)
        elif t["a"] in universe:
            book.marks[t["a"]] = _quote(t, hs.get(t["a"], hs_default))[2]
    while next_b <= end_ms:
        while ri < len(rs) and int(rs[ri]["t_recv_ms"]) < next_b:
            accept(rs[ri])
            ri += 1
        boundary(next_b)
        next_b += DAY_MS
    while ri < len(rs) and int(rs[ri]["t_recv_ms"]) <= end_ms:
        accept(rs[ri])
        ri += 1

    positions = {p: {"qty": book.qty[p], "avg": book.avg[p], "mark": book.marks[p]}
                 for p in sorted(book.qty) if book.qty[p]}
    return {"account": account, "fills": fills, "refused": refused, "noops": noops,
            "daily": daily, "weeks": weeks, "series": series,
            "eliminated_at_ms": eliminated_at, "hwm": hwm,
            "equity": book.equity(), "balance": book.balance(),
            "unrealised": book.unrealised(), "ath": ath, "positions": positions,
            "declared": dict(sorted(declared.items())),
            "open_week_buckets": len(week_fill_buckets)}


def replay_books(receipts: list, ticks: list, universe: dict, *, start_ms: int,
                 end_ms: int, rules=None, half_spread_bps=None, carry_fn=None) -> dict:
    """Every account present in `receipts`, against one shared (sorted once) tick series."""
    tk = _sorted_ticks(ticks)
    accounts = sorted({str(r["account"]) for r in receipts})
    return {a: replay_book(a, receipts, tk, universe, start_ms=start_ms, end_ms=end_ms,
                           rules=rules, half_spread_bps=half_spread_bps, carry_fn=carry_fn,
                           ticks_sorted=True)
            for a in accounts}


def paid_for_week(result: dict, week_start: int) -> float:
    for w in result["weeks"]:
        if w["week_start_ms"] == week_start:
            return w["paid"]
    return 0.0


# ── pool split ───────────────────────────────────────────────────────────────
def weekly_weights(books_by_entity: dict, entity_bond_alpha: dict | None,
                   rules=None) -> dict:
    """{entity: {account: paid}} -> {account: share of the pool}.

    `entity_bond_alpha` = {entity: alpha bonded on netuid 89 at the weekly snapshot};
    an entity missing from it or below ENTITY_MIN_ALPHA is paid 0. Pass None only to
    skip the bond gate (tests, simulation). Each account's paid gain is floored at 0 on
    its own, so a losing account never nets against a sibling. Σ paid == 0 -> {}.
    """
    rules = rules or RULES_V1
    paid: dict = {}
    for ent in sorted(books_by_entity):
        if entity_bond_alpha is not None and not entity_eligible(
                entity_bond_alpha.get(ent, 0.0), rules):
            continue
        for acct in sorted(books_by_entity[ent]):
            v = float(books_by_entity[ent][acct])
            if math.isfinite(v) and v > 0:
                paid[acct] = paid.get(acct, 0.0) + v
    total = sum(paid[a] for a in sorted(paid))
    if total <= 0:
        return {}
    return {a: paid[a] / total for a in sorted(paid)}


def entity_weights(books_by_entity: dict, shares: dict) -> dict:
    """Σ of each entity's accounts' shares."""
    out: dict = {}
    for ent in sorted(books_by_entity):
        s = sum(shares.get(a, 0.0) for a in sorted(books_by_entity[ent]))
        if s > 0:
            out[ent] = s
    return out
