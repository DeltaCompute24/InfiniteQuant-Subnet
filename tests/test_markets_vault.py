"""Markets V5 (Whit 2026-10-12): collateral is alpha held in the vault, not stake the entity can unstake."""
from decimal import Decimal

import pytest

from sn89_signals import config, hf, markets, markets_vault as vault
from tests.test_markets_v2 import (DAY, ENTITY, S1, FakeChain, _db_after, _payload, _publish,  # noqa: F401
                                   _rate_ticks, _ticks, v2)
from tests.test_markets_v2b import ENTITY_B, _entry_for, v2b  # noqa: F401

D = Decimal
VCK, VHK, OP = "5VaultColdkey", "5VaultHotkey", "5Operator"
OWNERS = {ENTITY: "5OwnerA", ENTITY_B: "5OwnerB"}


@pytest.fixture
def v5(v2b, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_CARRY_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_OWED_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_LOCK_FROM_UNIX", 0)
    monkeypatch.setattr(config, "MARKETS_VAULT_OPEN_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_VAULT_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_VAULT_COLDKEY", VCK)
    monkeypatch.setattr(config, "MARKETS_VAULT_HOTKEY", VHK)
    monkeypatch.setattr(config, "MARKETS_VAULT_OPERATORS", (OP,))
    return config


class VaultChain(FakeChain):
    def __init__(self, xfers=None, price=D(1), vault_stake=D(10**9), **kw):
        super().__init__(stake=kw.pop("stake", D(10**6)), **kw)
        self.xfers, self.price, self.vstake = xfers or {}, price, vault_stake

    def transfer_events(self, b, i):
        return self.xfers.get((b, i), [])

    def hotkey_owner(self, hk, b):
        return OWNERS.get(hk, "5Nobody")

    def share_price(self, hk, b):
        return self.price(b) if callable(self.price) else self.price

    def stake_of(self, ck, hk, b):
        return self.vstake(b) if callable(self.vstake) else self.vstake


def _xfer(origin, dest, alpha, hk=VHK, net=None):
    net = config.NETUID if net is None else net
    return [{"event": "StakeTransferred", "origin_coldkey": origin, "destination_coldkey": dest, "hotkey": hk,
             "origin_netuid": net, "destination_netuid": net},
            {"event": "StakeAdded", "coldkey": dest, "hotkey": hk, "alpha": D(alpha), "netuid": net}]


def _vf(kind, **kw):
    return {"kind": kind, "trade_pair": config.MARKETS_RATE_PAIR, **kw}


def _dep(hk, t, seq, block, alpha, idx=1):
    return _entry_for(hk, t, seq, _vf(vault.KIND_DEPOSIT, amount_alpha=str(alpha), block=block, ext_index=idx))


def _bet(hk, t, seq, start, side="UP", dollars="50.00", n=1):
    p = _payload(start, side, dollars, n)
    p["account"] = f"{hk}_{n}"
    return _entry_for(hk, t, seq, p)


def _run(tmp_path, entries, chain, until, name="c", extra_ticks=()):
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + list(extra_ticks)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    base = _publish(tmp_path / f"feed{name}", entries, ticks, until + 900)
    return _db_after(tmp_path, base, chain, until, name=name), base


def _status(db, hk=ENTITY):
    return [s for (s,) in db.execute("SELECT c.status FROM coll c JOIN bets b ON b.key=c.key WHERE b.hk=? "
                                     "ORDER BY b.t_recv_us", (hk,))]


# ── deposits ──────────────────────────────────────────────────────────────────
def test_bets_are_checked_against_the_vault_not_the_hotkey_stake(v5, tmp_path):
    # 100 alpha in the vault at 1.2 $/alpha = $120: $50 + $50 fit, the third does not, although the
    # entity's own hotkey holds 1,000,000 alpha.
    blk = (DAY + 240) // 12
    chain = VaultChain(xfers={(blk, 1): _xfer("5OwnerA", VCK, 100)})
    entries = [_dep(ENTITY, DAY + 300, 1, blk, 100)] + [_bet(ENTITY, S1 - 600 + i * 30, i + 2, S1, n=i + 1)
                                                        for i in range(3)]
    db, _ = _run(tmp_path, entries, chain, S1 + 1800)
    assert _status(db) == ["ok", "ok", "ignored:collateral"]


def test_a_deposit_that_did_not_reach_the_vault_credits_nothing(v5, tmp_path):
    blk = (DAY + 240) // 12
    for name, ev in (("elsewhere", _xfer("5OwnerA", "5SomeoneElse", 100)),
                     ("not-owner", _xfer("5Stranger", VCK, 100)),
                     ("other-hotkey", _xfer("5OwnerA", VCK, 100, hk="5OtherHotkey"))):
        chain = VaultChain(xfers={(blk, 1): ev})
        db, _ = _run(tmp_path, [_dep(ENTITY, DAY + 300, 1, blk, 100), _bet(ENTITY, S1 - 600, 2, S1)], chain,
                     S1 + 1800, name=name)
        assert _status(db) == ["ignored:collateral"], name


def test_one_extrinsic_is_credited_once_and_never_above_what_arrived(v5, tmp_path):
    blk = (DAY + 240) // 12
    chain = VaultChain(xfers={(blk, 1): _xfer("5OwnerA", VCK, 100)})
    entries = [_dep(ENTITY, DAY + 300, 1, blk, 500), _dep(ENTITY, DAY + 310, 2, blk, 100)]
    db, _ = _run(tmp_path, entries, chain, S1)
    rows = db.execute("SELECT alpha FROM vault_ledger WHERE entity=?", (ENTITY,)).fetchall()
    assert rows == [("100",)]


def test_staking_yield_reaches_each_entity_pro_rata(v5, tmp_path):
    # Both deposit at share price 1; by 01:00 the price is 2, so 100 alpha has become 200 ($240).
    b1, b2 = (DAY + 240) // 12, (DAY + 264) // 12
    chain = VaultChain(xfers={(b1, 1): _xfer("5OwnerA", VCK, 100), (b2, 1): _xfer("5OwnerB", VCK, 50)},
                       price=lambda b: D(1) if b * 12 < DAY + 3000 else D(2))
    s2 = DAY + 3600 + 1800
    entries = [_dep(ENTITY, DAY + 300, 1, b1, 100), _dep(ENTITY_B, DAY + 300, 1, b2, 50)]
    entries += [_bet(ENTITY, s2 - 600 + i * 20, i + 2, s2, n=1) for i in range(5)]
    db, _ = _run(tmp_path, entries, chain, s2 + 1800, extra_ticks=_ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0))
    assert _status(db) == ["ok", "ok", "ok", "ok", "ignored:collateral"]        # $200 fits $240, $250 does not
    bal = vault.balances(db, chain, s2)
    assert D(bal[ENTITY]["alpha"]) == 200 and D(bal[ENTITY_B]["alpha"]) == 100


