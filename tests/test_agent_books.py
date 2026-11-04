"""Agent competition v2 books (sn89_signals.agent_books): pure replay, staged, not wired."""
from types import MappingProxyType

import pytest

from sn89_signals import agent_books as ab
from sn89_signals import hf

H = 3_600_000
D = ab.DAY_MS
MON = 1793577600000          # 2026-11-02 00:00:00 UTC, a Monday
assert ab.week_start_ms(MON) == MON

UNI = {"BTCUSD": "crypto", "ETHUSD": "crypto", "XAUUSD": "forex-commodities",
       "NVDAUSD": "equities", "EURUSD": "forex"}


def rules(**kw):
    d = dict(ab.RULES_V1)
    d.update(kw)
    return MappingProxyType(d)


NOFEE = rules(fee_bps=MappingProxyType({}), fee_bps_default=0.0)


def tick(a, t, p, b=None, k=None):
    d = {"a": a, "t": t, "p": p}
    d["b"] = p if b is None else b
    d["k"] = p if k is None else k
    return d


def rc(t, targets, seq=None, acct="A"):
    return {"account": acct, "seq": t if seq is None else seq, "t_recv_ms": t,
            "targets": targets}


def run(receipts, ticks, start=MON, end=MON + 7 * D, r=NOFEE, **kw):
    return ab.replay_book("A", receipts, ticks, UNI, start_ms=start, end_ms=end, rules=r, **kw)


# ── fill timing and side ────────────────────────────────────────────────────
def test_no_fill_before_delay_first_tick_after():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + dt, 100.0 + i) for i, dt in
             enumerate((0, 1000, 2999, 3000, 3500))]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks)
    assert len(res["fills"]) == 1
    f = res["fills"][0]
    assert f["t_ms"] == t0 + 3000 and f["px"] == 103.0


def test_fill_waits_for_first_tick_strictly_after_gap():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 2999, 100.0), tick("BTCUSD", t0 + 9000, 101.0)]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks)
    assert [f["t_ms"] for f in res["fills"]] == [t0 + 9000]


def test_buy_at_ask_sell_at_bid():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0, 99.0, 101.0),
             tick("BTCUSD", t0 + 10_000, 100.0, 99.0, 101.0)]
    res = run([rc(t0, {"BTCUSD": 0.2}), rc(t0 + 6000, {"BTCUSD": 0.0})], ticks)
    assert [f["px"] for f in res["fills"]] == [101.0, 99.0]
    # round trip paid the full spread: qty = 0.2/100 = 0.002, loss = 0.002 * 2
    assert res["balance"] == pytest.approx(1.0 - 0.002 * 2.0)


def test_mid_only_tick_uses_half_spread():
    t0 = MON + H
    ticks = [{"a": "BTCUSD", "t": t0 + 3000, "p": 100.0}]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks, half_spread_bps={"BTCUSD": 10.0})
    assert res["fills"][0]["px"] == pytest.approx(100.1)


def test_crossed_quote_falls_back_to_mid():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0, 101.0, 99.0)]
    res = run([rc(t0, {"BTCUSD": 0.2})], ticks)
    assert res["fills"][0]["px"] == 100.0


# ── fees ────────────────────────────────────────────────────────────────────
def test_crypto_fee_2_9_bps_per_leg():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("BTCUSD", t0 + 10_000, 100.0)]
    res = run([rc(t0, {"BTCUSD": 0.25}), rc(t0 + 6000, {"BTCUSD": 0.0})], ticks,
              r=ab.RULES_V1)
    # qty 0.0025 bought and sold at 100: notional 0.25 per leg
    assert res["balance"] == pytest.approx(1.0 - 2 * 0.25 * 2.9e-4, rel=1e-12)


def test_fee_on_delta_only():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("BTCUSD", t0 + 10_000, 100.0)]
    r = rules(fee_bps=MappingProxyType({"crypto": 10.0}))
    res = run([rc(t0, {"BTCUSD": 0.1}), rc(t0 + 6000, {"BTCUSD": 0.25})], ticks, r=r)
    e1 = 1.0 - 0.1 * 1e-3
    expected = 0.1 * 1e-3 + (0.25 * e1 - 0.1) * 1e-3
    assert 1.0 - res["balance"] == pytest.approx(expected, rel=1e-9)


