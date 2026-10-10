"""Markets V2b (Whit 2026-10-10): lost stakes stay with the entity; emission on net entity P&L."""
import json
from decimal import Decimal

import pytest

from sn89_signals import config, hf, markets
from tests.test_markets_v2 import (DAY, ENTITY, RATE, S1, W, FakeChain, _db_after, _entry, _payload, _publish,
                                   _rate_ticks, _ticks, _two_sided_day, v2)  # noqa: F401

ENTITY_B = "5FjFwQuTbKhEqoKvbgMTsHxp3yLq5xJc8y1nZcB6rrJUwrmL"


@pytest.fixture
def v2b(v2, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_RETAIN_FROM_UNIX", DAY)          # every V2 market here is retained
    return config


def _entry_for(hk, t, seq, payload):
    return {"submit": {"hk": hk, "seq": seq, "payload": payload},
            "receipt": {"t_recv_us": int(t * 1_000_000), "grid_t0_ms": int(t * 1000)}}


def test_before_the_stamp_v2_settles_byte_for_byte_as_before(v2, monkeypatch, tmp_path):
    chain = FakeChain(stake=Decimal(10_000))
    base, now = _two_sided_day(tmp_path, chain)
    monkeypatch.setattr(config, "MARKETS_RETAIN_FROM_UNIX", 0)
    off = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "off"), chain)
    db_off = markets._db(str(tmp_path / "off"))
    row_off = db_off.execute("SELECT won, lost, debt, payable FROM settle WHERE cycle=? AND entity=?",
                             (DAY, ENTITY)).fetchone()
    monkeypatch.setattr(config, "MARKETS_RETAIN_FROM_UNIX", DAY + 10 * 86400)      # arms later
    later = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "later"), chain)
    db_l = markets._db(str(tmp_path / "later"))
    row_l = db_l.execute("SELECT won, lost, debt, payable FROM settle WHERE cycle=? AND entity=?",
                         (DAY, ENTITY)).fetchone()
    assert off == later and row_off == row_l
    assert Decimal(row_l[1]) > 0 and Decimal(row_l[2]) > 0           # pre-stamp: the loss is still debt
    assert not markets.is_retained(S1)


def test_a_wash_pair_inside_one_entity_earns_nothing_and_owes_nothing(v2b, tmp_path):
    chain = FakeChain(stake=Decimal(10_000))
    base, now = _two_sided_day(tmp_path, chain)
    db = _db_after(tmp_path, base, chain, now)
    won, lost, won_r, lost_r, debt, payable = (Decimal(x) for x in db.execute(
        "SELECT won, lost, won_ret, lost_ret, debt, payable FROM settle WHERE cycle=? AND entity=?",
        (DAY, ENTITY)).fetchone())
    assert won == 0 and lost == 0                                     # nothing under the burn rule
    assert lost_r == Decimal(100) / RATE and won_r > 0 and won_r - lost_r <= 0   # LMSR impact: net <= 0
    assert payable == 0 and debt == 0                                 # earns nothing, owes nothing
    assert markets.debt_as_of(db, ENTITY, DAY + 3600 + 61) == 0
    vec = markets.pnl_vector(db, {ENTITY: 7}, DAY + 3600 + 61)
    assert vec.get(7, 0.0) == 0.0 and vec[config.BURN_UID] == pytest.approx(1.0)


