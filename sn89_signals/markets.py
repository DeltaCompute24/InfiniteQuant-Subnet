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

V2 — COLLATERAL + P&L-BASIS EMISSION PER WEIGHT CYCLE (markets STARTING at/after
config.MARKETS_COLLATERAL_FROM_UNIX; Whit 2026-10-08, modelled on Vanta's P&L payouts)
  limits   $1–$10,000 per bet, $25,000 per account per market; liquidity-sensitive LMSR, each bet
           priced with b = B0 + K x dollars already bet in that market.
  rate     dollars -> alpha for a market: the subnet pool price (TAO per alpha) at the first block
           of the market's start UTC day x the average TAOUSD mark of the minute before 00:00 from
           the sealed ticks. Missing -> the previous day's rate (up to RATE_FALLBACK_DAYS).
  collateral  an entity's collateral at hour h = the alpha stake its OWNER COLDKEY holds on the
           entity hotkey (netuid = config.NETUID) at the first block of h, minus its unburned
           losses as of the last closed cycle. Bets of all its subaccounts are taken in canonical
           order; a bet is IGNORED ("ignored:collateral") when the alpha stake of the entity's open
           bets (markets not yet ended at that receipt) plus this one would exceed it.
  losses   a losing bet's stake is owed as a burn. The entity burns it on chain (burn_alpha or
           recycle_alpha on its own hotkey) and files a signed `mk.burn` claim {amount_alpha,
           block, ext_index}; validators read that extrinsic's AlphaBurned/AlphaRecycled event and
           credit it (one credit per extrinsic).
  cycles   settlement runs on a fixed grid of SETTLE_PERIOD_S (default one tempo, 4,320 s)
           anchored at the arm. A cycle closes at its end + SETTLE_GRACE_S once its markets are
           graded. Per entity: W = winnings (payout - stake) of its winning bets in markets ending
           in the cycle, in alpha; L = stakes of its losing bets; B = verified burns claimed in
           [c + grace, c_end + grace). Losses still unburned BURN_DEADLINE_S after their cycle
           ended are taken out of W (withheld emission burns like burned alpha, so not burning
           never pays); payable = W - that deduction. Burns ahead of losses are carried as credit.
  weights  for the latest closed cycle: entity weight in the Markets vector = payable / the
           cycle's Markets emission (blocks in the cycle x alpha_out per block x MINER_FRACTION x
           mecid-0 split x the Markets share), pro rata when they sum past 1; the rest burns. Every
           weight commit during the next cycle carries it. Skill scoring of pre-V2 markets stops
           when V2 arms.

V3 — LIVE IN-WINDOW PRICING (markets STARTING at/after config.MARKETS_V3_FROM_UNIX; Whit
2026-10-09: "IQ Markets must work like Kalshi's 15-min markets")
  entry    two intervals: the pre-start one above, unchanged, and IN-WINDOW
           [start + V3_OPEN_DELAY_S, end - V3_LATE_CUTOFF_S[window]). The gap [start - 60 s,
           start + delay) stays closed: the target is forming.
  fill     the FIRST sealed tick for the asset stamped at or after the bet's RECEIPT time
           (t_recv_us, signed into the receipt by the ingest — never the miner's own timestamp)
           plus V3_FILL_DELAY_S. The bettor commits before the price he is filled at exists, so a
           feed that leads ours by less than the delay carries no edge (the replay in the spec:
           filled at the tick AT receipt, a 1 s lead took ~10% per bet; at the next print after
           3 s, nothing). No tick within V3_MAX_TICK_AGE_S of that instant (stale feed) IGNORES
           the bet ("ignored:no_fill", refunded); a bet is never priced blind.
  model    p_model = P(settle avg >= target | fill) under a driftless lognormal:
           p = Phi( ln(fill/target)/(s) - s/2 ), s = sigma x sqrt(tau), tau = end - t_fill - 30 s (the
           settle average sits in the last minute), Phi = Abramowitz-Stegun 26.2.17 evaluated in
           Decimal (the approximation IS the definition, so every machine gets the same digits),
           quantized to 1e-6 and clamped to [P_FLOOR, 1 - P_FLOOR].
  sigma    per asset per UTC day: the RMS one-minute log return of the PREVIOUS day's minute-average
           marks (consecutive minutes only), as a per-second value; under
           V3_SIGMA_MIN_RETURNS returns -> the class fallback. Computed once per (asset, day) from
           the sealed windows and cached (v3_sigma).
  crowd    the LMSR keeps running; its UP quantity is shifted by an anchor
           a = b x ln(p_model / (1 - p_model)) at each bet, so with no net bets the quoted price IS
           p_model and every bet still moves it (price_up(qy + a, qn, b)). Pre-start bets keep
           anchor 0 (50/50 start), exactly as before.
  spread   an in-window buyer pays min(p_avg + spread(tau), V3_PRICE_CAP) per share, p_avg being
           the LMSR average price of the bet and spread(tau) = V3_SPREAD + V3_SPREAD_K x
           sqrt(V3_LEAD_S / tau) (config): shares issued = dollars / that; the LMSR state moves by
           the UNSPREAD shares (the full nudge). Pre-start bets pay no spread.
  exposure what a sniper can take: with a price lead of D seconds he sees p_true while we quote
           p_model(spot at t - D) + spread; his edge per share is p_true - p_model - spread when
           positive, zero otherwise. The late cutoff bounds the time-left where a small lead is worth
           a lot; the spread bounds it everywhere else. Sized in the 2026-10-09 replay
           (SN89-PREDICTION-MARKETS-SPEC.md "V3 live pricing").
  payout   unchanged: a share pays $1 if its side wins. Fees and collateral unchanged.

V2b — RETENTION + NET-P&L EMISSION (markets STARTING at/after config.MARKETS_RETAIN_FROM_UNIX;
Whit 2026-10-10)
  losses   a lost stake STAYS with the entity: it is not owed as a burn and is not debt against its
           collateral. Burn claims (mk.burn) stay valid and are credited only against losses of
           pre-stamp markets; once those are settled a claim changes nothing.
  emission per cycle and entity: net = winnings paid (payout - stake) - stakes lost, over every
           account of the entity, in alpha at the market's day rate, for markets starting at/after
           the stamp that ended in the cycle. payable = max(0, net), plus whatever the pre-stamp
           rule still owes for pre-stamp markets. A wash pair inside one entity nets to zero. The
           weight is payable / the cycle's Markets emission, pro rata past 1, as in V2.
  collateral unchanged: open bets must fit inside the entity's alpha stake at the hour (the debt
           term can only come from pre-stamp losses).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

from . import config, hf, oracle, sessions

KIND = "mk.bet"
KIND_BURN = "mk.burn"
MARKETS_KINDS = (KIND, KIND_BURN)
GRADER_VERSION = 2
SIDES = ("UP", "DOWN")
_ID_RE = re.compile(r"^UD:([A-Z0-9]+):(15m|1h):(\d+)$")
_ACCOUNT_RE = re.compile(r"^(5[1-9A-HJ-NP-Za-km-z]{46,47})(?:_(\d{1,9}))?$")
CTX = Context(prec=40, rounding=ROUND_HALF_EVEN)
SHARE_Q = Decimal("0.000001")
CENT = Decimal("0.01")
RAO = Decimal("0.000000001")


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
    """[open, close) in unix seconds of the PRE-START interval: when bets on this market count
    before it starts. A V3 market has a second, in-window interval: see entry_intervals."""
    _a, window, start, _end = parse_market_id(mid)
    lead = config.MARKETS_OPEN_LEAD_S or config.MARKETS_WINDOWS[window]
    return start - lead, start - config.MARKETS_ENTRY_CLOSE_LEAD_S


def is_v3(start: int) -> bool:
    return config.markets_v3_as_of(start)