def test_other_class_defaults_to_crypto_fee():
    assert ab.RULES_V1["fee_bps_default"] == ab.CRYPTO_FEE_BPS == 2.9
    t0 = MON + H
    ticks = [tick("NVDAUSD", t0 + 3000, 100.0)]
    res = run([rc(t0, {"NVDAUSD": 0.2})], ticks, r=ab.RULES_V1)
    assert 1.0 - res["balance"] == pytest.approx(0.2 * 2.9e-4)


# ── hold / close ────────────────────────────────────────────────────────────
def test_omitted_pair_holds_and_zero_closes():
    t0 = MON + H
    ticks = []
    for i in range(10):
        ticks += [tick("BTCUSD", t0 + 3000 + i * 4000, 100.0 + i),
                  tick("ETHUSD", t0 + 3000 + i * 4000, 50.0)]
    res = run([rc(t0, {"BTCUSD": 0.2, "ETHUSD": 0.1}),
               rc(t0 + 8000, {"ETHUSD": 0.0})], ticks)
    pairs = [(f["pair"], f["target"]) for f in res["fills"]]
    assert pairs == [("BTCUSD", 0.2), ("ETHUSD", 0.1), ("ETHUSD", 0.0)]
    # BTC still open, quantity unchanged since its only fill
    assert res["equity"] > res["balance"]


def test_identical_receipt_is_noop_not_fill():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000 + i * 4000, 100.0) for i in range(5)]
    res = run([rc(t0, {"BTCUSD": 0.2}), rc(t0 + 5000, {"BTCUSD": 0.2})], ticks)
    assert len(res["fills"]) == 1 and len(res["noops"]) == 1


# ── caps ────────────────────────────────────────────────────────────────────
def test_per_pair_cap_refuses_whole_receipt():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("ETHUSD", t0 + 3000, 50.0)]
    res = run([rc(t0, {"BTCUSD": 0.26, "ETHUSD": 0.1})], ticks)
    assert res["fills"] == [] and res["refused"][0][1] == "pair_cap:BTCUSD"
    res = run([rc(t0, {"BTCUSD": -0.25, "ETHUSD": 0.1})], ticks)
    assert len(res["fills"]) == 2


def test_gross_cap_counts_held_targets():
    t0 = MON + H
    r = [rc(t0, {"BTCUSD": 0.25, "ETHUSD": 0.25, "XAUUSD": 0.25}),
         rc(t0 + 1, {"NVDAUSD": 0.25}),          # gross 1.00: ok
         rc(t0 + 2, {"NVDAUSD": -0.25}),         # gross 1.00: ok (sign flip)
         rc(t0 + 3, {"BTCUSD": 0.0, "NVDAUSD": 0.25}),
         ]
    res = run(r, [])
    assert res["refused"] == []
    uni = dict(UNI, SOLUSD="crypto")
    r2 = r + [rc(t0 + 4, {"SOLUSD": 0.25, "BTCUSD": 0.01})]   # 1.01 -> refused whole
    res = ab.replay_book("A", r2, [], uni, start_ms=MON, end_ms=MON + D, rules=NOFEE)
    assert [x[1] for x in res["refused"]] == ["gross_cap"]


def test_forex_and_unknown_refused():
    t0 = MON + H
    res = run([rc(t0, {"EURUSD": 0.1}), rc(t0 + 1, {"DOGEUSD": 0.1})], [])
    assert [x[1] for x in res["refused"]] == ["excluded_class:EURUSD", "unknown_pair:DOGEUSD"]


def test_bad_values_refused():
    t0 = MON + H
    res = run([rc(t0, {"BTCUSD": float("nan")}), rc(t0 + 1, {"BTCUSD": True}),
               rc(t0 + 2, {"BTCUSD": "0.1"})], [])
    assert len(res["refused"]) == 3


def test_48_changes_per_day_cap():
    t0 = MON + H
    # every target distinct, so a refused receipt cannot make the next one a no-op
    r = [rc(t0 + i * 60_000, {"BTCUSD": round((i + 1) * 0.004, 3)}) for i in range(50)]
    res = run(r, [])
    assert sum(1 for x in res["refused"] if x[1] == "day_cap") == 2
    # the next UTC day gets a fresh allowance
    r += [rc(MON + D + H, {"BTCUSD": 0.05})]
    res = run(r, [])
    assert sum(1 for x in res["refused"] if x[1] == "day_cap") == 2