def test_two_entities_split_the_cycle_by_net_profit(v2b, tmp_path):
    # A is long $100 and wins; B is short $100 and loses. A is paid its net, B nothing, B owes nothing.
    e1 = S1 + 900
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", "100.00", n=1)),
               _entry_for(ENTITY_B, S1 - 500, 1, {**_payload(S1, "DOWN", "100.00", n=1), "account": f"{ENTITY_B}_1"})]
    ticks = _ticks("BTCUSD", S1, e1, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / "feed", entries, ticks, DAY + 86400 + 3600)
    chain = FakeChain(stake=Decimal(10_000), aout=Decimal(100))
    now = DAY + 86400 + 120
    db = _db_after(tmp_path, base, chain, now)
    a = {k: Decimal(v) for k, v in zip(("won_ret", "lost_ret", "payable", "debt"), db.execute(
        "SELECT won_ret, lost_ret, payable, debt FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone())}
    b = {k: Decimal(v) for k, v in zip(("won_ret", "lost_ret", "payable", "debt"), db.execute(
        "SELECT won_ret, lost_ret, payable, debt FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY_B)).fetchone())}
    assert a["payable"] == a["won_ret"] > 0 and a["lost_ret"] == 0
    assert b["lost_ret"] == Decimal(100) / RATE and b["payable"] == 0 and b["debt"] == 0
    emission = Decimal(db.execute("SELECT emission FROM settle_meta WHERE cycle=?", (DAY,)).fetchone()[0])
    vec = markets.pnl_vector(db, {ENTITY: 7, ENTITY_B: 8}, DAY + 3600 + 61)
    assert vec[7] == pytest.approx(float(a["payable"] / emission)) and vec.get(8, 0.0) == 0.0


def test_a_retained_loss_does_not_reduce_collateral(v2b, tmp_path):
    # 100 alpha of collateral = $120. Lose $100 in the 00:30 market; a $100 bet in the next hour's
    # market still fits, because the loss is kept, not owed. Under V2 it would be ignored:collateral.
    s2 = S1 + 3600
    entries = [_entry(S1 - 300, 1, _payload(S1, "UP", "100.00", n=1)),
               _entry(s2 - 300, 2, _payload(s2, "UP", "100.00", n=2))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 99.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / "feed", entries, ticks, s2 + 3600)
    db = _db_after(tmp_path, base, FakeChain(stake=Decimal(100)), s2 + 2400)
    assert [s for (s,) in db.execute("SELECT status FROM coll ORDER BY t_recv_us")] == ["ok", "ok"]
    assert markets.debt_as_of(db, ENTITY, s2) == 0


def test_a_burn_claim_after_the_stamp_is_credited_but_changes_nothing(v2b, tmp_path):
    amt = (Decimal(100) / RATE).quantize(Decimal("0.000000001"))
    blk = (DAY + 7200 + 30) // 12
    chain = FakeChain(stake=Decimal(10_000), burns={(blk, 3): [
        {"event": "AlphaBurned", "coldkey": "x", "hotkey": ENTITY, "amount": amt, "netuid": config.NETUID}]})
    base, now = _two_sided_day(tmp_path, chain)
    claim = {"kind": "mk.burn", "amount_alpha": str(amt), "block": blk, "ext_index": 3, "trade_pair": "TAOUSD"}
    markets.validate_entry(claim, ENTITY, DAY + 7200 + 40)             # still accepted
    t = DAY + 7200 + 40
    w = t * 1000 // W * W
    p = tmp_path / "feed" / str(w) / "receipts.jsonl"
    p.write_text(p.read_text() + json.dumps(_entry(t, 20, claim)) + "\n")
    db = _db_after(tmp_path, base, chain, DAY + 86400 + 900)
    burned = sum(Decimal(b) for (b,) in db.execute("SELECT burned FROM settle WHERE entity=?", (ENTITY,)))
    assert burned == amt                                              # credited as a burn credit...
    for (payable,) in db.execute("SELECT payable FROM settle WHERE entity=?", (ENTITY,)):
        assert Decimal(payable) == 0                                  # ...but a wash pair still earns nothing
    assert markets.debt_as_of(db, ENTITY, DAY + 86400 + 900) == 0


def test_fresh_and_incremental_validators_agree_under_retention(v2b, tmp_path):
    chain = FakeChain(stake=Decimal(10_000))
    base, now = _two_sided_day(tmp_path, chain)
    a = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "fresh"), chain)
    markets.markets_weights({ENTITY: 7}, S1 + 1200, base, str(tmp_path / "inc"), chain)
    markets.markets_weights({ENTITY: 7}, DAY + 86400 + 30, base, str(tmp_path / "inc"), chain)
    b = markets.markets_weights({ENTITY: 7}, now, base, str(tmp_path / "inc"), chain)
    assert a == b


def test_stamp_is_in_source_and_env_overrides_it(monkeypatch):
    assert config.MARKETS_RETAIN_FROM_UNIX == 1791601200 or config.MARKETS_RETAIN_FROM_UNIX == 0 \
        or "SN89_MARKETS_RETAIN_FROM" in __import__("os").environ
    monkeypatch.setattr(config, "MARKETS_RETAIN_FROM_UNIX", 100)
    assert markets.is_retained(100) and not markets.is_retained(99)
