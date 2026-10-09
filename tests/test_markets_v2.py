"""Markets V2 (Whit 2026-10-08): entity collateral + P&L-basis daily emission."""
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from sn89_signals import config, hf, markets

ENTITY = "5DPGU1Lw8zVHs6mCC6Q1MHr4cXEwwBf8mBXc2xNAxLn6EbV1"
W = 180_000
DAY = (int(time.time()) // 86400 - 2) * 86400          # a recent UTC day, fully in the past
S1 = DAY + 1800                                          # 00:30 UTC, 15m markets
PRICE_TAO = Decimal("0.003")                             # TAO per alpha
TAOUSD = 400.0                                           # -> 1.2 USD per alpha
RATE = PRICE_TAO * Decimal(str(TAOUSD))


class FakeChain(markets.ChainView):
    def __init__(self, stake=Decimal(1000), burns=None, aout=Decimal(1)):
        self.stake, self.burns, self.aout = stake, burns or {}, aout

    def block_at(self, t):
        return t // 12

    def block_time(self, b):
        return b * 12

    def entity_stake(self, hk, b):
        return self.stake(b) if callable(self.stake) else self.stake

    def alpha_price_tao(self, b):
        return PRICE_TAO

    def alpha_out_per_block(self, b):
        return self.aout

    def mecid0_fraction(self, b):
        return Decimal("0.8")

    def burn_events(self, b, i):
        return self.burns.get((b, i), [])


@pytest.fixture
def v2(monkeypatch):
    from sn89_signals import hf_grade
    monkeypatch.setattr(config, "MARKETS_FROM_UNIX", 1)
    monkeypatch.setattr(config, "MARKETS_COLLATERAL_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_SETTLE_PERIOD_S", 3600)
    monkeypatch.setattr(config, "MARKETS_SETTLE_GRACE_S", 60)
    monkeypatch.setattr(hf_grade, "LOCAL_TICK_SRC", "")
    monkeypatch.setattr(config, "comp_weights_as_of", lambda t: {"lf": 0.4375, "hf": 0.4375, "markets": 0.125})
    return config


def _payload(start, side="UP", dollars="10.00", n=1, pair="BTCUSD"):
    return {"kind": "mk.bet", "market_id": markets.market_id(pair, "15m", start), "side": side,
            "dollars": dollars, "account": f"{ENTITY}_{n}", "trade_pair": pair}


def _entry(t, seq, payload):
    return {"submit": {"hk": ENTITY, "seq": seq, "payload": payload},
            "receipt": {"t_recv_us": int(t * 1_000_000), "grid_t0_ms": int(t * 1000)}}


def _ticks(asset, start, end, open_px, close_px):
    rows = [{"a": asset, "t": (start - 60 + i) * 1000, "b": None, "k": None, "p": open_px} for i in range(0, 61, 5)]
    rows += [{"a": asset, "t": (end - 60 + i) * 1000, "b": None, "k": None, "p": close_px} for i in range(0, 61, 5)]
    return rows


def _rate_ticks(d):
    return [{"a": "TAOUSD", "t": (d - 60 + i) * 1000, "b": None, "k": None, "p": TAOUSD} for i in range(0, 61, 10)]


def _publish(root, entries, ticks, until):
    ws = list(range((DAY - 600) * 1000 // W * W, until * 1000 // W * W + W, W))
    by_w = {}
    for e in entries:
        by_w.setdefault(e["receipt"]["t_recv_us"] // 1000 // W * W, []).append(e)
    for w in ws:
        d = root / str(w)
        d.mkdir(parents=True, exist_ok=True)
        (d / "receipts.jsonl").write_text("".join(json.dumps(e) + "\n" for e in by_w.get(w, [])))
        (d / "ticks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ticks if w <= r["t"] < w + W))
    (root / "index.json").write_text(json.dumps({"windows": ws}))
    return "file://" + str(root)


def _db_after(tmp_path, base, chain, now, name="c"):
    cache = str(tmp_path / name)
    markets.sync_and_grade(base, cache, now, chain)
    return markets._db(cache)


# ── limits and pricing ───────────────────────────────────────────────────────
def test_v2_limits_and_liquidity_sensitive_lmsr(v2):
    assert markets.limits_for(S1) == (Decimal(1), Decimal(10000), Decimal(25000))
    assert markets.limits_for(DAY - 900)[1] == config.MARKETS_MAX_BET           # V1 untouched
    one = markets.replay([{"key": "a", "account": f"{ENTITY}_1", "side": "UP", "dollars": Decimal("1000.00"),
                           "order": (0, 1, ENTITY, 1)}], start=S1)
    p = markets.market_price_up(one, S1)
    assert Decimal("0.545") < p < Decimal("0.555")                              # ~5 points for $1,000
    with pytest.raises(markets.MarketError):
        markets.parse_dollars("10000.01", S1)
    assert markets.parse_dollars("10000.00", S1) == Decimal(10000)


def test_v1_pricing_is_unchanged_by_the_v2_code():
    bets = [{"key": k, "account": f"{ENTITY}_1", "side": s, "dollars": Decimal(d), "order": (0, i, ENTITY, i)}
            for i, (k, s, d) in enumerate([("a", "UP", "40.00"), ("b", "DOWN", "25.00")])]
    assert markets.replay(bets, Decimal(100)) == markets.replay(bets, start=None)


# ── collateral ───────────────────────────────────────────────────────────────
def test_collateral_cap_ignores_the_over_limit_bet(v2, tmp_path):
    # 100 alpha at 1.2 $/alpha = $120 of collateral: $50 + $50 fit, the third $50 does not.
    e1 = S1 + 900
    entries = [_entry(S1 - 600 + i * 30, i + 1, _payload(S1, dollars="50.00", n=i + 1)) for i in range(3)]
    ticks = _ticks("BTCUSD", S1, e1, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / "feed", entries, ticks, e1 + 900)
    db = _db_after(tmp_path, base, FakeChain(stake=Decimal(100)), e1 + 600)
    st = [s for (s,) in db.execute("SELECT c.status FROM coll c JOIN bets b ON b.key=c.key ORDER BY b.t_recv_us")]
    assert st == ["ok", "ok", "ignored:collateral"]
    res = dict(db.execute("SELECT key, status FROM results"))
    assert res[f"{ENTITY}:3"] == "ignored:collateral" and res[f"{ENTITY}:1"] == "ok"


def test_open_stake_is_released_when_its_market_ends(v2, tmp_path):
    # $100 in the 00:30 market; once it has ended, another $100 in the 01:00 market fits again.
    s2 = S1 + 1800
    entries = [_entry(S1 - 300, 1, _payload(S1, dollars="100.00")),
               _entry(s2 - 300, 2, _payload(s2, dollars="100.00", n=2))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 99.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / "feed", entries, ticks, s2 + 1800)
    db = _db_after(tmp_path, base, FakeChain(stake=Decimal(100)), s2 + 1500)
    assert [s for (s,) in db.execute("SELECT status FROM coll ORDER BY t_recv_us")] == ["ok", "ok"]


# ── daily P&L settlement and weights ─────────────────────────────────────────
def _two_sided_day(tmp_path, chain, up_dollars="100.00", down_dollars="100.00", name="feed"):
    e1 = S1 + 900
    entries = [_entry(S1 - 600, 1, _payload(S1, "UP", up_dollars, n=1)),
               _entry(S1 - 500, 2, _payload(S1, "DOWN", down_dollars, n=2))]
    ticks = _ticks("BTCUSD", S1, e1, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / name, entries, ticks, DAY + 86400 + 3600)
    return base, DAY + 86400 + 120


def test_cycle_winnings_become_the_entity_weight(v2, tmp_path):
    chain = FakeChain(stake=Decimal(10_000), aout=Decimal(100), burns={(DAY // 12 + 100, 2): [
        {"event": "AlphaBurned", "coldkey": "x", "hotkey": ENTITY, "amount": Decimal("1000"), "netuid": config.NETUID}]})
    base, now = _two_sided_day(tmp_path, chain)
    # the entity burns the loser's stake on chain and files the claim
    burn = {"kind": "mk.burn", "amount_alpha": str((Decimal(100) / RATE).quantize(Decimal("0.000000001"))),
            "block": DAY // 12 + 100, "ext_index": 2, "trade_pair": "TAOUSD"}
    entries_b = [_entry(DAY + 3600, 9, burn)]
    import json as _j
    w = (DAY + 3600) * 1000 // W * W
    p = tmp_path / "feed" / str(w) / "receipts.jsonl"
    p.write_text(p.read_text() + "".join(_j.dumps(e) + "\n" for e in entries_b))
    db = _db_after(tmp_path, base, chain, now)
    won, lost, burned, debt, payable = db.execute(
        "SELECT won, lost, burned, debt, payable FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone()
    assert Decimal(lost) == Decimal(100) / RATE                      # the loser's stake, in alpha
    assert Decimal(burned) == Decimal(burn["amount_alpha"]) and Decimal(debt) < Decimal("0.000001")
    assert Decimal(payable) == Decimal(won) > 0                       # burned, so the winnings are paid in full
    emission = Decimal(db.execute("SELECT emission FROM settle_meta WHERE cycle=?", (DAY,)).fetchone()[0])
    assert emission == Decimal(3600 // 12) * Decimal(100) * Decimal("0.41") * Decimal("0.8") * Decimal("0.125")
    now = DAY + 3600 + 60 + 1                                         # the cycle after it: its vector
    vec = markets.pnl_vector(db, {ENTITY: 7}, now)
    assert vec[7] == pytest.approx(float(Decimal(payable) / emission))
    assert vec[7] + vec[config.BURN_UID] == pytest.approx(1.0)


def test_unburned_losses_past_the_deadline_come_out_of_winnings(v2, tmp_path):
    # cycle DAY: one win, one loss, nothing burned -> paid in full, the loss is debt
    s2 = DAY + 86400 + 1800                                           # a day later: past the deadline
    entries = [_entry(S1 - 600, 1, _payload(S1, "UP", "100.00", n=1)),
               _entry(S1 - 500, 2, _payload(S1, "DOWN", "100.00", n=2)),
               _entry(s2 - 600, 3, _payload(s2, "UP", "300.00", n=3))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400) + _rate_ticks(DAY + 2 * 86400))
    base = _publish(tmp_path / "feed", entries, ticks, DAY + 86400 + 7200)
    chain = FakeChain(stake=Decimal(10_000))
    db = _db_after(tmp_path, base, chain, DAY + 86400 + 3600 + 120)
    won, lost, deducted, debt, payable = (Decimal(x) for x in db.execute(
        "SELECT won, lost, deducted, debt, payable FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone())
    assert deducted == 0 and payable == won and debt == lost        # inside the deadline: nothing taken yet
    assert markets.debt_as_of(db, ENTITY, DAY + 3600 + 61) == lost   # but it reduces collateral
    c2 = DAY + 86400
    won2, deducted2, debt2, payable2 = (Decimal(x) for x in db.execute(
        "SELECT won, deducted, debt, payable FROM settle WHERE cycle=? AND entity=?", (c2, ENTITY)).fetchone())
    assert deducted2 == min(won2, lost) and payable2 == won2 - deducted2 and debt2 == lost - deducted2


def test_a_hedged_pair_in_one_entity_nets_nothing_after_the_burn(v2, tmp_path):
    # same entity, opposite sides, same stake: what it is emitted never exceeds what it burns
    base, now = _two_sided_day(tmp_path, FakeChain(stake=Decimal(10_000)))
    db = _db_after(tmp_path, base, FakeChain(stake=Decimal(10_000)), now)
    won, lost = (Decimal(x) for x in db.execute(
        "SELECT won, lost FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone())
    assert won - lost <= Decimal("0.000001")                         # LMSR price impact makes it < 0


def test_weights_are_cut_pro_rata_past_the_cycles_share(v2, tmp_path):
    chain = FakeChain(stake=Decimal(10**9), aout=Decimal("0.000001"))   # a tiny day: winnings exceed it
    base, now = _two_sided_day(tmp_path, chain, up_dollars="10000.00", down_dollars="1.00")
    db = _db_after(tmp_path, base, chain, now)
    vec = markets.pnl_vector(db, {ENTITY: 7}, DAY + 3600 + 61)       # the vector right after its cycle
    assert vec[7] == pytest.approx(1.0) and vec.get(config.BURN_UID, 0) == pytest.approx(0.0)


def test_fresh_and_incremental_validators_agree(v2, tmp_path):
    chain = FakeChain(stake=Decimal(10_000))
    base, now = _two_sided_day(tmp_path, chain)
    a = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "fresh"), chain)
    markets.markets_weights({ENTITY: 7}, S1 + 1200, base, str(tmp_path / "inc"), chain)
    markets.markets_weights({ENTITY: 7}, DAY + 86400 + 30, base, str(tmp_path / "inc"), chain)
    b = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "inc"), chain)
    assert a == b


def test_before_the_v2_arm_the_skill_vector_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MARKETS_COLLATERAL_FROM_UNIX", DAY + 10 * 86400)   # arms later
    assert not markets.is_v2(S1) and markets.limits_for(S1)[1] == config.MARKETS_MAX_BET
    assert not config.markets_collateral_as_of(S1)


def test_testnet_env_overrides_the_v2_arm():
    code = "from sn89_signals import config; print(config.MARKETS_COLLATERAL_FROM_UNIX)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "SN89_MARKETS_COLLATERAL_FROM": "123"}, cwd=os.getcwd())
    assert out.stdout.strip() == "123"


def test_burn_claims_are_valid_only_once_v2_is_armed(v2):
    p = {"kind": "mk.burn", "amount_alpha": "1.5", "block": 10, "ext_index": 1, "trade_pair": "TAOUSD"}
    markets.validate_entry(p, ENTITY, DAY + 10)
    with pytest.raises(hf.HFRejected):
        markets.validate_entry(p, ENTITY, DAY - 10)
    with pytest.raises(hf.HFRejected):
        markets.validate_entry({**p, "amount_alpha": "-1"}, ENTITY, DAY + 10)
    assert not hf.is_hf_call(p)


def test_one_extrinsic_is_credited_once_and_a_burn_inside_the_deadline_costs_nothing(v2, tmp_path):
    amt = (Decimal(100) / RATE).quantize(Decimal("0.000000001"))
    blk = (DAY + 7200 + 30) // 12                       # burned two hours after the loss
    chain = FakeChain(stake=Decimal(10_000), burns={(blk, 3): [
        {"event": "AlphaRecycled", "coldkey": "x", "hotkey": ENTITY, "amount": amt, "netuid": config.NETUID}]})
    base, now = _two_sided_day(tmp_path, chain)
    claim = {"kind": "mk.burn", "amount_alpha": str(amt), "block": blk, "ext_index": 3, "trade_pair": "TAOUSD"}
    t = DAY + 7200 + 40
    w = t * 1000 // W * W
    p = tmp_path / "feed" / str(w) / "receipts.jsonl"
    p.write_text(p.read_text() + "".join(json.dumps(_entry(t + i, 20 + i, claim)) + "\n" for i in range(2)))
    db = _db_after(tmp_path, base, chain, DAY + 86400 + 900)
    burned = sum(Decimal(b) for (b,) in db.execute("SELECT burned FROM settle WHERE entity=?", (ENTITY,)))
    assert burned == amt                                 # two claims, one extrinsic: credited once
    won, payable = db.execute("SELECT won, payable FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone()
    assert Decimal(payable) == Decimal(won)
    assert markets.debt_as_of(db, ENTITY, DAY + 86400 + 900) == 0