def test_noops_and_refusals_do_not_count_toward_day_cap():
    t0 = MON + H
    r = [rc(t0 + i, {"BTCUSD": 0.1}) for i in range(60)]            # 1 change, 59 no-ops
    r += [rc(t0 + 100 + i, {"BTCUSD": 0.9}) for i in range(60)]     # all refused
    r += [rc(t0 + 1000 + i, {"BTCUSD": 0.2 if i % 2 else 0.1}) for i in range(47)]
    res = run(r, [])
    assert not any(x[1] == "day_cap" for x in res["refused"])


def test_stale_seq_refused():
    t0 = MON + H
    res = run([rc(t0, {"BTCUSD": 0.1}, seq=5), rc(t0 + 1, {"BTCUSD": 0.2}, seq=5)], [])
    assert res["refused"][0][1] == "stale_seq"


# ── elimination ─────────────────────────────────────────────────────────────
def _dd_book(drop_frac):
    """Long 0.25 at 100, price falls so equity falls by drop_frac at day end."""
    t0 = MON + H
    p1 = 100.0 * (1 - drop_frac / 0.25)
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("BTCUSD", t0 + 2 * H, p1)]
    return run([rc(t0, {"BTCUSD": 0.25})], ticks, end=MON + 3 * D)


def test_elimination_7_99_survives():
    res = _dd_book(0.0799)
    assert res["eliminated_at_ms"] is None


def test_elimination_exactly_8_survives_strict_rule():
    res = _dd_book(0.08)
    assert res["daily"][0]["eod_equity"] == pytest.approx(0.92)
    assert res["eliminated_at_ms"] is None


def test_elimination_8_01_eliminated_at_day_end():
    res = _dd_book(0.0801)
    assert res["eliminated_at_ms"] == MON + D
    assert res["equity"] == pytest.approx(1 - 0.0801)       # closed at day-end mark
    assert res["balance"] == pytest.approx(res["equity"])


def test_elimination_measured_from_ath_not_start():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0),
             tick("BTCUSD", t0 + 2 * H, 140.0),            # day 1 EOD 1.10
             tick("BTCUSD", MON + D + 2 * H, 100.0)]       # day 2 EOD 1.00 < 0.92*1.10
    res = run([rc(t0, {"BTCUSD": 0.25})], ticks, end=MON + 3 * D)
    assert res["eliminated_at_ms"] == MON + 2 * D


def test_no_fills_after_elimination():
    t0 = MON + H
    ticks = [tick("BTCUSD", t0 + 3000, 100.0), tick("BTCUSD", t0 + 2 * H, 50.0),
             tick("BTCUSD", MON + D + 2 * H, 60.0)]
    res = run([rc(t0, {"BTCUSD": 0.25}), rc(MON + D + H, {"BTCUSD": 0.1})], ticks)
    assert res["eliminated_at_ms"] == MON + D
    assert len(res["fills"]) == 1 and res["refused"][0][1] == "eliminated"
    assert all(w["paid"] == 0 for w in res["weeks"])


# ── activity ────────────────────────────────────────────────────────────────
def _flip_book(bucket_starts, n_per_bucket=1, end=MON + 7 * D, drift=True):
    """Fills in the given 12h buckets, then a final week-end price gain."""
    ticks, rs, s = [], [], 0
    for b in bucket_starts:
        for j in range(n_per_bucket):
            t = b + j * 60_000
            s += 1
            rs.append(rc(t, {"BTCUSD": 0.2 if s % 2 else 0.1}))
            ticks.append(tick("BTCUSD", t + 3000, 100.0))
    ticks.append(tick("BTCUSD", end - H, 100.0))
    rs.append(rc(end - H - 10_000, {"BTCUSD": 0.0}))   # realise at 100, flat
    return rs, ticks


