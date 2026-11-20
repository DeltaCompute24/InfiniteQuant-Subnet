"""Stock splits and quote sanity in the agent-book replay (Whit 2026-11-20, SNDK 8-for-1)."""
from types import MappingProxyType

import pytest

from sn89_signals import agent_books as ab

H = 3_600_000
D = ab.DAY_MS
MON = 1794787200000          # 2026-11-16 00:00 UTC, a Monday, = QUOTE_SANITY_FROM_MS
OLD = MON - 14 * D           # 2026-11-02, before the quote-sanity cutover
UNI = {"SNDKUSD": "equities", "NVDAUSD": "equities", "BTCUSD": "crypto"}
NOFEE = MappingProxyType({**ab.book_rules_as_of(MON / 1000), "fee_bps": MappingProxyType({}),
                          "fee_bps_default": 0.0})


def tick(a, t, p, b=None, k=None):
    return {"a": a, "t": t, "p": p, "b": p if b is None else b, "k": p if k is None else k}


def rc(t, targets):
    return {"account": "A", "seq": t, "t_recv_ms": t, "targets": targets}


def run(receipts, ticks, start=MON, **kw):
    return ab.replay_book("A", receipts, ticks, UNI, start_ms=start, end_ms=start + 7 * D,
                          rules=NOFEE, **kw)


def test_sndk_split_is_registered():
    assert ab.SPLITS["SNDKUSD"] == ((1795132800000, 8.0),)


def test_held_position_keeps_value_across_split(monkeypatch):
    eff = MON + 10 * H
    monkeypatch.setattr(ab, "SPLITS", {"NVDAUSD": ((eff, 8.0),)})
    t0 = MON + H
    ticks = [tick("NVDAUSD", t0 + 3000, 800.0),
             tick("NVDAUSD", eff + 5 * H, 100.0),        # first split-adjusted tick, flat
             tick("NVDAUSD", eff + 6 * H, 100.0)]
    res = run([rc(t0, {"NVDAUSD": -0.25}), rc(eff + 5 * H + 1000, {"NVDAUSD": 0.0})], ticks)
    assert res["equity"] == pytest.approx(1.0)
    assert res["balance"] == pytest.approx(1.0)
    assert [f["px"] for f in res["fills"]] == [800.0, 100.0]


def test_split_moves_pnl_by_the_adjusted_move_only(monkeypatch):
    eff = MON + 10 * H
    monkeypatch.setattr(ab, "SPLITS", {"NVDAUSD": ((eff, 8.0),)})
    t0 = MON + H
    ticks = [tick("NVDAUSD", t0 + 3000, 800.0), tick("NVDAUSD", eff + H, 99.0)]
    res = run([rc(t0, {"NVDAUSD": -0.25})], ticks)
    # short 0.25x at 800 (= 100 adjusted), now 99: +1% move, +0.25% equity
    assert res["equity"] == pytest.approx(1.0025)


def test_without_split_entry_the_repricing_is_read_as_a_move(monkeypatch):
    monkeypatch.setattr(ab, "SPLITS", {})
    t0 = MON + H
    ticks = [tick("NVDAUSD", t0 + 3000, 800.0), tick("NVDAUSD", t0 + 10 * H, 100.0)]
    res = run([rc(t0, {"NVDAUSD": -0.25})], ticks)
    assert res["equity"] == pytest.approx(1.21875)


def test_split_after_replay_start_with_no_position_is_harmless(monkeypatch):
    eff = MON + 10 * H
    monkeypatch.setattr(ab, "SPLITS", {"NVDAUSD": ((eff, 8.0),)})
    ticks = [tick("NVDAUSD", eff + H, 100.0), tick("NVDAUSD", eff + 2 * H, 100.0)]
    res = run([rc(eff + H - 5000, {"NVDAUSD": 0.25})], ticks)
    assert res["fills"][0]["px"] == 100.0 and res["equity"] == pytest.approx(1.0)


def test_stale_book_beside_new_price_falls_back_to_mid():
    t0 = MON + H
    # the first split-adjusted SNDK tick: pre-split book, post-split p
    ticks = [{"a": "BTCUSD", "t": t0 + 3000, "p": 92.58, "b": 753.23, "k": 753.25}]
    res = run([rc(t0, {"BTCUSD": -0.05})], ticks, half_spread_bps={"BTCUSD": 10.0})
    assert res["fills"][0]["px"] == pytest.approx(92.58 * (1 - 10e-4))


def test_wide_ask_alone_is_unusable():
    t0 = MON + H
    ticks = [{"a": "BTCUSD", "t": t0 + 3000, "p": 100.0, "b": 99.9, "k": 103.0}]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks, half_spread_bps={"BTCUSD": 5.0})
    assert res["fills"][0]["px"] == pytest.approx(100.05)


def test_quote_within_2pct_is_used():
    t0 = MON + H
    ticks = [{"a": "BTCUSD", "t": t0 + 3000, "p": 100.0, "b": 98.5, "k": 101.9}]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks)
    assert res["fills"][0]["px"] == 101.9


def test_before_cutover_any_uncrossed_quote_is_used():
    t0 = OLD + H
    ticks = [{"a": "BTCUSD", "t": t0 + 3000, "p": 92.58, "b": 753.23, "k": 753.25}]
    res = run([rc(t0, {"BTCUSD": -0.05})], ticks, start=OLD)
    assert res["fills"][0]["px"] == 753.23


def test_trade_history_entry_is_split_adjusted(monkeypatch):
    from sn89_signals import agent_books_io as abio
    eff = MON + 10 * H
    monkeypatch.setattr(ab, "SPLITS", {"NVDAUSD": ((eff, 8.0),)})
    fills = [{"pair": "NVDAUSD", "t_ms": MON + H, "dq": -0.001, "px": 800.0},
             {"pair": "NVDAUSD", "t_ms": eff + H, "dq": 0.008, "px": 99.0}]
    out = abio.annotate_closes(fills)
    assert out[1]["entry_px"] == pytest.approx(100.0)
    assert out[1]["move_pct"] == pytest.approx(1.0)


def test_max_dd_skips_empty_book_tick():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0),
             {"a": "BTCUSD", "t": t0 + 9000, "p": 193.0, "b": 92.0, "k": 294.0},
             tick("BTCUSD", t0 + 12000, 100.0)]
    res = run([rc(t0, {"BTCUSD": -0.25})], ticks)
    assert res["max_dd"] == pytest.approx(0.0)


def test_max_dd_ignores_a_broken_mark_left_behind():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("NVDAUSD", t0 + 3000, 100.0),
             {"a": "BTCUSD", "t": t0 + 9000, "p": 193.0, "b": 92.0, "k": 294.0},
             tick("NVDAUSD", t0 + 10000, 100.0)]          # sane tick of another held pair
    res = run([rc(t0, {"BTCUSD": -0.25, "NVDAUSD": 0.1})], ticks)
    assert res["max_dd"] == pytest.approx(0.0)
    assert res["equity"] == pytest.approx(1.0 - 0.0025 * 93.0)   # payout marks unchanged