# ── withdrawals ───────────────────────────────────────────────────────────────
def test_a_withdrawal_leaves_open_bets_covered_and_is_released_on_chain(v5, tmp_path):
    # 100 alpha; $60 (50 alpha) open on the 00:30-00:45 market; ask for 80 at 00:25 -> 50 approved at 00:30.
    blk, rel = (DAY + 240) // 12, (S1 + 300) // 12
    chain = VaultChain(xfers={(blk, 1): _xfer("5OwnerA", VCK, 100), (rel, 3): _xfer(VCK, "5OwnerA", 50)},
                       vault_stake=lambda b: D(100) if b < rel else D(50))
    wd = _entry_for(ENTITY, S1 - 300, 3, _vf(vault.KIND_WITHDRAW, amount_alpha="80"))
    release = _entry_for(OP, S1 + 400, 1, _vf(vault.KIND_RELEASE, withdraw_key=f"{ENTITY}:3", block=rel, ext_index=3))
    entries = [_dep(ENTITY, DAY + 300, 1, blk, 100), _bet(ENTITY, S1 - 600, 2, S1, dollars="60.00"), wd, release]
    db, _ = _run(tmp_path, entries, chain, S1 + 3600)
    req = db.execute("SELECT cp, approved, status, released_alpha FROM vault_requests WHERE key=?",
                     (f"{ENTITY}:3",)).fetchone()
    assert req == (S1, "50.000000000", "released", "50")
    assert not db.execute("SELECT 1 FROM vault_checks WHERE short=1").fetchone()