def test_activity_3_vs_4_buckets():
    b = [MON + 1 * H, MON + 13 * H, MON + D + H]
    # closing fill lands in the last bucket of the week, so 3 listed + close = 4
    rs, tk = _flip_book(b)
    res = run(rs, tk)
    assert res["weeks"][0]["buckets"] == 4 and res["weeks"][0]["active"]
    rs, tk = _flip_book(b[:2])
    res = run(rs, tk)
    assert res["weeks"][0]["buckets"] == 3 and not res["weeks"][0]["active"]


def test_two_fills_one_bucket_count_once():
    rs, tk = _flip_book([MON + H, MON + 13 * H], n_per_bucket=3)
    res = run(rs, tk)
    assert len(res["fills"]) == 7
    assert res["weeks"][0]["buckets"] == 3


def test_bucket_boundary_at_noon():
    assert ab.bucket_of(MON + 12 * H - 1) != ab.bucket_of(MON + 12 * H)
    assert ab.bucket_of(MON + 12 * H) == ab.bucket_of(MON + 24 * H - 1)
    # fills at 11:59:59.999 and 12:00:00.000 are two buckets
    t1, t2 = MON + 12 * H - 1 - 3000, MON + 12 * H - 3000
    rs = [rc(t1, {"BTCUSD": 0.1}), rc(t2, {"BTCUSD": 0.2})]
    tk = [tick("BTCUSD", t1 + 3000, 100.0), tick("BTCUSD", t2 + 3000, 100.0)]
    res = run(rs, tk)
    assert [ab.bucket_of(f["t_ms"]) for f in res["fills"]] == [
        ab.bucket_of(MON), ab.bucket_of(MON) + 1]
    assert res["weeks"][0]["buckets"] == 2


def test_refused_targets_are_not_activity():
    rs = [rc(MON + i * 12 * H + H, {"BTCUSD": 0.9}) for i in range(14)]
    tk = [tick("BTCUSD", MON + i * 12 * H + H + 5000, 100.0) for i in range(14)]
    res = run(rs, tk)
    assert res["weeks"][0]["buckets"] == 0


# ── weekly HWM payout ───────────────────────────────────────────────────────
def _active_week(ws, last_px, base=100.0, close=True):
    """Four fills in four buckets of week `ws` at `base`, then (optionally) a close at
    last_px in the final day. Leaves a 0.25 long open when close=False."""
    rs, tk = [], []
    for i, lev in enumerate((0.25, 0.1, 0.25, 0.1, 0.25)):
        t = ws + i * 12 * H + H
        rs.append(rc(t, {"BTCUSD": lev}))
        tk.append(tick("BTCUSD", t + 3000, base))
    tk.append(tick("BTCUSD", ws + 6 * D + 2 * H, last_px))
    if close:
        t = ws + 6 * D + 2 * H - 3000
        rs.append(rc(t, {"BTCUSD": 0.0}))
    return rs, tk


def test_hwm_gain_realised():
    rs, tk = _active_week(MON, 104.0)         # +4% on 0.25 long = +0.01
    res = run(rs, tk)
    w = res["weeks"][0]
    assert w["active"] and w["gain"] == pytest.approx(0.01) and w["paid"] == w["gain"]
    assert res["hwm"] == pytest.approx(1.01)


def test_hwm_unrealised_loss_term():
    # realise +0.01 on BTC, then hold an ETH long that is down at week end
    rs, tk = _active_week(MON, 104.0)
    t = MON + 6 * D + 3 * H
    rs.append(rc(t, {"ETHUSD": 0.2}))
    tk += [tick("ETHUSD", t + 3000, 100.0), tick("ETHUSD", t + 4 * H, 98.0)]
    res = run(rs, tk)
    w = res["weeks"][0]
    unr = -0.2 * 1.01 * 0.02                  # 0.2x of equity 1.01, down 2%
    assert w["eow_unrealised"] == pytest.approx(unr, rel=1e-9)
    assert w["gain"] == pytest.approx(0.01 + unr, rel=1e-9)


def test_unrealised_gain_not_paid_on_balance_basis():
    rs, tk = _active_week(MON, 110.0, close=False)
    res = run(rs, tk)
    assert res["weeks"][0]["eow_unrealised"] > 0
    assert res["weeks"][0]["gain"] == 0.0


