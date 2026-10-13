"""Markets V3.2 (Whit 2026-10-12): finite house depth. An account's and its entity's same-side stake
across every market of one window+start fills flat up to an allowance; past it the price walks."""
from decimal import Decimal

import pytest

from sn89_signals import config, markets
from tests.test_markets_v3 import ENTITY, SIG, _bet, _series, _start

OTHER = "5CDPZpc5Wq3cWuq3zZ3oCjXv6Qe9F3DdZ7ywqF8f3oJ4hDUd"


@pytest.fixture
def depth(v3, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_DEPTH_FROM_UNIX", 1)
    monkeypatch.setattr(config, "MARKETS_DEPTH_B", Decimal("300"))
    monkeypatch.setattr(config, "MARKETS_DEPTH_ACCOUNT", Decimal("100"))
    monkeypatch.setattr(config, "MARKETS_DEPTH_ENTITY", Decimal("400"))
    return config


from tests.test_markets_v3 import v3  # noqa: E402,F401  (fixture)


def _ctx(p="0.10", depth=(Decimal(0), Decimal(0))):
    return {"p": Decimal(p), "spread": Decimal("0.03"), "depth": depth}


def test_gate_is_by_arm_and_window(depth, monkeypatch):
    s = _start()
    assert markets.uses_depth(markets.market_id("BTCUSD", "15m", s))
    assert markets.uses_depth(markets.market_id("BTCUSD", "1h", s // 3600 * 3600))
    assert not markets.uses_depth(markets.market_id("BTCUSD", "1d", 1791666000))
    monkeypatch.setattr(config, "MARKETS_DEPTH_FROM_UNIX", s + 900)
    assert not markets.uses_depth(markets.market_id("BTCUSD", "15m", s))
    monkeypatch.setattr(config, "MARKETS_DEPTH_FROM_UNIX", 0)
    assert not markets.uses_depth(markets.market_id("BTCUSD", "15m", s))


def test_inside_the_allowance_the_price_is_flat(depth):
    p_flat = Decimal("0.13")
    sh = markets.depth_shares(Decimal("50"), "DOWN", p_flat, _ctx("0.90"))
    assert sh == (Decimal("50") / p_flat).quantize(markets.SHARE_Q, rounding="ROUND_DOWN")


def test_past_the_allowance_each_dollar_buys_less_and_more_stake_buys_even_less(depth):
    p_flat = Decimal("0.13")
    flat = Decimal("50") / p_flat
    half = markets.depth_shares(Decimal("50"), "UP", p_flat, _ctx("0.10", (Decimal("75"), Decimal("75"))))
    full = markets.depth_shares(Decimal("50"), "UP", p_flat, _ctx("0.10", (Decimal("150"), Decimal("150"))))
    deeper = markets.depth_shares(Decimal("50"), "UP", p_flat, _ctx("0.10", (Decimal("600"), Decimal("600"))))
    assert flat > half > full > deeper > 0
    # the entity allowance binds across accounts: a fresh account of a loaded entity walks too
    ent = markets.depth_shares(Decimal("50"), "UP", p_flat, _ctx("0.10", (Decimal("0"), Decimal("450"))))
    assert ent < flat


def test_prior_counts_same_side_stake_across_the_group_per_account_and_entity():
    o = lambda t: (0, t, "h", 0)
    rows = [("a", f"{ENTITY}_1", "UP", Decimal("60"), o(1)),
            ("b", f"{ENTITY}_2", "UP", Decimal("30"), o(2)),       # same entity, other account
            ("c", f"{ENTITY}_1", "DOWN", Decimal("40"), o(3)),     # other side: not counted for UP
            ("d", f"{OTHER}_1", "UP", Decimal("500"), o(4)),       # other entity: not counted
            ("e", f"{ENTITY}_1", "UP", Decimal("10"), o(5))]
    pr = markets.depth_prior(list(reversed(rows)))                 # order comes from the order key
    assert pr["a"] == (0, 0)
    assert pr["b"] == (0, Decimal("60"))
    assert pr["c"] == (0, 0)
    assert pr["e"] == (Decimal("60"), Decimal("90"))


def _grade(tmp_path, name, bets_by_mid, rows_by_asset):
    db = markets._db(str(tmp_path / name))
    i = 0
    for mid, bets in bets_by_mid.items():
        for b in bets:
            db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                       (b["key"], mid, b["account"], b["side"], str(b["dollars"]), b["order"][0], b["order"][1],
                        ENTITY, i))
            i += 1
    for mid in bets_by_mid:
        markets.grade_market(db, mid, rows_by_asset[markets.parse_market_id(mid)[0]], SIG)
    return {r[0]: Decimal(r[4]) for r in db.execute("SELECT * FROM results")}


def test_a_second_market_of_the_same_start_walks_the_price(depth, tmp_path, monkeypatch):
    s = _start(); e = s + 900
    btc, eth = markets.market_id("BTCUSD", "15m", s), markets.market_id("ETHUSD", "15m", s)
    rows = {"BTCUSD": _series(s, e, lambda t: 100.0, asset="BTCUSD"),
            "ETHUSD": _series(s, e, lambda t: 50.0, asset="ETHUSD")}
    bets = {btc: [_bet("b1", s + 10, "UP", "80.00", 1, 1)],
            eth: [_bet("e1", s + 20, "UP", "80.00", 1, 2), _bet("e2", s + 30, "DOWN", "20.00", 1, 3)]}
    on = _grade(tmp_path, "on", bets, rows)
    again = _grade(tmp_path, "again", bets, rows)
    monkeypatch.setattr(config, "MARKETS_DEPTH_FROM_UNIX", 0)
    off = _grade(tmp_path, "off", bets, rows)
    assert on == again                                      # deterministic
    assert on["b1"] == off["b1"]                            # first $80 of the group: inside the $100
    assert on["e1"] < off["e1"]                             # $60 of the second market past the allowance
    assert on["e2"] == off["e2"]                            # the other side has its own allowance


def test_depth_ends_for_markets_starting_at_the_until_arm(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_DEPTH_FROM_UNIX", 1)
    s = 1_800_000_000 // 900 * 900
    monkeypatch.setattr(config, "MARKETS_DEPTH_UNTIL_UNIX", s)
    assert markets.uses_depth(markets.market_id("BTCUSD", "15m", s - 900))
    assert not markets.uses_depth(markets.market_id("BTCUSD", "15m", s))
    assert not markets.uses_depth(markets.market_id("BTCUSD", "1h", s // 3600 * 3600 + 3600))
    monkeypatch.setattr(config, "MARKETS_DEPTH_UNTIL_UNIX", 0)
    assert markets.uses_depth(markets.market_id("BTCUSD", "15m", s))