def test_frames_need_the_vault_open_and_a_release_needs_an_operator(v5, monkeypatch):
    ok = _vf(vault.KIND_RELEASE, withdraw_key="k", block=1, ext_index=0)
    vault.validate(ok, OP, DAY + 10)
    with pytest.raises(hf.HFRejected, match="release_not_operator"):
        vault.validate(ok, ENTITY, DAY + 10)
    with pytest.raises(hf.HFRejected, match="markets_vault_not_open"):
        vault.validate(_vf(vault.KIND_EXIT), ENTITY, DAY - 10)
    monkeypatch.setattr(config, "MARKETS_VAULT_OPEN_UNIX", 0)
    with pytest.raises(hf.HFRejected, match="markets_vault_not_open"):
        vault.validate(_vf(vault.KIND_EXIT), ENTITY, DAY + 10)


# ── solvency ──────────────────────────────────────────────────────────────────
def test_a_vault_short_of_its_ledger_takes_no_bets(v5, tmp_path):
    blk = (DAY + 240) // 12
    chain = VaultChain(xfers={(blk, 1): _xfer("5OwnerA", VCK, 100)}, vault_stake=D(60))
    db, _ = _run(tmp_path, [_dep(ENTITY, DAY + 300, 1, blk, 100), _bet(ENTITY, S1 - 600, 2, S1)], chain, S1 + 1800)
    assert _status(db) == ["ignored:vault_short"]


# ── exit and the reserve ──────────────────────────────────────────────────────
def test_an_exit_forfeits_the_locked_loss_to_the_reserve_which_pays_the_next_winner(v5, tmp_path):
    # A loses $60 (50 alpha) in 00:30-00:45 (cycle 00:00-01:00, closes 01:01): locked 50. A exits at 01:10:
    # 50 is released, 50 goes to the reserve. B wins in 01:30-01:45 (cycle 01:00-02:00): the reserve pays
    # B's claim first, as a vault credit at 02:01.
    ba, bb = (DAY + 240) // 12, (DAY + 264) // 12
    chain = VaultChain(xfers={(ba, 1): _xfer("5OwnerA", VCK, 100), (bb, 1): _xfer("5OwnerB", VCK, 100)})
    s2 = DAY + 3600 + 1800
    entries = [_dep(ENTITY, DAY + 300, 1, ba, 100), _dep(ENTITY_B, DAY + 300, 1, bb, 100),
               _bet(ENTITY, S1 - 600, 2, S1, side="DOWN", dollars="60.00"),
               _entry_for(ENTITY, DAY + 3600 + 600, 3, _vf(vault.KIND_EXIT)),
               _bet(ENTITY_B, s2 - 600, 2, s2, dollars="60.00")]
    db, _ = _run(tmp_path, entries, chain, DAY + 3 * 3600,
                 extra_ticks=_ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0))
    assert _status(db) == ["ok"] and _status(db, ENTITY_B) == ["ok"]
    ex = db.execute("SELECT cp, after_cycle, forfeited FROM vault_exits WHERE entity=?", (ENTITY,)).fetchone()
    assert ex[0] == DAY + 3600 + 900 and ex[1] == DAY and D(ex[2]) == 50
    assert D(db.execute("SELECT approved FROM vault_requests WHERE key=?", (f"{ENTITY}:3",)).fetchone()[0]) == 50
    paid_r = D(db.execute("SELECT reserve_paid FROM settle WHERE cycle=? AND entity=?",
                          (DAY + 3600, ENTITY_B)).fetchone()[0])
    payable = D(db.execute("SELECT payable FROM settle WHERE cycle=? AND entity=?", (DAY + 3600, ENTITY_B)).fetchone()[0])
    assert paid_r > 0 and abs(paid_r - min(D(50), payable)) < D("0.000000001")   # rao-rounded down
    rsv = vault.balances(db, chain, DAY + 3 * 3600)[vault.RESERVE]
    assert D(rsv["alpha"]) == 50 - paid_r
    # A's carry restarts after the exit: no hole carried into the 01:00 cycle
    row = db.execute("SELECT rcum, hwm FROM settle WHERE cycle=? AND entity=?", (DAY + 3600, ENTITY)).fetchone()
    assert row is None or (D(row[0]) == 0 and D(row[1]) == 0)
    assert vault.locked(db, ENTITY, DAY + 3 * 3600) == 0


