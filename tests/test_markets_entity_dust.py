"""Entity dust (Whit, 2026-10-09): any Markets entity UID that posts collateral keeps dust."""
from decimal import Decimal

import pytest

from sn89_signals import competitions, config, markets

T = config.MARKETS_ENTITY_DUST_FROM_UNIX + 7200          # a cycle after the stamp
ENT, OTHER = "5EntityHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "5OtherHotkeyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
UIDS = {ENT: 199, OTHER: 7}


class Chain(markets.ChainView):
    def __init__(self, stake, fail=False):
        self.stake, self.fail = stake, fail

    def block_at(self, t):
        if self.fail:
            raise RuntimeError("rpc down")
        return t // 12

    def block_time(self, b):
        return b * 12

    def entity_stake(self, hk, b):
        return Decimal(self.stake.get(hk, 0))


def _db(tmp_path, bets=(), burns=()):
    db = markets._db(str(tmp_path))
    for i, (hk, t) in enumerate(bets):
        db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                   (f"{hk}:{i}", "UD:BTCUSD:1h:0", hk, "UP", "10", 0, int(t * 1e6), hk, i))
    for i, (hk, t) in enumerate(burns):
        db.execute("INSERT INTO burns VALUES (?,?,?,?,?,?,?)", (f"{hk}:b{i}", hk, "1", 1, 1, 0, int(t * 1e6)))
    db.commit()
    return db


def _uids(tmp_path, stake, bets=((ENT, T - 3600),), burns=(), fail=False):
    db = _db(tmp_path, bets, burns)
    try:
        return markets.entity_dust_uids(db, UIDS, T, Chain(stake, fail))
    finally:
        db.close()


MIN = config.markets_dust_min_collateral_as_of(T)
RAISE_AT = 1791590400                                    # 2026-10-10 00:00:00 UTC: 120 -> 6,100 alpha


def _uids_at(tmp_path, stake, when):
    sub = tmp_path / f"d{len(list(tmp_path.iterdir()))}"   # a fresh cache per call
    sub.mkdir()
    db = _db(sub, ((ENT, when - 3600),))
    try:
        return markets.entity_dust_uids(db, UIDS, when, Chain(stake))
    finally:
        db.close()


def test_minimum_is_120_before_the_raise_and_6100_after():
    assert config.markets_dust_min_collateral_as_of(RAISE_AT - 1) == Decimal("120")
    assert config.markets_dust_min_collateral_as_of(RAISE_AT) == Decimal("6100")
    assert config.MARKETS_DUST_MIN_COLLATERAL_ALPHA == Decimal("6100")


def test_before_the_raise_120_alpha_is_enough(tmp_path):
    assert _uids_at(tmp_path, {ENT: Decimal("213.1425")}, RAISE_AT - 1800) == {199}


def test_from_the_raise_213_alpha_gets_nothing_and_6100_gets_dust(tmp_path):
    assert _uids_at(tmp_path, {ENT: Decimal("213.1425")}, RAISE_AT + 1800) == set()
    assert _uids_at(tmp_path, {ENT: Decimal("6100")}, RAISE_AT + 1800) == {199}
    assert _uids_at(tmp_path, {ENT: Decimal("6099.999")}, RAISE_AT + 1800) == set()


def test_below_minimum_collateral_gets_nothing(tmp_path):
    assert _uids(tmp_path, {ENT: MIN - Decimal("0.001")}) == set()


def test_at_or_above_minimum_collateral_gets_dust(tmp_path):
    assert _uids(tmp_path, {ENT: MIN}) == {199}


def test_a_burn_claim_also_makes_an_entity_active(tmp_path):
    assert _uids(tmp_path, {ENT: MIN}, bets=(), burns=((ENT, T - 60),)) == {199}


def test_inactive_entity_gets_nothing(tmp_path):
    old = T - config.MARKETS_ENTITY_ACTIVE_S - 1
    assert _uids(tmp_path, {ENT: MIN * 10}, bets=((ENT, old),)) == set()


def test_unregistered_or_burn_uid_entity_gets_nothing(tmp_path):
    db = _db(tmp_path, bets=((ENT, T - 60),))
    try:
        assert markets.entity_dust_uids(db, {ENT: config.BURN_UID}, T, Chain({ENT: MIN})) == set()
        assert markets.entity_dust_uids(db, {OTHER: 7}, T, Chain({ENT: MIN})) == set()
    finally:
        db.close()