def late_cutoff_s(window: str) -> int:
    return int(config.MARKETS_V3_LATE_CUTOFF_S.get(window, config.MARKETS_WINDOWS[window] // 5))


def entry_intervals(mid: str) -> list[tuple[int, int]]:
    """Every [open, close) in unix seconds in which a bet on this market counts: the pre-start
    interval, plus the in-window one for a V3 market."""
    _a, window, start, end = parse_market_id(mid)
    out = [entry_interval(mid)]
    if is_v3(start):
        out.append((start + config.MARKETS_V3_OPEN_DELAY_S, end - late_cutoff_s(window)))
    return out


def in_entry(mid: str, t_unix: float) -> bool:
    return any(o <= t_unix < c for o, c in entry_intervals(mid))


def in_entry_us(mid: str, t_us: int) -> bool:
    return any(o * 1_000_000 <= int(t_us) < c * 1_000_000 for o, c in entry_intervals(mid))


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


def uses_oracle(mid: str) -> bool:
    """V4: this market is priced, filled and settled on the Hyperliquid oracle record (oracle.py),
    not the HF tick mid. Decided by its asset and start alone (config.markets_oracle_coin)."""
    asset, _w, start, _end = parse_market_id(mid)
    return config.markets_oracle_coin(asset, start) is not None


def market_target(mid: str, rows: list[dict]) -> float | None:
    """The market's starting price from `rows` of its own series (oracle or ticks)."""
    _a, _w, start, _e = parse_market_id(mid)
    avg = config.MARKETS_AVG_S
    if uses_oracle(mid):
        v = oracle.twap(rows, (start - avg) * 1000, start * 1000)
        return float(v) if v is not None else None
    return average_marks(rows, (start - avg) * 1000, start * 1000)


def market_spot(mid: str, rows: list[dict], t_ms: int) -> tuple[int, float] | None:
    """(t, price) a front end shows as the price at t_ms, from the market's own series."""
    if uses_oracle(mid):
        v = oracle.value_at(rows, t_ms)
        return (v[0], float(v[1])) if v is not None else None
    return latest_mark(rows, t_ms, config.MARKETS_V3_MAX_TICK_AGE_S * 1000)


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


def is_v2(start: int) -> bool:
    return config.markets_collateral_as_of(start)


def is_retained(start: int) -> bool:
    """V2b: a market whose lost stakes stay with the entity and score on net entity P&L."""
    return config.markets_retain_as_of(start)


def limits_for(start: int | None) -> tuple[Decimal, Decimal, Decimal]:
    """(min bet, max bet, per-account per-market cap) for a market starting at `start`."""
    if start is not None and is_v2(start):
        return config.MARKETS_V2_MIN_BET, config.MARKETS_V2_MAX_BET, config.MARKETS_V2_MAX_PER_MARKET
    return config.MARKETS_MIN_BET, config.MARKETS_MAX_BET, config.MARKETS_MAX_PER_MARKET


def lmsr_b(start: int | None, volume: Decimal) -> Decimal:
    """Liquidity a bet is priced with, given the dollars already bet in the market before it."""
    if start is not None and is_v2(start):
        return CTX.add(config.MARKETS_V2_LMSR_B0, CTX.multiply(config.MARKETS_V2_LMSR_K, volume))
    return Decimal(config.MARKETS_LMSR_B)


def parse_dollars(v, start: int | None = None) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise MarketError(f"bad_dollars:{v}")
    if not d.is_finite() or d != d.quantize(CENT):
        raise MarketError(f"bad_dollars:{v}")
    lo, hi, _cap = limits_for(start)
    if d < lo or d > hi:
        raise MarketError(f"dollars_out_of_range:{d}")
    return d


def parse_alpha(v) -> Decimal:
    """A burn claim's alpha amount: positive, at most 9 decimals (rao)."""
    try:
        a = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise MarketError(f"bad_amount:{v}")
    if not a.is_finite() or a <= 0 or a != a.quantize(Decimal("0.000000001")):
        raise MarketError(f"bad_amount:{v}")
    return a


def validate_burn(payload: dict, signer_hk: str, t_unix: float) -> None:
    """An entity's claim that it burned alpha on chain (V2). The claim only says WHERE to look;
    validators credit it only after reading the extrinsic's event (verify_burn)."""
    if not config.MARKETS_COLLATERAL_FROM_UNIX or t_unix < config.MARKETS_COLLATERAL_FROM_UNIX:
        raise hf.HFRejected("markets_collateral_not_live")
    try:
        parse_alpha(payload.get("amount_alpha"))
        blk, idx = int(payload.get("block")), int(payload.get("ext_index"))
        if blk <= 0 or idx < 0:
            raise MarketError("bad_extrinsic_ref")
        if str(payload.get("trade_pair", "")).upper() != config.MARKETS_RATE_PAIR:
            raise MarketError(f"trade_pair_mismatch:expected {config.MARKETS_RATE_PAIR}")
    except (TypeError, ValueError) as e:
        raise hf.HFRejected(str(e) if isinstance(e, MarketError) else "bad_extrinsic_ref")


def validate_entry(payload: dict, signer_hk: str, t_unix: float) -> None:
    """Ingest dispatch for every Markets kind."""
    if str(payload.get("kind", "")) == KIND_BURN:
        validate_burn(payload, signer_hk, t_unix)
    else:
        validate_bet(payload, signer_hk, t_unix)


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
        parse_dollars(payload.get("dollars"), start)
        owner, _n = parse_account(payload.get("account"))
        if owner != signer_hk:
            raise MarketError("account_not_signer")
    except MarketError as e:
        raise hf.HFRejected(str(e))
    if in_entry(mid, t_unix):
        return
    ivs = entry_intervals(mid)
    if t_unix < ivs[0][0]:
        raise hf.HFRejected("market_not_open")
    if len(ivs) > 1 and t_unix < ivs[1][0]:
        raise hf.HFRejected("target_forming")       # V3: reopens once the window is running
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


# ── V3: the live-price model (Decimal end to end, so every validator gets the same digits) ──
_SQRT_2PI = Decimal("2.5066282746310005024157652848110452530069867406099")
_AS_P = Decimal("0.2316419")
_AS_B = (Decimal("0.319381530"), Decimal("-0.356563782"), Decimal("1.781477937"),
         Decimal("-1.821255978"), Decimal("1.330274429"))
P_Q = Decimal("0.000001")
SIGMA_Q = Decimal("0.000000000001")


def norm_cdf(z: Decimal) -> Decimal:
    """Standard normal CDF, Abramowitz & Stegun 26.2.17 (|error| < 7.5e-8), in the fixed Decimal
    context. The approximation IS the consensus definition."""
    z = Decimal(z)
    neg = z < 0
    x = -z if neg else z
    if x > 40:
        return Decimal(0) if neg else Decimal(1)
    t = CTX.divide(Decimal(1), CTX.add(Decimal(1), CTX.multiply(_AS_P, x)))
    poly = Decimal(0)
    for coef in reversed(_AS_B):
        poly = CTX.multiply(t, CTX.add(coef, poly))
    pdf = CTX.divide(CTX.exp(CTX.divide(CTX.multiply(x, x), Decimal(-2))), _SQRT_2PI)
    c = CTX.subtract(Decimal(1), CTX.multiply(pdf, poly))
    return CTX.subtract(Decimal(1), c) if neg else c


def p_model(spot: Decimal, target: Decimal, sigma_s: Decimal, tau_s: Decimal) -> Decimal:
    """P(settle >= target | spot) for a driftless lognormal with per-second sigma over tau seconds,
    quantized to 1e-6 and clamped to [P_FLOOR, 1 - P_FLOOR]."""
    spot, target = Decimal(spot), Decimal(target)
    floor = config.MARKETS_V3_P_FLOOR
    if spot <= 0 or target <= 0:
        return Decimal("0.5")
    s = CTX.multiply(Decimal(sigma_s), CTX.sqrt(max(Decimal(tau_s), Decimal(0))))
    if s <= 0:
        p = Decimal(1) if spot >= target else Decimal(0)
    else:
        z = CTX.subtract(CTX.divide(CTX.ln(CTX.divide(spot, target)), s), CTX.divide(s, Decimal(2)))
        p = norm_cdf(z)
    p = p.quantize(P_Q, rounding=ROUND_HALF_EVEN)
    return min(max(p, floor), Decimal(1) - floor)


def spread_for(tau_s: Decimal) -> Decimal:
    """Per-share spread on an in-window bet with tau_s seconds to the end:
    SPREAD + SPREAD_K x sqrt(LEAD_S / tau)."""
    tau = max(Decimal(tau_s), Decimal(1))
    lat = CTX.multiply(config.MARKETS_V3_SPREAD_K, CTX.sqrt(CTX.divide(config.MARKETS_V3_LEAD_S, tau)))
    return CTX.add(config.MARKETS_V3_SPREAD, lat)


def anchor(p: Decimal, b: Decimal) -> Decimal:
    """The UP-quantity shift that makes an empty LMSR quote p: b x ln(p / (1 - p))."""
    p = Decimal(p)
    return CTX.multiply(Decimal(b), CTX.ln(CTX.divide(p, CTX.subtract(Decimal(1), p))))


def latest_mark(rows: list[dict], t_ms: int, max_age_ms: int | None = None) -> tuple[int, float] | None:
    """(t, mark) of the last tick AT OR BEFORE t_ms (ties broken by the larger mark, the same
    (t, mark) order the averages use); None if there is none, or it is older than max_age_ms."""
    best = None
    for r in rows:
        t = int(r["t"])
        if t > t_ms:
            continue
        m = tick_mark(r)
        if m is None:
            continue
        if best is None or (t, m) > best:
            best = (t, float(m))
    if best is None or (max_age_ms is not None and best[0] < t_ms - max_age_ms):
        return None
    return best


def first_mark_after(rows: list[dict], t_ms: int, max_wait_ms: int | None = None) -> tuple[int, float] | None:
    """(t, mark) of the first tick AT OR AFTER t_ms (ties broken by the smaller mark: the first in
    the (t, mark) order the averages use); None if there is none, or it is later than max_wait_ms."""
    best = None
    for r in rows:
        t = int(r["t"])
        if t < t_ms:
            continue
        m = tick_mark(r)
        if m is None:
            continue
        if best is None or (t, m) < best:
            best = (t, float(m))
    if best is None or (max_wait_ms is not None and best[0] > t_ms + max_wait_ms):
        return None
    return best


def minute_marks(rows: list[dict], lo_ms: int, hi_ms: int) -> list[tuple[int, float]]:
    """[(minute index, average mark)] for every minute in [lo_ms, hi_ms) that has a tick, each
    average over its ticks sorted by (t, mark) and rounded like every other consensus average."""
    by_m: dict[int, list[tuple[int, float]]] = {}
    for r in rows:
        t = int(r["t"])
        if t < lo_ms or t >= hi_ms:
            continue
        m = tick_mark(r)
        if m is None:
            continue
        by_m.setdefault(t // 60_000, []).append((t, float(m)))
    out = []
    for k in sorted(by_m):
        marks = [m for _, m in sorted(by_m[k])]
        out.append((k, round_px(sum(marks) / len(marks))))
    return out


def sigma_per_s(rows: list[dict], day_unix: int) -> Decimal | None:
    """Per-second sigma from one asset's ticks of the UTC day starting at day_unix: the RMS of the
    one-minute log returns between CONSECUTIVE minute averages (a session gap contributes nothing).
    None when fewer than MARKETS_V3_SIGMA_MIN_RETURNS returns exist."""
    mins = minute_marks(rows, day_unix * 1000, (day_unix + 86400) * 1000)
    acc, n = Decimal(0), 0
    for (k0, m0), (k1, m1) in zip(mins, mins[1:]):
        if k1 != k0 + 1 or m0 <= 0 or m1 <= 0:
            continue
        r = CTX.ln(CTX.divide(Decimal(str(m1)), Decimal(str(m0))))
        acc = CTX.add(acc, CTX.multiply(r, r))
        n += 1
    if n < config.MARKETS_V3_SIGMA_MIN_RETURNS:
        return None
    s_min = CTX.sqrt(CTX.divide(acc, Decimal(n)))
    return CTX.divide(s_min, CTX.sqrt(Decimal(60))).quantize(SIGMA_Q, rounding=ROUND_HALF_EVEN)


def sigma_fallback(asset: str, start: int) -> Decimal:
    board = hf.hf_bands_as_of(start) or {}
    cls = (board.get(asset) or (None, None, None, ""))[3]
    return config.MARKETS_V3_SIGMA_FALLBACK.get(cls, config.MARKETS_V3_SIGMA_DEFAULT)


def sigma_day(start: int) -> int:
    """The UTC day whose ticks size a market starting at `start`: the previous one."""
    return (int(start) // 86400 - 1) * 86400


def make_pricer(mid: str, rows: list[dict], target: float | None, sigma: Decimal):
    """The per-bet pricing context for a V3 market: bet -> None (pre-start: crowd only),
    {"ignore": why} (no usable fill tick) or {"p", "tick_t", "tick_mark", "tau", "spread"}. `rows`
    are the asset's sealed ticks covering the window; the fill is the first tick at or after the
    bet's receipt time + MARKETS_V3_FILL_DELAY_S (the latency guard)."""
    _a, _w, start, end = parse_market_id(mid)
    max_wait_ms = config.MARKETS_V3_MAX_TICK_AGE_S * 1000
    delay_ms = config.MARKETS_V3_FILL_DELAY_S * 1000
    half_avg = Decimal(config.MARKETS_AVG_S) / Decimal(2)
    oracle_mkt = uses_oracle(mid)       # V4: the fill is the oracle value in effect FILL_DELAY on

    def pricer(bet: dict):
        t_us = int(bet["order"][1])
        if t_us < start * 1_000_000:
            return None
        if target is None:
            return {"ignore": "no_target"}
        tk = (oracle.fill_at(rows, t_us // 1000) if oracle_mkt
              else first_mark_after(rows, t_us // 1000 + delay_ms, max_wait_ms))
        if tk is None:
            return {"ignore": "no_fill"}
        tau = CTX.subtract(CTX.subtract(Decimal(end), CTX.divide(Decimal(tk[0]), Decimal(1000))), half_avg)
        tau = max(tau, Decimal(1))
        p = p_model(Decimal(str(tk[1])), Decimal(str(target)), sigma, tau)
        return {"p": p, "tick_t": tk[0], "tick_mark": tk[1], "tau": tau, "spread": spread_for(tau)}
    return pricer


def quote_context(mid: str, rows: list[dict], target: float | None, sigma: Decimal, now: float) -> dict | None:
    """What a front end shows RIGHT NOW for an in-window market: the model at the latest tick at or
    before `now` (the fill of a bet placed now lands FILL_DELAY_S later, at a tick that does not
    exist yet). {"p", "tick_t", "tick_mark", "tau", "spread"} or None."""
    _a, _w, start, end = parse_market_id(mid)
    if target is None or now < start:
        return None
    tk = market_spot(mid, rows, int(now * 1000))
    if tk is None:
        return None
    half_avg = Decimal(config.MARKETS_AVG_S) / Decimal(2)
    tau = max(CTX.subtract(CTX.subtract(Decimal(end), Decimal(str(now))), half_avg), Decimal(1))
    p = p_model(Decimal(str(tk[1])), Decimal(str(target)), sigma, tau)
    return {"p": p, "tick_t": tk[0], "tick_mark": tk[1], "tau": tau, "spread": spread_for(tau)}


def replay(bets: list[dict], b: Decimal | None = None, start: int | None = None, pricer=None) -> list[dict]:
    """Price one market's bets in canonical order. Each bet: {key, account, side, dollars(Decimal),
    order(tuple)}. Returns the bets in order with `shares` (what the bet pays per $1 share) and
    `status` ("ok" or "ignored:<why>"), plus `lmsr_shares` (what moved the market), `price_paid`
    and, for a live-priced bet, `p_model`/`tick_t`/`tick_mark`.
    `start` selects the rule set (V1 fixed b, V2 liquidity-sensitive b); an explicit `b` pins it.
    `pricer` (V3, make_pricer) anchors the LMSR to the live-price model for in-window bets and
    charges the spread; without it every bet is crowd-only, exactly as before V3.
    A bet already marked ignored (e.g. by the collateral pass) is passed through unpriced."""
    _lo, _hi, cap = limits_for(start)
    qy = qn = Decimal(0)
    volume = Decimal(0)
    spent: dict[str, Decimal] = {}
    out = []
    pcap = config.MARKETS_V3_PRICE_CAP
    for bet in sorted(bets, key=lambda x: x["order"]):
        if str(bet.get("status", "ok")).startswith("ignored"):
            out.append({**bet, "shares": Decimal(0), "lmsr_shares": Decimal(0)})
            continue
        d = bet["dollars"]
        acct = bet["account"]
        if spent.get(acct, Decimal(0)) + d > cap:
            out.append({**bet, "shares": Decimal(0), "lmsr_shares": Decimal(0), "status": "ignored:per_market_cap"})
            continue
        ctx = pricer(bet) if pricer is not None else None
        if ctx and ctx.get("ignore"):
            out.append({**bet, "shares": Decimal(0), "lmsr_shares": Decimal(0), "status": "ignored:" + ctx["ignore"]})
            continue
        bb = Decimal(b) if b is not None else lmsr_b(start, volume)
        a = anchor(ctx["p"], bb) if ctx else Decimal(0)
        sh = shares_for(d, bet["side"], CTX.add(qy, a), qn, bb)
        rec = {**bet, "lmsr_shares": sh, "status": "ok"}
        if ctx:
            p_avg = CTX.divide(d, sh)
            p_paid = min(CTX.add(p_avg, ctx["spread"]), pcap)
            rec["shares"] = CTX.divide(d, p_paid).quantize(SHARE_Q, rounding=ROUND_DOWN)
            rec.update(price_paid=p_paid, p_model=ctx["p"], tick_t=ctx["tick_t"], tick_mark=ctx["tick_mark"],
                       spread=ctx["spread"], tau=ctx["tau"])
        else:
            rec["shares"] = sh
            rec["price_paid"] = CTX.divide(d, sh) if sh > 0 else None
        if bet["side"] == "UP":
            qy += sh
        else:
            qn += sh
        volume += d
        spent[acct] = spent.get(acct, Decimal(0)) + d
        out.append(rec)
    return out


def market_state_after(bets: list[dict], start: int | None = None) -> tuple[Decimal, Decimal, Decimal]:
    """(qy, qn, b) after a market's replayed (status ok) bets; b is what the NEXT bet is priced with."""
    qy = qn = volume = Decimal(0)
    for r in bets:
        if r.get("status") != "ok":
            continue
        sh = r.get("lmsr_shares", r["shares"])
        if r["side"] == "UP":
            qy += sh
        else:
            qn += sh
        volume += r["dollars"]
    return qy, qn, lmsr_b(start, volume)


def market_price_up(bets: list[dict], start: int | None = None, p_now: Decimal | None = None) -> Decimal:
    """The UP price after a market's (already replayed, status ok) bets, at the b the NEXT bet
    would be priced with — what a front end quotes. `p_now` (V3, in-window) anchors it to the
    live-price model the way the next bet will be."""
    qy, qn, bb = market_state_after(bets, start)
    a = anchor(p_now, bb) if p_now is not None else Decimal(0)
    return price_up(CTX.add(qy, a), qn, bb)


def buy_price(side: str, p_up: Decimal, live: bool, tau_s: Decimal | None = None) -> Decimal:
    """What one share of `side` costs the next buyer at the quoted UP price (the spread applies to
    an in-window V3 bet only, and depends on the time left)."""
    p = Decimal(p_up) if side == "UP" else CTX.subtract(Decimal(1), Decimal(p_up))
    if not live:
        return p
    return min(CTX.add(p, spread_for(tau_s if tau_s is not None else Decimal(1))), config.MARKETS_V3_PRICE_CAP)


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
    # V2
    c.execute("CREATE TABLE IF NOT EXISTS burns (key TEXT PRIMARY KEY, hk TEXT, amount TEXT, block INTEGER, "
              "ext_index INTEGER, w INTEGER, t_recv_us INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS coll (key TEXT PRIMARY KEY, entity TEXT, status TEXT, alpha TEXT, "
              "t_recv_us INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS chain_cache (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS settle (cycle INTEGER, entity TEXT, won TEXT, lost TEXT, burned TEXT, "
              "deducted TEXT, debt TEXT, payable TEXT, lcum TEXT, bcum TEXT, dcum TEXT, PRIMARY KEY (cycle, entity))")
    c.execute("CREATE TABLE IF NOT EXISTS settle_meta (cycle INTEGER PRIMARY KEY, emission TEXT)")
    # V2b (additive): per-cycle winnings and lost stakes of RETAINED markets, scored net per entity
    cols = {r[1] for r in c.execute("PRAGMA table_info(settle)")}
    for col in ("won_ret", "lost_ret"):
        if col not in cols:
            c.execute(f"ALTER TABLE settle ADD COLUMN {col} TEXT DEFAULT '0'")
    # V3 (additive: a cache built before V3 keeps its grader version; these fill in from the replay)
    c.execute("CREATE TABLE IF NOT EXISTS v3_prices (key TEXT PRIMARY KEY, market_id TEXT, p_model TEXT, "
              "price_paid TEXT, tick_t INTEGER, tick_mark REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS v3_sigma (asset TEXT, day INTEGER, sigma TEXT, measured INTEGER, "
              "PRIMARY KEY (asset, day))")
    row = c.execute("SELECT v FROM meta WHERE k='grader_version'").fetchone()
    if (int(row[0]) if row else 0) != GRADER_VERSION:
        for t in ("windows_seen", "bets", "outcomes", "results", "burns", "coll", "settle", "settle_meta"):
            c.execute(f"DELETE FROM {t}")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('grader_version', ?)", (str(GRADER_VERSION),))
        c.commit()
    return c


def ingest_entries(db: sqlite3.Connection, w: int, entries: list[dict]) -> int:
    """Store the window's valid mk.bet and mk.burn entries. Validity is re-checked here off the
    receipt time, so an entry the ingest should have refused can never count."""
    n = 0
    for e in entries:
        sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
        p = sub.get("payload") or {}
        kind = str(p.get("kind", ""))
        if kind not in MARKETS_KINDS:
            continue
        hk, seq = sub.get("hk"), sub.get("seq")
        t_us = rcpt.get("t_recv_us")
        if not hk or seq is None or not t_us:
            continue
        try:
            validate_entry(p, hk, int(t_us) / 1e6)
        except hf.HFRejected:
            continue
        if kind == KIND_BURN:
            db.execute("INSERT OR IGNORE INTO burns VALUES (?,?,?,?,?,?,?)",
                       (f"{hk}:{seq}", hk, str(parse_alpha(p["amount_alpha"])), int(p["block"]),
                        int(p["ext_index"]), int(w), int(t_us)))
        else:
            start = parse_market_id(str(p["market_id"]))[2]
            db.execute("INSERT OR IGNORE INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"{hk}:{seq}", str(p["market_id"]), str(p["account"]), str(p["side"]),
                        str(parse_dollars(p["dollars"], start)), int(w), int(t_us), hk, int(seq)))
        n += 1
    return n


def grade_market(db: sqlite3.Connection, mid: str, rows: list[dict], sigma: Decimal | None = None) -> str:
    """Grade one market from the asset's sealed ticks (covering target minute .. end). `sigma` is the
    V3 per-second volatility (sigma_for_market); a V3 market without one prices with the class
    fallback."""
    asset, _w, start, end = parse_market_id(mid)
    outcome, target, settle = (oracle.resolve(rows, start, end) if uses_oracle(mid)
                               else resolve(rows, start, end))
    v2 = is_v2(start)
    pricer = None
    if is_v3(start):
        pricer = make_pricer(mid, rows, target, sigma if sigma is not None else sigma_fallback(asset, start))
    coll = {}
    if v2:
        coll = {k: s for k, s in db.execute(
            "SELECT c.key, c.status FROM coll c JOIN bets b ON b.key = c.key WHERE b.market_id=?", (mid,))}
    bets, outside = [], []
    for k, a, s, d, w, t, h, q in db.execute(
            "SELECT key, account, side, dollars, w, t_recv_us, hk, seq FROM bets WHERE market_id=?", (mid,)):
        bet = {"key": k, "account": a, "side": s, "dollars": Decimal(d), "order": (w, t, h, q)}
        if v2 and coll.get(k, "ok") != "ok":
            bet["status"] = coll[k]                  # ignored:collateral — never priced
        # The replay decides the entry window, whatever the ingest accepted.
        (bets if in_entry_us(mid, int(t)) else outside).append(bet)
    for r in replay(bets, start=start, pricer=pricer) + [
            {**b, "shares": Decimal(0), "status": "ignored:outside_entry_window"} for b in outside]:
        pnl = bet_pnl(r["side"], r["shares"], r["dollars"], outcome) if r["status"] == "ok" else Decimal(0)
        db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                   (r["key"], mid, r["account"], str(r["dollars"]), str(r["shares"]), str(pnl),
                    r["status"], outcome, end))
        if r.get("p_model") is not None:
            db.execute("INSERT OR REPLACE INTO v3_prices VALUES (?,?,?,?,?,?)",
                       (r["key"], mid, str(r["p_model"]), str(r["price_paid"]), int(r["tick_t"]), float(r["tick_mark"])))
    db.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)", (mid, outcome, target, settle, end))
    return outcome


# ── V2: the chain view (everything validators read from the chain, cached) ───
class ChainView:
    """What V2 needs from the chain. Every value is a function of (block, args) on the canonical
    chain, so any validator reading the same blocks gets the same numbers; the cache only saves
    RPCs. Subclass for tests (FakeChain) or use RpcChainView."""

    def block_at(self, t_unix: int) -> int | None:            # first block with timestamp >= t
        raise NotImplementedError

    def block_time(self, block: int) -> int:
        raise NotImplementedError

    def entity_stake(self, hotkey: str, block: int) -> Decimal:  # alpha, owner coldkey on hotkey
        raise NotImplementedError

    def alpha_price_tao(self, block: int) -> Decimal:
        raise NotImplementedError

    def alpha_out_per_block(self, block: int) -> Decimal:
        raise NotImplementedError

    def mecid0_fraction(self, block: int) -> Decimal:
        raise NotImplementedError

    def burn_events(self, block: int, ext_index: int) -> list[dict]:
        """[{event, coldkey, hotkey, amount (alpha Decimal), netuid}] for that extrinsic."""
        raise NotImplementedError


class CachedChain:
    """Memoises a ChainView in the grade DB so a rebuild reads the chain once per value."""

    def __init__(self, db: sqlite3.Connection, view: ChainView):
        self.db, self.view = db, view

    def _get(self, key: str, fn):
        row = self.db.execute("SELECT v FROM chain_cache WHERE k=?", (key,)).fetchone()
        if row is not None:
            return json.loads(row[0])
        v = fn()
        if v is None:
            return None                              # not available yet: never cache a miss
        enc = v if isinstance(v, (int, list, dict)) else str(v)
        self.db.execute("INSERT OR REPLACE INTO chain_cache VALUES (?,?)", (key, json.dumps(enc)))
        return enc

    def block_at(self, t: int) -> int | None:
        v = self._get(f"block_at:{int(t)}", lambda: self.view.block_at(int(t)))
        return int(v) if v is not None else None

    def block_time(self, b: int) -> int:
        return int(self._get(f"block_time:{b}", lambda: self.view.block_time(b)))

    def entity_stake(self, hk: str, b: int) -> Decimal:
        return Decimal(self._get(f"stake:{hk}:{b}", lambda: self.view.entity_stake(hk, b)))

    def alpha_price_tao(self, b: int) -> Decimal:
        return Decimal(self._get(f"price:{b}", lambda: self.view.alpha_price_tao(b)))

    def alpha_out_per_block(self, b: int) -> Decimal:
        return Decimal(self._get(f"aout:{b}", lambda: self.view.alpha_out_per_block(b)))

    def mecid0_fraction(self, b: int) -> Decimal:
        return Decimal(self._get(f"mech0:{b}", lambda: self.view.mecid0_fraction(b)))

    def burn_events(self, b: int, i: int) -> list[dict]:
        v = self._get(f"burnev:{b}:{i}", lambda: [
            {**e, "amount": str(e["amount"])} for e in self.view.burn_events(b, i)])
        return [{**e, "amount": Decimal(e["amount"])} for e in (v or [])]


class RpcChainView(ChainView):
    """Reads the live chain (finney / test) through sn89_signals.chain.Chain; historic state from
    the archive node when the public node has pruned it."""

    def __init__(self, netuid: int | None = None, network: str | None = None):
        from . import chain as _chain
        self.c = _chain.Chain(network=network, netuid=netuid)
        self.netuid = self.c.netuid

    # The public finney node answers a query for PRUNED state (older than ~256 blocks) with the
    # storage default (0, None, []) instead of an error, so "try public, fall back on error" read
    # SubnetAlphaOutEmission = 0 for settle cycle 1791598320 and burned that cycle's whole Markets
    # share (2026-10-10). Historic reads go to the archive first; the public node only serves blocks
    # it still holds. Testnet has no archive alias, so it reads its own node.
    _RECENT_BLOCKS = 200

    def _nodes(self, block):
        if str(getattr(config, "NETWORK", "finney")).startswith("test"):
            return (self.c.st,)
        try:
            recent = self.c.current_block() - int(block) <= self._RECENT_BLOCKS
        except Exception:  # noqa: BLE001
            recent = False
        return (self.c.st, self.c._archive()) if recent else (self.c._archive(),)

    def _q(self, module, name, params, block):
        for st in self._nodes(block):
            try:
                bh = st.get_block_hash(block)
                r = st.substrate.query(module, name, params, block_hash=bh)
                return getattr(r, "value", r)
            except Exception:  # noqa: BLE001 — pruned: try the archive
                continue
        raise RuntimeError(f"unreadable {module}.{name} at {block}")

    def block_time(self, block: int) -> int:
        return int(self.c.block_time_ms(block) // 1000)

    def block_at(self, t_unix: int) -> int | None:
        head = self.c.current_block()
        if self.block_time(head) < t_unix:
            return None                              # not reached yet
        lo, hi = max(1, head - int((self.block_time(head) - t_unix) / 12) - 600), head
        while self.block_time(lo) >= t_unix:
            lo = max(1, lo - 3600)
        while hi - lo > 1:                            # invariant: time(lo) < t <= time(hi)
            mid = (lo + hi) // 2
            if self.block_time(mid) >= t_unix:
                hi = mid
            else:
                lo = mid
        return hi

    def entity_stake(self, hotkey: str, block: int) -> Decimal:
        owner = self._q("SubtensorModule", "Owner", [hotkey], block)
        err = None
        for st in self._nodes(block):                 # pruned state reads as 0 on the public node
            try:
                bal = st.get_stake(coldkey_ss58=str(owner), hotkey_ss58=hotkey, netuid=self.netuid, block=block)
                return Decimal(str(getattr(bal, "tao", bal)))
            except Exception as e:  # noqa: BLE001
                err = e
        raise RuntimeError(f"unreadable stake {hotkey[:8]} at {block}: {err}")

    def alpha_price_tao(self, block: int) -> Decimal:
        tao = Decimal(int(self._q("SubtensorModule", "SubnetTAO", [self.netuid], block)))
        alpha_in = Decimal(int(self._q("SubtensorModule", "SubnetAlphaIn", [self.netuid], block)))
        return CTX.divide(tao, alpha_in) if alpha_in else Decimal(0)

    def alpha_out_per_block(self, block: int) -> Decimal:
        return Decimal(int(self._q("SubtensorModule", "SubnetAlphaOutEmission", [self.netuid], block))) / Decimal(10**9)

    def mecid0_fraction(self, block: int) -> Decimal:
        split = self._q("SubtensorModule", "MechanismEmissionSplit", [self.netuid], block) or []
        split = [int(x) for x in split]
        return CTX.divide(Decimal(split[0]), Decimal(sum(split))) if split and sum(split) else Decimal(1)

    def burn_events(self, block: int, ext_index: int) -> list[dict]:
        out = []
        for st in self._nodes(block):
            try:
                evs = st.substrate.get_events(block_hash=st.get_block_hash(block))
                break
            except Exception:  # noqa: BLE001
                evs = None
        for ev in evs or []:
            v = getattr(ev, "value", ev)
            if v.get("extrinsic_idx") != ext_index or v.get("module_id") != "SubtensorModule":
                continue
            name = v.get("event_id")
            if name not in ("AlphaBurned", "AlphaRecycled"):
                continue
            a = v.get("attributes")
            vals = list(a.values()) if isinstance(a, dict) else list(a)
            out.append({"event": name, "coldkey": str(vals[0]), "hotkey": str(vals[1]),
                        "amount": Decimal(int(vals[2])) / Decimal(10**9), "netuid": int(vals[3])})
        return out


def _day(t: int) -> int:
    return int(t) // 86400 * 86400


def market_rate(db: sqlite3.Connection, ch: CachedChain, base: str, cache_dir: str, start: int) -> Decimal | None:
    """USD per alpha for a market: the rate of its start UTC day (see module doc). None until the
    day's first block and the TAOUSD minute are both available."""
    from . import hf_grade
    day0 = _day(start)
    for back in range(config.MARKETS_RATE_FALLBACK_DAYS + 1):
        d = day0 - back * 86400
        row = db.execute("SELECT v FROM chain_cache WHERE k=?", (f"rate:{d}",)).fetchone()
        if row is not None:
            return Decimal(json.loads(row[0]))
        blk = ch.block_at(d)
        if blk is None:
            return None
        rows, missing = hf_grade._ticks_for(base, os.path.join(cache_dir, "ticks"), config.MARKETS_RATE_PAIR,
                                            (d - config.MARKETS_AVG_S) * 1000, d * 1000)
        if missing:
            return None
        usd = average_marks(rows, (d - config.MARKETS_AVG_S) * 1000, d * 1000)
        if usd is None:
            continue                                  # no TAOUSD that minute: previous day's rate
        rate = CTX.multiply(ch.alpha_price_tao(blk), Decimal(str(usd)))
        if rate <= 0:
            continue
        db.execute("INSERT OR REPLACE INTO chain_cache VALUES (?,?)", (f"rate:{d}", json.dumps(str(rate))))
        return rate
    return None


def _cycle(t: int) -> int:
    """Start of the settlement cycle containing t (grid anchored at the V2 arm)."""
    P, T0 = config.MARKETS_SETTLE_PERIOD_S, config.MARKETS_COLLATERAL_FROM_UNIX
    return T0 + (int(t) - T0) // P * P


def _closed_by(t: int) -> int:
    """Start of the latest cycle that is closed at t (end + grace <= t)."""
    P, G = config.MARKETS_SETTLE_PERIOD_S, config.MARKETS_SETTLE_GRACE_S
    return _cycle(int(t) - G - P)


def debt_as_of(db: sqlite3.Connection, entity: str, t: int) -> Decimal:
    """The entity's unburned losses after the last cycle CLOSED by t (never negative here)."""
    row = db.execute("SELECT debt FROM settle WHERE entity=? AND cycle <= ? ORDER BY cycle DESC LIMIT 1",
                     (entity, _closed_by(t))).fetchone()
    return max(Decimal(row[0]), Decimal(0)) if row else Decimal(0)


def collateral_pass(db: sqlite3.Connection, ch: CachedChain, base: str, cache_dir: str, frontier_us: int) -> int:
    """Decide ok / ignored:collateral for every undecided V2 bet received before `frontier_us`,
    entity by entity in canonical order. A bet waits (and so does every later bet of the same
    entity) until its hour snapshot, its market's rate and the debt it depends on are known."""
    decided = 0
    rows = db.execute(
        "SELECT b.key, b.market_id, b.account, b.dollars, b.w, b.t_recv_us, b.hk, b.seq FROM bets b "
        "LEFT JOIN coll c ON c.key = b.key WHERE c.key IS NULL AND b.t_recv_us < ? "
        "ORDER BY b.w, b.t_recv_us, b.hk, b.seq", (int(frontier_us),)).fetchall()
    blocked: set[str] = set()
    for key, mid, acct, dollars, _w, t_us, _hk, _seq in rows:
        start = parse_market_id(mid)[2]
        if not is_v2(start):
            continue
        entity = account_owner(acct)
        if entity in blocked:
            continue
        t = int(t_us) // 1_000_000
        hour = t // config.MARKETS_COLLATERAL_SNAPSHOT_S * config.MARKETS_COLLATERAL_SNAPSHOT_S
        if _cycle_unclosed_before(db, entity, hour):
            blocked.add(entity)
            continue
        blk = ch.block_at(hour)
        rate = market_rate(db, ch, base, cache_dir, start)
        if blk is None or rate is None:
            blocked.add(entity)
            continue
        collateral = ch.entity_stake(entity, blk) - debt_as_of(db, entity, hour)
        alpha = CTX.divide(Decimal(dollars), rate)
        open_alpha = Decimal(0)
        for a, m2 in db.execute(
                "SELECT c.alpha, b.market_id FROM coll c JOIN bets b ON b.key = c.key "
                "WHERE c.entity=? AND c.status='ok' AND c.t_recv_us < ?", (entity, int(t_us))):
            if parse_market_id(m2)[3] > t:           # its market has not ended: still open
                open_alpha += Decimal(a)
        status = "ok" if open_alpha + alpha <= collateral else "ignored:collateral"
        db.execute("INSERT INTO coll VALUES (?,?,?,?,?)", (key, entity, status, str(alpha), int(t_us)))
        decided += 1
    return decided


def _cycle_unclosed_before(db: sqlite3.Connection, entity: str, t: int) -> bool:
    """True when a cycle that is closed at t (so its debt counts) has not been settled yet and this
    entity had V2 results in or before it: the bet must wait for it."""
    need = _closed_by(t)
    if need < config.MARKETS_COLLATERAL_FROM_UNIX:
        return False
    row = db.execute("SELECT MAX(cycle) FROM settle_meta").fetchone()
    if row and row[0] is not None and row[0] >= need:
        return False
    has = db.execute("SELECT 1 FROM coll c JOIN results r ON r.key = c.key WHERE c.entity=? AND r.end_ts < ? LIMIT 1",
                     (entity, need + config.MARKETS_SETTLE_PERIOD_S)).fetchone()
    return has is not None


def settle_cycles(db: sqlite3.Connection, ch: CachedChain, base: str, cache_dir: str, now: int,
                  published_through: int | None = None) -> int:
    """Close every settlement cycle that is past end + grace and whose V2 markets are all graded.
    Per entity: W (winnings, alpha), L (lost stakes), verified burns, the overdue-loss deduction,
    debt and payable; plus the cycle's Markets emission (alpha). From the V2b stamp, markets
    starting at/after it are kept apart (won_ret / lost_ret): their lost stakes are neither burned
    nor debt, and they add max(0, won_ret - lost_ret) to payable."""
    P, G, DL = config.MARKETS_SETTLE_PERIOD_S, config.MARKETS_SETTLE_GRACE_S, config.MARKETS_BURN_DEADLINE_S
    row = db.execute("SELECT MAX(cycle) FROM settle_meta").fetchone()
    c = (row[0] + P) if row and row[0] is not None else config.MARKETS_COLLATERAL_FROM_UNIX
    closed = 0
    while c + P + G <= int(now) and (published_through is None or c + P + G <= published_through):
        ends = {}
        for (mid,) in db.execute("SELECT DISTINCT market_id FROM bets").fetchall():
            _a, _w, st, en = parse_market_id(mid)
            if is_v2(st) and c <= en < c + P:
                ends[mid] = en
        graded = {m for (m,) in db.execute("SELECT market_id FROM outcomes")}
        if any(m not in graded for m in ends):
            break                                      # grade first, then settle
        b0, b1 = ch.block_at(c), ch.block_at(c + P)
        if b0 is None or b1 is None:
            break
        # entity -> [won, lost] for pre-stamp (burn) markets, [won_ret, lost_ret] for retained ones
        per: dict[str, list[Decimal]] = {}
        for key, acct, dollars, shares, pnl, status, outcome, mid in db.execute(
                "SELECT key, account, dollars, shares, pnl, status, outcome, market_id FROM results "
                "WHERE end_ts >= ? AND end_ts < ?", (c, c + P)).fetchall():
            if mid not in ends or status != "ok" or outcome == "VOID":
                continue
            start = parse_market_id(mid)[2]
            rate = market_rate(db, ch, base, cache_dir, start)
            ent = account_owner(acct)
            w_l = per.setdefault(ent, [Decimal(0), Decimal(0), Decimal(0), Decimal(0)])
            off = 2 if is_retained(start) else 0
            p = Decimal(pnl)
            if p > 0:
                w_l[off] += CTX.divide(p, rate)
            elif p < 0:
                w_l[off + 1] += CTX.divide(-p, rate)
        # A burn counts for cycle c when its CLAIM was received in [c + grace, c + P + grace). Each
        # on-chain extrinsic is credited once, to the first claim naming it in canonical order.
        burned: dict[str, Decimal] = {}
        for hk, amount, blk, idx, t_us in db.execute(
                "SELECT hk, amount, block, ext_index, t_recv_us FROM burns b WHERE t_recv_us >= ? AND t_recv_us < ? "
                "AND NOT EXISTS (SELECT 1 FROM burns e WHERE e.block = b.block AND e.ext_index = b.ext_index "
                "AND (e.w < b.w OR (e.w = b.w AND (e.t_recv_us < b.t_recv_us OR (e.t_recv_us = b.t_recv_us "
                "AND e.key < b.key)))))",
                ((c + G) * 1_000_000, (c + P + G) * 1_000_000)).fetchall():
            bt_ = ch.block_time(int(blk))
            if not (c - 86400 <= bt_ <= int(t_us) // 1_000_000):
                continue                               # a burn after its own claim, or a stale one
            ok = verified_burn(ch, hk, Decimal(amount), int(blk), int(idx))
            if ok > 0:
                burned[hk] = burned.get(hk, Decimal(0)) + ok
        ents = set(per) | set(burned) | {e for (e,) in db.execute("SELECT DISTINCT entity FROM settle")}
        Z = Decimal(0)
        for ent in sorted(ents):
            won, lost, won_r, lost_r = per.get(ent, [Z, Z, Z, Z])
            prev = db.execute("SELECT lcum, bcum, dcum FROM settle WHERE entity=? AND cycle < ? ORDER BY cycle DESC "
                              "LIMIT 1", (ent, c)).fetchone()
            lcum0, bcum0, dcum0 = (Decimal(x) for x in prev) if prev else (Z, Z, Z)
            lcum, bcum = lcum0 + lost, bcum0 + burned.get(ent, Z)
            # losses from cycles that ended at least the burn deadline before this cycle ends
            old = db.execute("SELECT lcum FROM settle WHERE entity=? AND cycle <= ? ORDER BY cycle DESC LIMIT 1",
                             (ent, c - DL)).fetchone()
            overdue = max(Z, (Decimal(old[0]) if old else Z) - bcum - dcum0)
            if overdue < RAO:
                overdue = Z                            # burns are whole rao: sub-rao rounding is not debt
            deducted = min(won, overdue)
            dcum = dcum0 + deducted
            debt = lcum - bcum - dcum                   # negative = burn credit carried forward
            if abs(debt) < RAO:
                debt = Z
            # V2b: retained markets pay on the entity's NET; a net loss is simply kept, never owed
            net_r = max(Z, won_r - lost_r)
            db.execute("INSERT OR REPLACE INTO settle (cycle, entity, won, lost, burned, deducted, debt, payable, "
                       "lcum, bcum, dcum, won_ret, lost_ret) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (c, ent, str(won), str(lost), str(burned.get(ent, Z)), str(deducted), str(debt),
                        str(won - deducted + net_r), str(lcum), str(bcum), str(dcum), str(won_r), str(lost_r)))
        share = Decimal(str(config.comp_weights_as_of(c).get("markets", 0)))
        emission = CTX.multiply(CTX.multiply(CTX.multiply(CTX.multiply(
            Decimal(b1 - b0), ch.alpha_out_per_block(b0)), config.MARKETS_MINER_FRACTION),
            ch.mecid0_fraction(b0)), share)
        db.execute("INSERT OR REPLACE INTO settle_meta VALUES (?,?)", (c, str(emission)))
        c += P
        closed += 1
    return closed


def verified_burn(ch: CachedChain, entity: str, claimed: Decimal, block: int, ext_index: int) -> Decimal:
    """The alpha a claim may be credited with: the claimed amount when that extrinsic emitted an
    AlphaBurned/AlphaRecycled on this subnet for the entity hotkey of at least that amount."""
    for ev in ch.burn_events(block, ext_index):
        if ev["hotkey"] == entity and int(ev["netuid"]) == int(config.NETUID) and ev["amount"] >= claimed:
            return claimed
    return Decimal(0)


def pnl_vector(db: sqlite3.Connection, uid_by_hk: dict, now: int) -> dict[int, float]:
    """{uid: weight} for the latest closed cycle: payable / that cycle's Markets emission, pro rata
    past 1, the rest to the burn UID. Every weight commit during the next cycle carries it."""
    row = db.execute("SELECT cycle, emission FROM settle_meta WHERE cycle <= ? ORDER BY cycle DESC LIMIT 1",
                     (_closed_by(now),)).fetchone()
    weights: dict[int, float] = {}
    if row is not None and Decimal(row[1]) > 0:
        c, emission = row[0], Decimal(row[1])
        x = {}
        for ent, payable in db.execute("SELECT entity, payable FROM settle WHERE cycle=?", (c,)):
            p = Decimal(payable)
            if p > 0 and ent in uid_by_hk:
                x[uid_by_hk[ent]] = x.get(uid_by_hk[ent], Decimal(0)) + CTX.divide(p, emission)
        tot = sum(x.values(), Decimal(0))
        scale = CTX.divide(Decimal(1), tot) if tot > 1 else Decimal(1)
        weights = {u: float(CTX.multiply(v, scale)) for u, v in x.items()}
    weights[config.BURN_UID] = weights.get(config.BURN_UID, 0.0) + max(0.0, 1.0 - sum(weights.values()))
    total = sum(weights.values())
    return {u: w / total for u, w in weights.items()}


def day_ticks(base: str, tick_dir: str, pairs: set, day_unix: int) -> tuple[dict, list]:
    """({pair: rows} for the UTC day, missing windows) reading every window of the day ONCE (a window
    file carries every pair; fetching per pair would read it |pairs| times). Same cache and same
    in-place local source as hf_grade._ticks_for."""
    from . import hf_grade
    os.makedirs(tick_dir, exist_ok=True)
    out: dict = {p: [] for p in pairs}
    missing = []
    lo_ms, hi_ms = day_unix * 1000, (day_unix + 86400) * 1000
    w = lo_ms
    while w < hi_ms:
        local = os.path.join(tick_dir, f"{w}.ticks.jsonl")
        src = local
        if not os.path.exists(local):
            cand = (os.path.join(hf_grade.LOCAL_TICK_SRC, f"{w}.ticks.jsonl") if hf_grade.LOCAL_TICK_SRC else None)
            if cand and os.path.exists(cand) and os.path.exists(cand[:-6] + ".json"):
                src = cand
            else:
                txt = hf_grade._fetch_text(f"{base.rstrip('/')}/{w}/ticks.jsonl")
                if txt is not None:
                    tmp = local + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(txt)
                    os.replace(tmp, local)
        rows = []
        try:
            with open(src, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("a") in out:
                        rows.append(d)
        except (OSError, ValueError, KeyError):
            missing.append(w)
        else:
            for d in rows:
                out[d["a"]].append(d)
        w += hf_grade.WINDOW_MS
    for p in out:
        out[p].sort(key=lambda d: int(d["t"]))
    return out, missing


def sigma_for_market(db: sqlite3.Connection, base: str, tick_dir: str, asset: str, start: int,
                     final: bool = False) -> Decimal | None:
    """The V3 sigma for a market starting at `start`: measured from the previous UTC day's sealed
    ticks (every board asset of that day computed and cached in one pass), else the class
    fallback. None while a window of that day is still unfetchable and `final` is False (wait);
    with `final` the fallback is used, and remembered."""
    day = sigma_day(start)
    row = db.execute("SELECT sigma FROM v3_sigma WHERE asset=? AND day=?", (asset, day)).fetchone()
    if row:
        return Decimal(row[0])
    board = hf.hf_bands_as_of(start) or {}
    pairs = set(board) | {asset}
    rows_by, missing = day_ticks(base, tick_dir, pairs, day)
    if missing and not final:
        return None
    for p in sorted(pairs):
        s = sigma_per_s(rows_by.get(p, []), day) if not missing else None
        measured = s is not None
        if s is None:
            s = sigma_fallback(p, start)
        db.execute("INSERT OR REPLACE INTO v3_sigma VALUES (?,?,?,?)", (p, day, str(s), int(measured)))
    db.commit()
    return Decimal(db.execute("SELECT sigma FROM v3_sigma WHERE asset=? AND day=?", (asset, day)).fetchone()[0])


def sync_and_grade(base: str, cache_dir: str, now: float, chain_view: ChainView | None = None) -> None:
    """Pull Markets receipts from the published windows, decide V2 collateral, grade every market
    whose receipts and ticks are complete, and close finished settlement cycles. Incremental; a from-scratch
    rebuild gives the same results (chain reads are a function of the block)."""
    from . import hf_grade

    db = _db(cache_dir)
    tick_dir = os.path.join(cache_dir, "ticks")
    seen = {r[0] for r in db.execute("SELECT w FROM windows_seen")}
    index = hf_grade._index(base)
    # No Markets frame is valid before the arm (ingest_entries re-checks every receipt), so windows
    # that ended well before it cannot change any result. Skipping them keeps a fresh cache from
    # fetching the network's whole HF history (21k+ windows on finney).
    lo_ms = (config.MARKETS_FROM_UNIX - 2 * 3600) * 1000 if config.MARKETS_FROM_UNIX else 0
    for w in index:
        if w in seen or w < lo_ms:
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
    ch = CachedChain(db, chain_view) if chain_view is not None else None
    v2_live = bool(config.MARKETS_COLLATERAL_FROM_UNIX)
    # V2 cycles/collateral interleave: grade and settle in a loop until nothing moves.
    for _ in range(64):
        moved = 0
        if ch is not None and v2_live:
            # bets two windows behind what is published are final (windows seal in order)
            frontier = (published_through - 2 * hf_grade.WINDOW_MS // 1000) * 1_000_000
            moved += collateral_pass(db, ch, base, cache_dir, frontier)
        graded = {r[0] for r in db.execute("SELECT market_id FROM outcomes")}
        for (mid,) in db.execute("SELECT DISTINCT market_id FROM bets").fetchall():
            if mid in graded:
                continue
            asset, _w, start, end = parse_market_id(mid)
            if now_s < end + config.MARKETS_GRADE_SETTLE_S or published_through < end:
                continue
            if is_v2(start):
                if ch is None:
                    continue
                undecided = db.execute(
                    "SELECT COUNT(*) FROM bets b LEFT JOIN coll c ON c.key=b.key WHERE b.market_id=? AND c.key IS NULL",
                    (mid,)).fetchone()[0]
                if undecided:
                    continue                     # collateral first, then price
            if uses_oracle(mid):
                # V4: the separate, published oracle record (oracle.py); the HF windows are not read
                rows, missing = oracle.rows_for(config.MARKETS_ORACLE_PUBLIC_BASE,
                                                os.path.join(cache_dir, "oracle"), asset,
                                                (start - config.MARKETS_AVG_S) * 1000, end * 1000)
            else:
                rows, missing = hf_grade._ticks_for(base, tick_dir, asset,
                                                    (start - config.MARKETS_AVG_S) * 1000, end * 1000)
            if missing and now_s < end + config.MARKETS_GRADE_ABANDON_S:
                continue                       # never grade a hole: wait, then void
            sigma = None
            if is_v3(start):
                sigma = sigma_for_market(db, base, tick_dir, asset, start,
                                         final=now_s >= end + config.MARKETS_GRADE_ABANDON_S)
                if sigma is None:
                    continue                   # previous day's ticks not all in hand yet: wait
            grade_market(db, mid, rows, sigma)
            moved += 1
        if ch is not None and v2_live:
            moved += settle_cycles(db, ch, base, cache_dir, now_s, published_through)
        db.commit()
        if not moved:
            break
    db.close()


def window_results(cache_dir: str, now: float) -> list[dict]:
    db = _db(cache_dir)
    lo = int(now) - config.MARKETS_SCORE_WINDOW_S
    out = [{"account": a, "dollars": Decimal(d), "pnl": Decimal(p), "outcome": o}
           for a, d, p, o, s, mid in db.execute(
               "SELECT account, dollars, pnl, outcome, status, market_id FROM results WHERE end_ts > ? AND end_ts <= ?",
               (lo, int(now))) if s == "ok" and not is_v2(parse_market_id(mid)[2])]
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
                    cache_dir: str | None = None, chain_view: ChainView | None = None) -> dict[int, float]:
    """{uid: weight} for the markets competition. Before V2: the play-money skill vector. From the
    V2 arm: the P&L vector of the latest closed settlement cycle (burn until one has closed)."""
    now = time.time() if now is None else now
    base = base or hf.HF_PUBLIC_BASE
    cache_dir = cache_dir or os.path.expanduser(os.getenv("SN89_MARKETS_GRADE_CACHE", "~/.sn89/markets-grade"))
    if config.markets_collateral_as_of(now):
        view = chain_view or RpcChainView()
        sync_and_grade(base, cache_dir, now, view)
        db = _db(cache_dir)
        try:
            vec = pnl_vector(db, uid_by_hk, int(now))
        finally:
            db.close()
    else:
        vec = weights_from_scores(markets_tallies(now, base, cache_dir), uid_by_hk, now)
    if config.markets_entity_dust_as_of(now):
        db = _db(cache_dir)
        try:
            vec = apply_entity_dust(vec, entity_dust_uids(db, uid_by_hk, now, chain_view))
        finally:
            db.close()
    return vec


def active_entities(db: sqlite3.Connection, now: float) -> list[str]:
    """Hotkeys that filed a Markets frame (mk.bet or mk.burn) in the last MARKETS_ENTITY_ACTIVE_S."""
    since_us = int((now - config.MARKETS_ENTITY_ACTIVE_S) * 1_000_000)
    now_us = int(now * 1_000_000)
    rows = db.execute("SELECT hk FROM bets WHERE t_recv_us >= ? AND t_recv_us <= ? UNION "
                      "SELECT hk FROM burns WHERE t_recv_us >= ? AND t_recv_us <= ?",
                      (since_us, now_us, since_us, now_us)).fetchall()
    return sorted({r[0] for r in rows if r[0]})


def entity_dust_uids(db: sqlite3.Connection, uid_by_hk: dict, now: float,
                     chain_view: ChainView | None = None) -> set[int]:
    """UIDs of active entities whose owner coldkey holds >= markets_dust_min_collateral_as_of(now)
    on their hotkey at the first block of the cycle's UTC hour. A chain read failure gives no dust
    this cycle (logged), never a crash."""
    ents = [h for h in active_entities(db, now) if h in uid_by_hk and uid_by_hk[h] != config.BURN_UID]
    if not ents:
        return set()
    hour = int(now) // 3600 * 3600
    minimum = config.markets_dust_min_collateral_as_of(now)
    try:
        ch = CachedChain(db, chain_view or RpcChainView())
        blk = ch.block_at(hour)
        if blk is None:
            raise RuntimeError(f"no block at {hour}")
        out = set()
        for h in ents:
            if ch.entity_stake(h, blk) >= minimum:
                out.add(uid_by_hk[h])
        db.commit()
        return out
    except Exception as e:  # noqa: BLE001 — a weight cycle must never die on a chain read
        print(f"  !! MARKETS ENTITY DUST SKIPPED this cycle: chain read failed: {e}")
        return set()


def apply_entity_dust(vec: dict[int, float], uids: set[int]) -> dict[int, float]:
    """Raise each uid to at least DUST_WEIGHT inside the (already normalized) markets vector.
    max(earned, dust), never the sum. The difference comes from the burn UID first; only if the
    burn cannot cover it (every unit already paid out) are the other weights scaled down pro rata.
    A dust-only vector is never renormalized up to the whole share."""
    if not uids:
        return vec
    out = dict(vec)
    raised = set()
    for u in sorted(uids):
        if out.get(u, 0.0) < config.DUST_WEIGHT:
            out[u] = config.DUST_WEIGHT
            raised.add(u)
    excess = sum(out.values()) - 1.0
    if excess <= 0:
        return out
    take = min(excess, out.get(config.BURN_UID, 0.0))
    if take > 0:
        out[config.BURN_UID] -= take
        excess -= take
    if excess > 1e-15:
        rest = {u: w for u, w in out.items() if u not in raised and u != config.BURN_UID and w > 0}
        tot = sum(rest.values())
        if tot > 0:
            k = (tot - excess) / tot
            for u in rest:
                out[u] *= k
    if config.BURN_UID in out and out[config.BURN_UID] <= 0:
        out.pop(config.BURN_UID)
    return out