def test_a_rebuild_writes_the_same_ledger(v5, tmp_path):
    ba, bb = (DAY + 240) // 12, (DAY + 264) // 12
    chain = VaultChain(xfers={(ba, 1): _xfer("5OwnerA", VCK, 100), (bb, 1): _xfer("5OwnerB", VCK, 100)})
    s2 = DAY + 3600 + 1800
    entries = [_dep(ENTITY, DAY + 300, 1, ba, 100), _dep(ENTITY_B, DAY + 300, 1, bb, 100),
               _bet(ENTITY, S1 - 600, 2, S1, side="DOWN", dollars="60.00"),
               _entry_for(ENTITY, DAY + 3600 + 600, 3, _vf(vault.KIND_EXIT)),
               _bet(ENTITY_B, s2 - 600, 2, s2, dollars="60.00")]
    ticks = (_ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _ticks("BTCUSD", s2, s2 + 900, 100.0, 101.0)
             + _rate_ticks(DAY) + _rate_ticks(DAY + 86400))
    end = DAY + 3 * 3600
    base = _publish(tmp_path / "feed", entries, ticks, end + 900)
    inc = str(tmp_path / "inc")
    for t in range(DAY + 1800, end + 1, 1200):                     # incremental, as a live validator runs
        markets.sync_and_grade(base, inc, t, chain)
    fresh = _db_after(tmp_path, base, chain, end, name="fresh")
    q = "SELECT key, t_us, entity, units, alpha, kind FROM vault_ledger ORDER BY key"
    assert markets._db(inc).execute(q).fetchall() == fresh.execute(q).fetchall()
    q2 = "SELECT cycle, entity, payable, reserve_paid, claim, paid, owed FROM settle ORDER BY cycle, entity"
    assert markets._db(inc).execute(q2).fetchall() == fresh.execute(q2).fetchall()


def test_vault_off_changes_nothing(v2b, tmp_path):
    chain = VaultChain()
    db, _ = _run(tmp_path, [_bet(ENTITY, S1 - 600, 1, S1)], chain, S1 + 1800)
    assert _status(db) == ["ok"] and db.execute("SELECT COUNT(*) FROM vault_checks").fetchone()[0] == 0


# ── V5b: emission through the vault UID, the vault as the float ──────────────
VUID_HK = "5VaultUidHotkey"


