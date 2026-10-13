"""Grade from the record (2026-10-13): a market's verdict never depends on when a validator looked."""
from decimal import Decimal

from sn89_signals import config, markets, oracle
from tests.test_markets_v2 import (DAY, ENTITY, S1, FakeChain, _db_after, _entry, _payload, _publish,  # noqa: F401
                                   _rate_ticks, _ticks, v2)


def _oracle_market_with_a_hole(tmp_path, monkeypatch, sealed_from):
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", DAY)
    monkeypatch.setattr(config, "MARKETS_SEALED_FROM_UNIX", sealed_from)
    monkeypatch.setattr(oracle, "rows_for", lambda *a, **k: ([], [S1 * 1000]))       # one window never fetched
    monkeypatch.setattr(oracle, "rows_for_spans", lambda *a, **k: ([], [S1 * 1000]))
    entries = [_entry(S1 - 600, 1, _payload(S1, "UP", "10.00", n=1))]
    ticks = _ticks("BTCUSD", S1, S1 + 900, 100.0, 101.0) + _rate_ticks(DAY) + _rate_ticks(DAY + 86400)
    base = _publish(tmp_path / "feed", entries, ticks, DAY + 86400 + 3600)
    db = _db_after(tmp_path, base, FakeChain(stake=Decimal(10_000)), S1 + 900 + 10 * 3600)    # long after abandon
    return db.execute("SELECT outcome FROM outcomes WHERE market_id=?", (markets.market_id("BTCUSD", "15m", S1),)).fetchone()


def test_before_the_seal_arm_an_unfetched_oracle_window_voids_at_abandon(v2, tmp_path, monkeypatch):
    assert _oracle_market_with_a_hole(tmp_path, monkeypatch, 0) == ("VOID",)


def test_from_the_seal_arm_an_unfetched_oracle_window_waits_at_any_time(v2, tmp_path, monkeypatch):
    assert _oracle_market_with_a_hole(tmp_path, monkeypatch, DAY) is None


def test_sigma_falls_back_only_when_the_predicate_says_the_record_is_final(tmp_path, monkeypatch):
    db = markets._db(str(tmp_path))
    monkeypatch.setattr(markets, "day_ticks", lambda *a, **k: ({}, [123]))
    start = (config.MARKETS_V3_FROM_UNIX // 86400 + 3) * 86400 + 7200
    assert markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", start, final=lambda m: False) is None
    s = markets.sigma_for_market(db, "B", str(tmp_path), "BTCUSD", start, final=lambda m: m == [123])
    assert s is not None and s == markets.sigma_fallback("BTCUSD", start)


def test_consensus_float_sums_are_left_to_right_on_every_python():
    # 3.12 sum([0.1] * 10) is 1.0 (compensated); 3.10 and fsum_l2r give 0.9999999999999999
    assert markets.fsum_l2r([0.1] * 10) == 0.9999999999999999
    marks = [170.065, 170.06, 170.07, 170.065, 170.065, 170.06, 170.07]
    acc = 0.0
    for x in marks:
        acc += x
    assert markets.average_marks is not None and markets.fsum_l2r(marks) == acc
