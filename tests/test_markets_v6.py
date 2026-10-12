"""Markets V6 (Whit 2026-10-12): payout-based collateral check + emission credit, daily settlement,
owed held while the entity's stake is under its lock."""
from decimal import Decimal

import pytest

from sn89_signals import config, markets
from tests.test_markets_v2 import (DAY, ENTITY, RATE, S1, FakeChain, _db_after, _payload, _publish,  # noqa: F401
                                   _rate_ticks, _ticks, v2)
from tests.test_markets_v2b import ENTITY_B, _entry_for, v2b  # noqa: F401

D = Decimal
H = 3600
NOW = DAY + 86400 + 120


@pytest.fixture
def v6(v2b, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_CARRY_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_OWED_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_LOCK_FROM_UNIX", 0)
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", 0)
    monkeypatch.setattr(config, "MARKETS_DAILY_FROM_UNIX", 0)
    monkeypatch.setattr(config, "MARKETS_HOLD_FROM_UNIX", 0)
    return config


def _one_bet(tmp_path, name, stake, aout, dollars="100.00"):
    """$100 UP received 00:20 on the 00:30-00:45 market (an empty 50/50 book: pays ~$199), which wins."""
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", dollars))]
    ticks = _ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / f"feed{name}", entries, ticks, NOW)
    return _db_after(tmp_path, base, FakeChain(stake=D(stake), aout=D(aout)), NOW, name=name)


# ── (a) the bet must fit its maximum payout ──────────────────────────────────
def test_payout_over_the_pot_is_refused_where_the_stake_check_passed(v6, tmp_path, monkeypatch):
    # 100 alpha posted. Stake $100 = 83 alpha fits; the payout (~$199 = ~166 alpha) does not, and a
    # near-zero emission credit (aout 0.001 -> ~0.3 alpha a day) cannot cover the gap.
    off = _one_bet(tmp_path, "off", 100, "0.001")
    assert off.execute("SELECT status FROM coll").fetchone()[0] == "ok"
    assert off.execute("SELECT COUNT(*) FROM coll_pay").fetchone()[0] == 0        # arm off: nothing new
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", DAY)
    on = _one_bet(tmp_path, "on", 100, "0.001")
    assert on.execute("SELECT status FROM coll").fetchone()[0] == "ignored:collateral"
    pay = D(on.execute("SELECT payout FROM coll_pay").fetchone()[0])
    assert D(160) < pay < D(170)


def test_payout_is_exactly_what_the_grader_pays(v6, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", DAY)
    db = _one_bet(tmp_path, "exact", 10_000, "0.001")
    key, pay = db.execute("SELECT key, payout FROM coll_pay").fetchone()
    shares = D(db.execute("SELECT shares FROM results WHERE key=?", (key,)).fetchone()[0])
    assert shares > 0
    assert abs(D(pay) * RATE - shares) < D("1e-20")


def _two_bets(tmp_path, name, stake, aout):
    """$10 UP at 00:20 (makes the entity active before 01:00), then $100 UP at 01:20 on 01:30-01:45."""
    s2 = DAY + H + 1800
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", "10.00")),
               _entry_for(ENTITY, s2 - 600, 2, _payload(s2, "UP", "100.00"))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / f"feed{name}", entries, ticks, NOW)
    db = _db_after(tmp_path, base, FakeChain(stake=D(stake), aout=D(aout)), NOW, name=name)
    return db, db.execute("SELECT c.status FROM coll c JOIN bets b ON b.key=c.key ORDER BY b.t_recv_us").fetchall()


def test_emission_credit_lets_a_collateralized_entity_exceed_its_pot(v6, tmp_path, monkeypatch):
    # aout 1 -> one day of Markets emission = 7200 blocks x 1 x 0.41 x 0.8 x 0.125 = 295.2 alpha, all of
    # it this entity's (the only one active before 01:00), so 100 posted + 295 credit covers the ~166
    # alpha payout of the $100 bet. With ~0.3 alpha of credit it does not.
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", DAY)
    db, st = _two_bets(tmp_path, "credit", 100, "1")
    assert [r[0] for r in st] == ["ok", "ok"]
    pool = D(db.execute("SELECT credit FROM coll_credit WHERE hour=? AND entity=''", (DAY + H,)).fetchone()[0])
    assert pool == D("295.2")
    assert D(db.execute("SELECT credit FROM coll_credit WHERE hour=? AND entity=?", (DAY + H, ENTITY)).fetchone()[0]) == pool
    _db2, st2 = _two_bets(tmp_path, "nocredit", 100, "0.001")
    assert [r[0] for r in st2] == ["ok", "ignored:collateral"]