@pytest.fixture
def v5b(v5, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_VAULT_UID_HOTKEY", VUID_HK)
    return config


def _sweep(alpha, src=VUID_HK, dst=VHK, ck=VCK):
    net = config.NETUID
    return [{"event": "StakeMoved", "coldkey": ck, "origin_hotkey": src, "origin_netuid": net,
             "destination_hotkey": dst, "destination_netuid": net},
            {"event": "StakeAdded", "coldkey": ck, "hotkey": dst, "alpha": D(alpha), "netuid": net}]


def _sweep_claim(t, seq, block, idx=2, signer=OP):
    return _entry_for(signer, t, seq, _vf(vault.KIND_SWEEP, block=block, ext_index=idx))


def _paid_through(db, c):
    out = {}
    for e, p in db.execute("SELECT entity, paid FROM settle WHERE cycle <= ?", (c,)):
        out[e] = out.get(e, D(0)) + D(p)
    return out


def _two_winners(chain_kw=None, extra=()):
    """A and B each deposit 100; A's player wins $60 UP and B's player wins $30 UP in 00:30-00:45."""
    ba, bb = (DAY + 240) // 12, (DAY + 264) // 12
    xf = {(ba, 1): _xfer("5OwnerA", VCK, 100), (bb, 1): _xfer("5OwnerB", VCK, 100)}
    xf.update((chain_kw or {}).pop("xfers", {}))
    chain = VaultChain(xfers=xf, **(chain_kw or {}))
    entries = [_dep(ENTITY, DAY + 300, 1, ba, 100), _dep(ENTITY_B, DAY + 300, 1, bb, 100),
               _bet(ENTITY, S1 - 600, 2, S1, dollars="60.00"), _bet(ENTITY_B, S1 - 590, 2, S1, dollars="30.00")]
    return chain, entries + list(extra)


def test_players_net_winnings_are_debited_from_the_vault_and_sent_as_a_payout(v5b, tmp_path):
    chain, entries = _two_winners()
    db, _ = _run(tmp_path, entries, chain, DAY + 2 * 3600)
    tc = DAY + config.MARKETS_SETTLE_PERIOD_S + config.MARKETS_SETTLE_GRACE_S
    rows = {e: (cp, D(req), D(ok), st) for e, cp, req, ok, st in db.execute(
        "SELECT entity, cp, requested, approved, status FROM vault_requests WHERE kind=?", (vault.KIND_PAYOUT,))}
    won = {e: D(w) for e, w in db.execute("SELECT entity, won_ret FROM settle WHERE cycle=?", (DAY,))}
    assert set(rows) == {ENTITY, ENTITY_B}
    for e in rows:
        cp, req, ok, st = rows[e]
        assert cp == tc and st == "approved" and req == ok == won[e].quantize(markets.RAO, rounding="ROUND_DOWN")
    bal = vault.balances(db, chain, DAY + 2 * 3600)
    assert D(bal[ENTITY]["alpha"]) == 100 - rows[ENTITY][2]
    assert not db.execute("SELECT 1 FROM vault_checks WHERE short=1").fetchone()   # pending payouts count


def test_a_payout_is_capped_at_the_balance_and_the_rest_recorded_uncovered(v5b, tmp_path):
    # A deposits 30 alpha ($36) and its player's $30 bet pays well over 30 alpha of winnings
    ba = (DAY + 240) // 12
    chain = VaultChain(xfers={(ba, 1): _xfer("5OwnerA", VCK, 30)})
    db, _ = _run(tmp_path, [_dep(ENTITY, DAY + 300, 1, ba, 30), _bet(ENTITY, S1 - 600, 2, S1, dollars="30.00")],
                 chain, DAY + 2 * 3600)
    won = D(db.execute("SELECT won_ret FROM settle WHERE cycle=? AND entity=?", (DAY, ENTITY)).fetchone()[0])
    req, ok = db.execute("SELECT requested, approved FROM vault_requests WHERE kind=?", (vault.KIND_PAYOUT,)).fetchone()
    if won > 30:
        assert D(ok) == 30 and D(req) > D(ok)
    else:
        assert D(ok) == D(req)
    assert D(vault.balances(db, chain, DAY + 2 * 3600)[ENTITY]["alpha"]) >= 0


def test_the_markets_share_goes_to_the_vault_uid(v5b, tmp_path):
    chain, entries = _two_winners()
    db, _ = _run(tmp_path, entries, chain, DAY + 2 * 3600)
    uids = {ENTITY: 10, ENTITY_B: 11, VUID_HK: 99}
    vec = markets.pnl_vector(db, uids, DAY + 2 * 3600)
    em = D(db.execute("SELECT emission FROM settle_meta WHERE cycle=?", (DAY,)).fetchone()[0])
    paid = sum(D(p) for (p,) in db.execute("SELECT paid FROM settle WHERE cycle=?", (DAY,)))
    assert 10 not in vec and 11 not in vec
    assert vec[99] == pytest.approx(float(min(D(1), paid / em)))
    assert sum(vec.values()) == pytest.approx(1.0)
    # an unregistered vault UID sends the share to the burn UID (and says so), never to the entities
    vec2 = markets.pnl_vector(db, {ENTITY: 10, ENTITY_B: 11}, DAY + 2 * 3600)
    assert vec2 == {config.BURN_UID: 1.0}


def test_a_sweep_is_credited_pro_rata_to_owed_emission_once_and_the_excess_to_the_reserve(v5b, tmp_path):
    t_sw = DAY + 2 * 3600 + 100
    bs = t_sw // 12 - 2
    chain, entries = _two_winners({"xfers": {(bs, 2): _sweep(10**6)}},
                                  extra=[_sweep_claim(t_sw, 1, bs), _sweep_claim(t_sw + 10, 2, bs)])
    db, _ = _run(tmp_path, entries, chain, DAY + 3 * 3600)
    paid = _paid_through(db, DAY + 3600)                          # every cycle closed by the sweep's checkpoint
    em = {e: D(a) for e, a in db.execute("SELECT entity, alpha FROM vault_ledger WHERE kind='emission'")}
    for e in (ENTITY, ENTITY_B):
        assert abs(em[e] - paid[e]) < D("0.000000001")            # owed in full: 10**6 arrived
    assert abs(em[vault.RESERVE] - (10**6 - em[ENTITY] - em[ENTITY_B])) < D("0.000000001")
    assert db.execute("SELECT COUNT(DISTINCT ref) FROM vault_ledger WHERE kind='emission'").fetchone()[0] == 1
    assert vault.emission_outstanding(db, DAY + 3600, (DAY + 3 * 3600) * 1_000_000) == {}


def test_a_short_sweep_is_shared_pro_rata_and_the_rest_stays_outstanding(v5b, tmp_path):
    t_sw = DAY + 2 * 3600 + 100
    bs = t_sw // 12 - 2
    chain, entries = _two_winners({"xfers": {(bs, 2): _sweep("0.001")}}, extra=[_sweep_claim(t_sw, 1, bs)])
    db, _ = _run(tmp_path, entries, chain, DAY + 3 * 3600)
    paid = _paid_through(db, DAY + 3600)                          # every cycle closed by the sweep's checkpoint
    em = {e: D(a) for e, a in db.execute("SELECT entity, alpha FROM vault_ledger WHERE kind='emission'")}
    tot = paid[ENTITY] + paid[ENTITY_B]
    if tot > D("0.001"):
        for e in (ENTITY, ENTITY_B):
            assert abs(em[e] - D("0.001") * paid[e] / tot) < D("0.000000002")
        left = vault.emission_outstanding(db, DAY + 3600, (DAY + 3 * 3600) * 1_000_000)
        assert abs(sum(left.values()) - (tot - em[ENTITY] - em[ENTITY_B])) < D("0.000000001")
        assert vault.RESERVE not in em


def test_a_sweep_from_the_wrong_hotkey_or_signer_credits_nothing(v5b, tmp_path):
    t_sw = DAY + 2 * 3600 + 100
    bs = t_sw // 12 - 2
    chain, entries = _two_winners({"xfers": {(bs, 2): _sweep(100, src="5SomeEntityHotkey")}},
                                  extra=[_sweep_claim(t_sw, 1, bs)])
    db, _ = _run(tmp_path, entries, chain, DAY + 3 * 3600)
    assert not db.execute("SELECT 1 FROM vault_ledger WHERE kind='emission'").fetchone()
    with pytest.raises(hf.HFRejected, match="sweep_not_operator"):
        vault.validate(_vf(vault.KIND_SWEEP, block=1, ext_index=0), ENTITY, DAY + 10)
