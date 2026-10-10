"""Markets V2c: retained net is carried forward per entity and paid only above its high-water mark."""
from decimal import Decimal

import pytest

from sn89_signals import config, markets
from tests.test_markets_v2 import (DAY, ENTITY, RATE, S1, FakeChain, _db_after, _payload, _publish,  # noqa: F401
                                   _rate_ticks, _ticks, v2)
from tests.test_markets_v2b import _entry_for, v2b  # noqa: F401

D = Decimal


def test_carry_step_pays_only_above_the_high_water_mark():
    r, h, p = markets.carry_step(D(0), D(0), D(10))
    assert (r, h, p) == (10, 10, 10)                     # a first win is paid
    r, h, p = markets.carry_step(r, h, D(-25))
    assert (r, h, p) == (-15, 10, 0)                     # a loss is kept and carried
    r, h, p = markets.carry_step(r, h, D(20))
    assert (r, h, p) == (5, 10, 0)                       # winning it back pays nothing
    r, h, p = markets.carry_step(r, h, D(8))
    assert (r, h, p) == (13, 13, 3)                      # only what is above the old high


def test_a_coin_flip_self_dealer_is_paid_only_its_net_over_time():
    # The free option V2b left open: win, lose, win, lose of equal size. V2b paid every win.
    flips = [D(50), D(-50)] * 10
    r = h = D(0)
    paid = D(0)
    for n in flips:
        r, h, p = markets.carry_step(r, h, n)
        paid += p
    assert paid == 50 and sum(max(D(0), n) for n in flips) == 500


def _loss_then_win(tmp_path, carry_from):
    # cycle DAY: $100 UP loses; cycle DAY+3600: $100 UP wins. Same entity, one account.
    s2 = DAY + 3600 + 1800
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", "100.00")),
               _entry_for(ENTITY, s2 - 600, 2, _payload(s2, "UP", "100.00"))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 99.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / "feed", entries, ticks, DAY + 86400 + 3600)
    chain = FakeChain(stake=D(10_000), aout=D(100))
    config.MARKETS_CARRY_FROM_UNIX = carry_from
    db = _db_after(tmp_path, base, chain, DAY + 86400 + 120, name=f"c{carry_from}")
    return {c: (D(w), D(l), D(p), D(r), D(h)) for c, w, l, p, r, h in db.execute(
        "SELECT cycle, won_ret, lost_ret, payable, rcum, hwm FROM settle WHERE entity=? ORDER BY cycle", (ENTITY,))}


def test_settlement_carries_a_loss_into_the_next_cycle(v2b, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_CARRY_FROM_UNIX", 0)
    off = _loss_then_win(tmp_path, 0)
    on = _loss_then_win(tmp_path, DAY)
    c1, c2 = DAY, DAY + 3600
    assert off[c1][1] == D(100) / RATE and off[c2][0] > 0
    assert off[c2][2] == off[c2][0]                               # V2b: the win is paid in full
    assert on[c1][2] == 0 and on[c1][3] == -off[c1][1]            # V2c: the loss is carried...
    assert on[c2][2] == max(D(0), on[c1][3] + on[c2][0])          # ...and netted against the win
    assert on[c2][2] < off[c2][2]
    # cycles before the arm settle exactly as V2b and carry nothing
    mid = _loss_then_win(tmp_path, c2)
    assert mid[c1][2:] == (off[c1][2], 0, 0) and mid[c2][2] == off[c2][2]