def test_first_hour_of_an_entity_gets_no_credit(v6, tmp_path, monkeypatch):
    # active means it filed a frame BEFORE the hour snapshot, so the credit cannot depend on bets
    # that are still arriving: the 00:20 bet sees no credit even with a large emission
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", DAY)
    db = _one_bet(tmp_path, "first", 100, "1")
    assert db.execute("SELECT status FROM coll").fetchone()[0] == "ignored:collateral"


class TwoStakes(FakeChain):
    def entity_stake(self, hk, b):
        return D(300) if hk == ENTITY else D(100)


def test_credit_is_split_by_posted_collateral(v6, tmp_path):
    db = markets._db(str(tmp_path))
    hour = DAY + 5 * H
    db.execute("INSERT INTO settle_meta VALUES (?, '0')", (DAY + 3 * H,))       # the cycle closed by then
    for i, hk in enumerate((ENTITY, ENTITY_B)):
        db.execute("INSERT INTO bets (key, market_id, account, side, dollars, w, t_recv_us, hk, seq) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (f"k{i}", "m", hk, "UP", "1", 0, (hour - 60) * 1_000_000, hk, i))
    ch = markets.CachedChain(db, TwoStakes(aout=D(1)))
    cr = markets.emission_credit(db, ch, hour)
    assert cr[ENTITY] == D("221.4") and cr[ENTITY_B] == D("73.8")                   # 3:1 of 295.2
    assert markets.emission_credit(db, ch, hour) == cr                              # cached, same answer


def test_credit_waits_for_the_closed_cycle(v6, tmp_path):
    db = markets._db(str(tmp_path))
    assert markets.emission_credit(db, markets.CachedChain(db, FakeChain(aout=D(1))), DAY + 5 * H) is None


def test_an_entity_with_nothing_posted_gets_no_credit(v6, tmp_path):
    db = markets._db(str(tmp_path))
    db.execute("INSERT INTO settle_meta VALUES (?, '0')", (DAY + 3 * H,))
    db.execute("INSERT INTO bets (key, market_id, account, side, dollars, w, t_recv_us, hk, seq) "
               "VALUES ('k','m',?, 'UP','1',0,?,?,0)", (ENTITY, (DAY + 5 * H - 60) * 1_000_000, ENTITY))
    cr = markets.emission_credit(db, markets.CachedChain(db, FakeChain(stake=D(0))), DAY + 5 * H)
    assert cr == {}


# ── (b) daily settlement ─────────────────────────────────────────────────────
def _win_then_loss(tmp_path, name, chain=None, now=NOW, extra_until=None):
    """$100 UP wins 00:30-00:45; $100 UP loses 03:30-03:45 (same entity, same day)."""
    s2 = DAY + 3 * H + 1800
    entries = [_entry_for(ENTITY, S1 - 600, 1, _payload(S1, "UP", "100.00")),
               _entry_for(ENTITY, s2 - 600, 2, _payload(s2, "UP", "100.00"))]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _ticks("BTCUSD", s2, s2 + 900, 101.0, 100.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / f"feed{name}", entries, ticks, extra_until or now)
    return _db_after(tmp_path, base, chain or FakeChain(stake=D(10_000), aout=D("0.001")), now, name=name)


def test_daily_close_nets_the_cycles_of_the_day(v6, tmp_path, monkeypatch):
    hourly = _win_then_loss(tmp_path, "hourly")
    rows = hourly.execute("SELECT cycle, payable, won_ret, lost_ret FROM settle WHERE entity=? AND cycle < ? "
                          "ORDER BY cycle", (ENTITY, DAY + 86400)).fetchall()
    paid_hourly = sum(D(r[1]) for r in rows)
    assert paid_hourly > 0                                     # the 00:30 win paid in its own hour
    monkeypatch.setattr(config, "MARKETS_DAILY_FROM_UNIX", DAY)
    daily = _win_then_loss(tmp_path, "daily")
    d = daily.execute("SELECT cycle, payable, won_ret, lost_ret FROM settle WHERE entity=?", (ENTITY,)).fetchall()
    assert [r[0] for r in d] == [DAY]                          # one cycle for the whole UTC day
    assert D(d[0][2]) == sum(D(r[2]) for r in rows) and D(d[0][3]) == sum(D(r[3]) for r in rows)
    assert D(d[0][1]) == 0                                     # the day netted negative: nothing payable


def test_the_tempo_cycle_holding_the_daily_arm_is_cut_short_at_it(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_COLLATERAL_FROM_UNIX", 1000)
    monkeypatch.setattr(config, "MARKETS_SETTLE_PERIOD_S", 4320)
    monkeypatch.setattr(config, "MARKETS_SETTLE_GRACE_S", 900)
    Dd = 20 * 86400
    monkeypatch.setattr(config, "MARKETS_DAILY_FROM_UNIX", 0)
    before = {t: (markets._cycle(t), markets._closed_by(t)) for t in range(Dd - 20_000, Dd + 3 * 86400, 777)}
    # arm off: exactly the old arithmetic
    assert all(c == 1000 + (t - 1000) // 4320 * 4320 and cb == markets._cycle(t - 900 - 4320)
               for t, (c, cb) in before.items())
    monkeypatch.setattr(config, "MARKETS_DAILY_FROM_UNIX", Dd)
    c0 = markets._cycle(Dd - 1)
    assert c0 < Dd < c0 + 4320 and markets._cycle_end(c0) == Dd     # last tempo cycle ends at the arm
    assert markets._cycle(Dd) == Dd and markets._cycle_end(Dd) == Dd + 86400
    assert markets._prev_cycle(Dd) == c0 and markets._prev_cycle(Dd + 86400) == Dd
    assert markets._closed_by(Dd + 899) == markets._prev_cycle(c0)  # not closed until end + grace
    assert markets._closed_by(Dd + 900) == c0
    assert markets._closed_by(Dd + 86400 + 900) == Dd
    for t in range(Dd - 20_000, Dd, 777):                            # before the arm: unchanged
        assert markets._cycle(t) == before[t][0]


# ── (c) owed held while stake is under the lock ──────────────────────────────
def _stake_gap(lo, hi):
    return lambda b: D(0) if lo <= b * 12 < hi else D(10_000)


def test_owed_is_held_while_stake_is_under_the_lock_and_paid_once_covered(v6, tmp_path, monkeypatch):
    # The win is owed (emission is ~0.01 alpha a cycle). The 03:30 loss puts the entity below its
    # high-water mark; it unstakes 03:50-06:10, so the 04:00 .. 06:00 closes find it short.
    now = DAY + 8 * H + 120
    chain = FakeChain(stake=_stake_gap(DAY + 3 * H + 3000, DAY + 6 * H + 600), aout=D("0.001"))
    off = _win_then_loss(tmp_path, "holdoff", chain, now)
    monkeypatch.setattr(config, "MARKETS_HOLD_FROM_UNIX", DAY)
    on = _win_then_loss(tmp_path, "holdon", chain, now)
    q = "SELECT cycle, held, claim, paid, owed FROM settle WHERE entity=? ORDER BY cycle"
    a, b = off.execute(q, (ENTITY,)).fetchall(), on.execute(q, (ENTITY,)).fetchall()
    held = {r[0] for r in b if r[1]}
    assert held == {DAY + 3 * H, DAY + 4 * H, DAY + 5 * H}          # closes at 04:00, 05:00, 06:00
    for r in b:
        if r[0] in held:
            assert D(r[2]) == 0 and D(r[3]) == 0                    # claims nothing
    owed_before = D(next(r for r in b if r[0] == DAY + 2 * H)[4])
    assert owed_before > 0
    assert all(D(r[4]) == owed_before for r in b if r[0] in held)   # keeps its owed, cancels nothing
    after = next(r for r in b if r[0] == DAY + 6 * H)
    assert after[1] == 0 and D(after[2]) == owed_before and D(after[3]) > 0   # covered again: claims it
    assert not any(r[1] for r in a)                                  # arm off: never held
    assert sum(D(r[3]) for r in b) < sum(D(r[3]) for r in a)          # emission withheld while short


def _live_longshot(tmp_path, name):
    """$10 UP placed 5 minutes INTO the 00:30-00:45 window with BTC already 1% under the target: a
    live-priced longshot (model at its floor + spread). Ticks every 5 s so the fill exists."""
    entries = [_entry_for(ENTITY, S1 + 300, 1, _payload(S1, "UP", "10.00"))]
    ticks = [{"a": "BTCUSD", "t": t * 1000, "b": None, "k": None, "p": 100.0 if t < S1 else 99.0}
             for t in range(S1 - 120, S1 + 901, 5)] + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / f"feed{name}", entries, ticks, NOW)
    return _db_after(tmp_path, base, FakeChain(stake=D(20), aout=D("0.001")), NOW, name=name)


def test_live_longshot_is_refused_and_its_payout_is_the_graded_fill(v6, tmp_path, monkeypatch):
    off = _live_longshot(tmp_path, "lsoff")
    assert off.execute("SELECT status FROM coll").fetchone()[0] == "ok"       # stake 8.3 alpha fits 20
    shares_off = D(off.execute("SELECT shares FROM results").fetchone()[0])
    assert shares_off > D(50)                                                  # pays many times the stake
    monkeypatch.setattr(config, "MARKETS_PAYOUT_CHECK_FROM_UNIX", DAY)
    on = _live_longshot(tmp_path, "lson")
    assert on.execute("SELECT status FROM coll").fetchone()[0] == "ignored:collateral"
    pay = D(on.execute("SELECT payout FROM coll_pay").fetchone()[0])
    assert abs(pay * RATE - shares_off) < D("1e-20")                           # the exact fill the grader paid