def test_equity_basis_option():
    rs, tk = _active_week(MON, 104.0, close=False)
    res = run(rs, tk, r=rules(fee_bps=MappingProxyType({}), fee_bps_default=0.0,
                              hwm_basis="equity"))
    assert res["weeks"][0]["gain"] == pytest.approx(0.01)


def test_losing_week_pays_zero_and_hwm_holds():
    rs, tk = _active_week(MON, 104.0)
    rs2, tk2 = _active_week(MON + 7 * D, 98.0)
    res = run(rs + rs2, tk + tk2, end=MON + 14 * D)
    w1, w2 = res["weeks"]
    assert w1["paid"] == pytest.approx(0.01)
    assert w2["active"] and w2["paid"] == 0.0 and w2["prev_hwm"] == pytest.approx(1.01)
    assert res["hwm"] == pytest.approx(1.01)


def test_recovery_week_pays_only_above_old_high():
    rs, tk = _active_week(MON, 104.0)                         # 1.01
    rs2, tk2 = _active_week(MON + 7 * D, 96.0)                # -0.01 -> 1.00
    rs3, tk3 = _active_week(MON + 14 * D, 108.0)              # +0.02*... -> above 1.01
    res = run(rs + rs2 + rs3, tk + tk2 + tk3, end=MON + 21 * D)
    bal = res["weeks"][2]["eow_balance"]
    assert res["weeks"][2]["paid"] == pytest.approx(bal - 1.01)


def test_inactive_week_moves_hwm():
    rs, tk = _active_week(MON, 104.0)                          # paid, hwm 1.01
    # week 2: one fill only (inactive) but a big realised gain -> hwm moves, paid 0
    t = MON + 7 * D + H
    rs += [rc(t, {"BTCUSD": 0.25}), rc(t + 2 * H, {"BTCUSD": 0.0})]
    tk += [tick("BTCUSD", t + 3000, 100.0), tick("BTCUSD", t + 2 * H + 3000, 108.0)]
    # week 3: active, small gain that would be paid off the OLD high but not the new one
    rs3, tk3 = _active_week(MON + 14 * D, 102.0)
    res = run(rs + rs3, tk + tk3, end=MON + 21 * D)
    w1, w2, w3 = res["weeks"]
    assert not w2["active"] and w2["paid"] == 0.0 and w2["gain"] > 0
    assert w3["prev_hwm"] == pytest.approx(w2["eow_balance"])
    assert w3["paid"] == pytest.approx(w3["eow_balance"] - w2["eow_balance"], rel=1e-9)
    assert w3["paid"] < w3["eow_balance"] - 1.01          # the old high would have paid more


def test_inactive_losing_week_cannot_preserve_high():
    rs, tk = _active_week(MON, 104.0)                          # hwm 1.01
    t = MON + 7 * D + H
    rs += [rc(t, {"BTCUSD": 0.25}), rc(t + 2 * H, {"BTCUSD": 0.0})]
    tk += [tick("BTCUSD", t + 3000, 100.0), tick("BTCUSD", t + 2 * H + 3000, 98.0)]
    res = run(rs, tk, end=MON + 14 * D)
    # balance 1.005, hwm stays 1.01 (max), the loss is not forgiven
    assert res["hwm"] == pytest.approx(1.01)


def test_week_boundaries_are_monday_utc():
    assert ab.week_start_ms(MON + 7 * D - 1) == MON
    assert ab.week_start_ms(MON + 7 * D) == MON + 7 * D
    assert ab.week_start_ms(MON - 1) == MON - 7 * D


# ── pool split and entities ─────────────────────────────────────────────────
def test_pool_split_pro_rata_losing_account_does_not_net():
    books = {"E1": {"E1_1": 0.03, "E1_2": 0.0}, "E2": {"E2_1": 0.01}}
    w = ab.weekly_weights(books, None)
    assert w == {"E1_1": pytest.approx(0.75), "E2_1": pytest.approx(0.25)}
    assert ab.entity_weights(books, w) == {"E1": pytest.approx(0.75), "E2": pytest.approx(0.25)}
    # negative inputs (a caller passing a raw loss) are floored per account
    books["E1"]["E1_2"] = -0.02
    assert ab.weekly_weights(books, None) == w