def test_chain_failure_gives_no_dust_and_does_not_raise(tmp_path, capsys):
    assert _uids(tmp_path, {ENT: MIN}, fail=True) == set()
    assert "MARKETS ENTITY DUST SKIPPED" in capsys.readouterr().out


def test_no_earner_cycle_gives_the_entity_dust_not_the_share():
    vec = markets.apply_entity_dust({config.BURN_UID: 1.0}, {199})
    assert vec[199] == pytest.approx(config.DUST_WEIGHT)
    assert sum(vec.values()) == pytest.approx(1.0)
    combined = competitions.combine({"lf": None, "hf": None, "markets": vec},
                                    {"lf": 0.4375, "hf": 0.4375, "markets": 0.125})
    assert combined[199] == pytest.approx(0.125 * config.DUST_WEIGHT)


def test_an_earning_entity_keeps_max_not_sum():
    earned = {199: 0.3, config.BURN_UID: 0.7}
    assert markets.apply_entity_dust(earned, {199}) == earned
    small = {199: config.DUST_WEIGHT / 4, config.BURN_UID: 1 - config.DUST_WEIGHT / 4}
    out = markets.apply_entity_dust(small, {199})
    assert out[199] == pytest.approx(config.DUST_WEIGHT)
    assert sum(out.values()) == pytest.approx(1.0)


def _weights(monkeypatch, tmp_path, now):
    monkeypatch.setattr(config, "markets_collateral_as_of", lambda t: False)
    monkeypatch.setattr(markets, "markets_tallies", lambda now, base, cache_dir: {OTHER: 5.0})
    db = _db(tmp_path, bets=((ENT, now - 60),))
    db.close()
    return markets.markets_weights(UIDS, now, base="http://unused", cache_dir=str(tmp_path),
                                   chain_view=Chain({ENT: MIN}))


def test_before_the_stamp_the_vector_is_unchanged(monkeypatch, tmp_path):
    now = config.MARKETS_ENTITY_DUST_FROM_UNIX - 1
    assert _weights(monkeypatch, tmp_path, now) == markets.weights_from_scores({OTHER: 5.0}, UIDS, now)


def test_after_the_stamp_the_collateralised_entity_gets_dust(monkeypatch, tmp_path):
    vec = _weights(monkeypatch, tmp_path, T)
    base = markets.weights_from_scores({OTHER: 5.0}, UIDS, T)
    assert vec[199] == pytest.approx(config.DUST_WEIGHT)
    assert vec[7] == pytest.approx(base[7], abs=2 * config.DUST_WEIGHT)
    assert sum(vec.values()) == pytest.approx(1.0)


def test_mainnet_stamp_is_2026_10_09_1600_utc():
    import os
    if "SN89_MARKETS_ENTITY_DUST_FROM" not in os.environ:
        assert config.MARKETS_ENTITY_DUST_FROM_UNIX == 1791561600


def test_dust_comes_pro_rata_from_earners_when_nothing_is_burned():
    out = markets.apply_entity_dust({7: 0.6, 8: 0.4}, {199})
    assert out[199] == pytest.approx(config.DUST_WEIGHT)
    assert out[7] / out[8] == pytest.approx(1.5)
    assert sum(out.values()) == pytest.approx(1.0)


def test_windows_before_the_arm_are_never_fetched(monkeypatch, tmp_path):
    from sn89_signals import hf_grade
    arm = 1_800_000_000
    monkeypatch.setattr(config, "MARKETS_FROM_UNIX", arm)
    old, new = (arm - 3 * 3600) * 1000, arm * 1000
    monkeypatch.setattr(hf_grade, "_index", lambda base: [old, new])
    fetched = []
    monkeypatch.setattr(hf_grade, "_fetch_text", lambda url, *a, **k: fetched.append(url) and None)
    monkeypatch.setattr(config, "markets_collateral_as_of", lambda t: False)
    markets.sync_and_grade("http://x", str(tmp_path), arm + 60)
    assert not any(str(old) in u for u in fetched)
    assert any(str(new) in u for u in fetched)
