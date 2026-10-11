"""Markets V2d: an entity short of its open bets at any checkpoint of a cycle is paid nothing for it."""
from decimal import Decimal

from sn89_signals import config, markets
from tests.test_markets_v2 import (DAY, ENTITY, S1, FakeChain, _db_after, _payload, _publish,  # noqa: F401
                                   _rate_ticks, _ticks, v2)
from tests.test_markets_v2b import _entry_for, v2b  # noqa: F401

D = Decimal


def _win(tmp_path, stake, lock_from, name):
    # $100 UP received 00:20, market 00:30-00:45 wins. Settlement cycles are 1 h (v2 fixture).
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", "100.00"))]
    ticks = _ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / f"feed{name}", entries, ticks, DAY + 86400 + 3600)
    config.MARKETS_LOCK_FROM_UNIX = lock_from
    db = _db_after(tmp_path, base, FakeChain(stake=stake, aout=D(100)), DAY + 86400 + 120, name=name)
    return db.execute("SELECT payable, lock_breach, rcum, hwm, won_ret FROM settle WHERE entity=? AND cycle=?",
                      (ENTITY, DAY)).fetchone()


def _pulled(at):
    """Stake 10,000 alpha until `at`, then 0 (the owner unstaked)."""
    return lambda b: D(10_000) if b * 12 < at else D(0)


def test_unstaking_while_a_bet_is_open_forfeits_the_cycle(v2b, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_LOCK_FROM_UNIX", 0)
    kept = _win(tmp_path, D(10_000), DAY, "kept")
    pulled = _win(tmp_path, _pulled(S1 - 300), DAY, "pulled")      # unstaked at 00:25, bet open to 00:45
    assert D(kept[0]) > 0 and kept[1] == 0
    assert D(pulled[0]) == 0 and pulled[1] == S1                    # first short checkpoint: 00:30
    # carry still advances: the forfeited win is not paid in a later cycle either
    assert (pulled[2], pulled[3], pulled[4]) == (kept[2], kept[3], kept[4])


def test_unstaking_after_the_market_ended_is_not_a_breach(v2b, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_LOCK_FROM_UNIX", 0)
    after = _win(tmp_path, _pulled(S1 + 900 + 60), DAY, "after")    # unstaked 00:46, market ended 00:45
    assert D(after[0]) > 0 and after[1] == 0


def test_cycles_before_the_arm_are_unchanged(v2b, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_LOCK_FROM_UNIX", 0)
    off = _win(tmp_path, _pulled(S1 - 300), 0, "off")
    later = _win(tmp_path, _pulled(S1 - 300), DAY + 3600, "later")
    assert D(off[0]) > 0 and off[1] == 0
    assert later[:2] == off[:2]


def test_open_alpha_counts_a_market_ending_at_the_checkpoint(tmp_path):
    db = markets._db(str(tmp_path))
    mid = markets.market_id("BTCUSD", "15m", S1)
    db.execute("INSERT INTO bets (key, market_id, account, dollars, w, t_recv_us, hk, seq) VALUES (?,?,?,?,?,?,?,?)",
               ("k", mid, ENTITY, "100", 0, (S1 - 600) * 1_000_000, ENTITY, 1))
    db.execute("INSERT INTO coll VALUES (?,?,?,?,?)", ("k", ENTITY, "ok", "80", (S1 - 600) * 1_000_000))
    assert markets.open_alpha_at(db, ENTITY, S1 - 600) == 0          # received at, not before
    assert markets.open_alpha_at(db, ENTITY, S1 + 900) == 80         # ends exactly then: still owed
    assert markets.open_alpha_at(db, ENTITY, S1 + 900 + 1) == 0