def test_entity_below_bond_gets_zero():
    books = {"E1": {"E1_1": 0.03}, "E2": {"E2_1": 0.01}}
    w = ab.weekly_weights(books, {"E1": 39_999.99, "E2": 40_000})
    assert w == {"E2_1": pytest.approx(1.0)}
    w = ab.weekly_weights(books, {"E2": 40_000})              # missing bond = 0
    assert w == {"E2_1": pytest.approx(1.0)}


def test_all_zero_week_burns():
    assert ab.weekly_weights({"E1": {"a": 0.0}, "E2": {"b": 0.0}}, None) == {}
    assert ab.weekly_weights({}, None) == {}
    assert ab.weekly_weights({"E1": {"a": 0.5}}, {"E1": 1.0}) == {}


def test_entity_eligible():
    assert ab.entity_eligible(40_000)
    assert not ab.entity_eligible(39_999.999)
    assert not ab.entity_eligible(10_000)
    assert not ab.entity_eligible(None)
    assert not ab.entity_eligible(float("nan"))


SS58 = "5HBg742kVS1KXhKQGJhEMsyNcxatQzpyttipazfqGJSBS98o"


def test_parse_entity_commit():
    assert ab.parse_entity_commit(f"sn89entity:{SS58}") == SS58
    assert ab.parse_entity_commit(SS58) is None
    assert ab.parse_entity_commit(f"sn89entity:{SS58[:-1]}") is None       # short
    assert ab.parse_entity_commit(f"sn89entity:{SS58}A") is None           # long
    assert ab.parse_entity_commit(f"sn89entity:{SS58[:-1]}p") is None      # bad checksum
    assert ab.parse_entity_commit("sn89entity:0" + SS58[1:]) is None       # not base58
    assert ab.parse_entity_commit("sn89entity:") is None
    assert ab.parse_entity_commit(None) is None


# ── receipts ────────────────────────────────────────────────────────────────
def test_receipt_root_and_verify_hook():
    r1 = dict(rc(MON + 1, {"BTCUSD": 0.1, "ETHUSD": -0.2}), sig="ab")
    r2 = dict(rc(MON + 2, {"BTCUSD": 0.0}), sig="cd")
    root = ab.receipts_root([r2, r1])
    assert root == ab.receipts_root([r1, r2])
    assert root == hf.merkle_root([hf.leaf(ab.receipt_signing_bytes(r))
                                   for r in (r1, r2)])
    assert ab.receipts_root([]) == "00" * 32
    # key order inside targets does not change the bytes (canonical JSON)
    r1b = dict(r1, targets={"ETHUSD": -0.2, "BTCUSD": 0.1})
    assert ab.receipt_signing_bytes(r1) == ab.receipt_signing_bytes(r1b)
    seen = []
    assert ab.verify_receipt(r1, lambda m, s: seen.append((m, s)) or True)
    assert seen == [(ab.receipt_signing_bytes(r1), "ab")]
    assert not ab.verify_receipt(r1, lambda m, s: False)
    assert not ab.verify_receipt(dict(r1, sig=""), lambda m, s: True)

    def boom(m, s):
        raise ValueError
    assert not ab.verify_receipt(r1, boom)


def test_rules_as_of_and_immutable():
    assert ab.book_rules_as_of(0) is ab.RULES_V1
    assert ab.book_rules_as_of(2e9)["per_pair_max_lev"] == 0.25
    with pytest.raises(TypeError):
        ab.RULES_V1["gross_max_lev"] = 2.0


def test_deterministic_and_inputs_untouched():
    rs, tk = _active_week(MON, 104.0)
    import copy
    rs0, tk0 = copy.deepcopy(rs), copy.deepcopy(tk)
    a = run(rs, list(reversed(tk)))
    b = run(rs, tk)
    assert a["weeks"] == b["weeks"] and a["series"] == b["series"]
    assert rs == rs0 and tk == tk0


def test_replay_books_groups_by_account():
    rs, tk = _active_week(MON, 104.0)
    rs2 = [dict(r, account="B") for r in rs[:2]]
    out = ab.replay_books(rs + rs2, tk, UNI, start_ms=MON, end_ms=MON + 7 * D, rules=NOFEE)
    assert set(out) == {"A", "B"}
    assert out["A"]["weeks"][0]["paid"] == pytest.approx(0.01)
    assert out["B"]["weeks"][0]["paid"] == 0.0
