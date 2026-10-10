"""Markets V3 (Whit 2026-10-09): bets during the window, priced off the live tick, crowd-nudged,
spread, late cutoff, no look-ahead, deterministic."""
import time
from decimal import Decimal

import pytest

from sn89_signals import config, hf, markets

ENTITY = "5DPGU1Lw8zVHs6mCC6Q1MHr4cXEwwBf8mBXc2xNAxLn6EbV1"
W = 180_000


def _start(window="15m"):
    secs = config.MARKETS_WINDOWS[window]
    return (int(time.time()) // secs - 8) * secs


@pytest.fixture
def v3(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_FROM_UNIX", 1)
    monkeypatch.setattr(config, "MARKETS_V3_FROM_UNIX", 1)
    monkeypatch.setattr(config, "MARKETS_COLLATERAL_FROM_UNIX", 0)
    # starts come from the clock; these price off HF tick rows, so keep them off the V4 oracle arm
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", 2**62)
    return config


def _payload(start, side="UP", dollars="10.00", n=1, pair="BTCUSD", window="15m"):
    return {"kind": "mk.bet", "market_id": markets.market_id(pair, window, start), "side": side,
            "dollars": dollars, "account": f"{ENTITY}_{n}", "trade_pair": pair}


def _bet(key, t_s, side="UP", dollars="10.00", n=1, seq=0):
    t_us = int(t_s * 1_000_000)
    return {"key": key, "account": f"{ENTITY}_{n}", "side": side, "dollars": Decimal(dollars),
            "order": ((t_us // 1000) // W * W, t_us, ENTITY, seq)}


def _series(start, end, px, step_s=5, asset="BTCUSD"):
    """Ticks every step_s from the target minute through the end; px(t) gives the price."""
    return [{"a": asset, "t": t * 1000, "b": None, "k": None, "p": px(t)}
            for t in range(start - 60, end + 1, step_s)]


SIG = Decimal("0.0001")     # per second: ~3%/day, the crypto fallback


# ── the model ────────────────────────────────────────────────────────────────
def test_norm_cdf_is_the_standard_normal_to_1e7():
    assert abs(markets.norm_cdf(Decimal(0)) - Decimal("0.5")) < Decimal("1e-7")
    assert abs(markets.norm_cdf(Decimal("1.959964")) - Decimal("0.975")) < Decimal("1e-6")
    assert abs(markets.norm_cdf(Decimal("-1.959964")) - Decimal("0.025")) < Decimal("1e-6")
    assert markets.norm_cdf(Decimal(50)) == 1 and markets.norm_cdf(Decimal(-50)) == 0


def test_model_price_is_monotone_in_distance_and_in_time_left(v3):
    tgt = Decimal(100)
    ps = [markets.p_model(tgt * (1 + Decimal(k) / 10000), tgt, SIG, Decimal(300)) for k in range(-20, 21)]
    assert ps == sorted(ps) and ps[0] < Decimal("0.5") < ps[-1]
    assert markets.p_model(tgt, tgt, SIG, Decimal(300)) == pytest.approx(Decimal("0.5"), abs=Decimal("0.001"))
    # above the target, certainty fades as more time is left; below, it rises toward 50/50
    above = [markets.p_model(Decimal("100.1"), tgt, SIG, Decimal(t)) for t in (30, 120, 600, 3600)]
    assert above == sorted(above, reverse=True) and above[0] > Decimal("0.9")
    below = [markets.p_model(Decimal("99.9"), tgt, SIG, Decimal(t)) for t in (30, 120, 600, 3600)]
    assert below == sorted(below) and below[0] < Decimal("0.1")
    # clamped: never free, never certain
    assert markets.p_model(Decimal(200), tgt, SIG, Decimal(1)) == 1 - config.MARKETS_V3_P_FLOOR
    assert markets.p_model(Decimal(50), tgt, SIG, Decimal(1)) == config.MARKETS_V3_P_FLOOR


def test_anchor_makes_an_empty_market_quote_the_model_price(v3):
    for p in (Decimal("0.2"), Decimal("0.5"), Decimal("0.913")):
        a = markets.anchor(p, Decimal(100))
        assert abs(markets.price_up(a, Decimal(0), Decimal(100)) - p) < Decimal("1e-30")


def test_sigma_from_minute_marks_and_the_fallback(v3):
    day = _start() // 86400 * 86400 - 86400
    # alternating minute averages 100 / 100.1 -> every return is ±ln(1.001)
    rows = [{"a": "BTCUSD", "t": (day + 60 * i + 7) * 1000, "b": None, "k": None, "p": 100.0 + 0.1 * (i % 2)}
            for i in range(0, 300)]
    s = markets.sigma_per_s(rows, day)
    want = (Decimal("1.001").ln() / Decimal(60).sqrt())
    assert abs(s - want) < Decimal("1e-9")
    assert markets.sigma_per_s(rows[:100], day) is None                      # too few returns
    assert markets.sigma_fallback("BTCUSD", _start()) == config.MARKETS_V3_SIGMA_FALLBACK["crypto"]
    # a session gap (missing minutes) contributes no return
    gap = [r for r in rows if not (day + 60 * 100 <= r["t"] // 1000 < day + 60 * 200)]
    assert markets.sigma_per_s(gap, day) == s


# ── entry rules ──────────────────────────────────────────────────────────────
def test_entry_intervals_and_the_late_cutoff(v3):
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    assert markets.entry_intervals(mid) == [(s - 900, s - 60), (s + 5, e - 180)]
    ok = [s - 900, s - 61, s + 5, e - 181]
    for t in ok:
        markets.validate_bet(_payload(s), ENTITY, t)
    bad = [(s - 901, "market_not_open"), (s - 60, "target_forming"), (s, "target_forming"),
           (s + 4, "target_forming"), (e - 180, "market_closed"), (e - 1, "market_closed"), (e + 10, "market_closed")]
    for t, why in bad:
        with pytest.raises(hf.HFRejected, match=why):
            markets.validate_bet(_payload(s), ENTITY, t)
    h = s // 3600 * 3600
    assert markets.entry_intervals(markets.market_id("BTCUSD", "1h", h))[1] == (h + 5, h + 3600 - 300)


def test_before_the_stamp_a_market_has_one_interval_and_prices_as_before(v3, monkeypatch, tmp_path):
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    bets = [_bet("a", s - 400, "UP", "10.00", 1, 1), _bet("b", s - 300, "DOWN", "25.00", 2, 2),
            _bet("c", s + 100, "UP", "10.00", 3, 3)]
    rows = _series(s, e, lambda t: 100.0 + (t - s) * 0.001)

    def grade(stamp, name):
        monkeypatch.setattr(config, "MARKETS_V3_FROM_UNIX", stamp)
        db = markets._db(str(tmp_path / name))
        for i, b in enumerate(bets):
            db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                       (b["key"], mid, b["account"], b["side"], str(b["dollars"]), b["order"][0], b["order"][1], ENTITY, i))
        markets.grade_market(db, mid, rows)
        return db.execute("SELECT key, shares, pnl, status FROM results ORDER BY key").fetchall()

    before = grade(2 ** 40, "before")             # V3 not armed for this market
    assert dict((k, st) for k, _s, _p, st in before)["c"] == "ignored:outside_entry_window"
    monkeypatch.setattr(config, "MARKETS_V3_FROM_UNIX", 2 ** 40)
    assert markets.entry_intervals(mid) == [(s - 900, s - 60)]
    # the pre-V3 replay path is byte-identical to the crowd-only LMSR
    rep = markets.replay(bets[:2], start=s)
    sh = markets.shares_for(Decimal("10.00"), "UP", Decimal(0), Decimal(0), Decimal(100))
    assert rep[0]["shares"] == sh and rep[0]["lmsr_shares"] == sh
    assert [r[1] for r in before[:2]] == [str(rep[0]["shares"]), str(rep[1]["shares"])]
    # armed AFTER this market started: still identical for it
    assert grade(s + 1, "later") == before
    # armed before it: the in-window bet now counts, pre-start rows unchanged
    after = grade(1, "after")
    assert after[:2] == before[:2]
    assert dict((k, st) for k, _s, _p, st in after)["c"] == "ok"


# ── in-window pricing ────────────────────────────────────────────────────────
def test_in_window_bet_is_filled_at_the_first_tick_after_receipt_plus_the_delay(v3):
    """The latency guard: the bettor commits before the price he is filled at exists."""
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    rows = _series(s, e, lambda t: 100.0)
    t_bet = s + 303                                                              # off the 5 s tick grid
    d = config.MARKETS_V3_FILL_DELAY_S
    rows.append({"a": "BTCUSD", "t": t_bet * 1000 - 1, "b": None, "k": None, "p": 150.0})       # what he saw
    rows.append({"a": "BTCUSD", "t": (t_bet + d) * 1000 - 1, "b": None, "k": None, "p": 150.0})  # 1 ms too early
    rows.append({"a": "BTCUSD", "t": (t_bet + d) * 1000 + 1, "b": None, "k": None, "p": 100.2})  # the fill
    pricer = markets.make_pricer(mid, rows, 100.0, SIG)
    ctx = pricer(_bet("x", t_bet))
    assert ctx["tick_t"] == (t_bet + d) * 1000 + 1 and ctx["tick_mark"] == 100.2
    tau = Decimal(e) - Decimal(ctx["tick_t"]) / 1000 - Decimal(30)
    assert ctx["p"] == markets.p_model(Decimal("100.2"), Decimal(100), SIG, tau)
    assert Decimal("0.5") < ctx["p"] < Decimal("0.9")                           # not the 150 he saw
    assert pricer(_bet("y", s - 100)) is None                                    # pre-start: crowd only
    # a feed that stops: no fill within the wait -> ignored, never priced blind
    stale = [r for r in rows if r["t"] < (t_bet + d) * 1000]
    assert markets.make_pricer(mid, stale, 100.0, SIG)(_bet("z", t_bet)) == {"ignore": "no_fill"}
    assert markets.make_pricer(mid, rows, None, SIG)(_bet("z", t_bet)) == {"ignore": "no_target"}
    # the front end shows the model at the latest tick before now
    q = markets.quote_context(mid, rows, 100.0, SIG, t_bet)
    assert q["tick_mark"] == 150.0 and q["p"] == Decimal(1) - config.MARKETS_V3_P_FLOOR


def test_spread_is_charged_on_in_window_bets_only_and_the_crowd_still_moves(v3):
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    rows = _series(s, e, lambda t: 100.0)
    pricer = markets.make_pricer(mid, rows, 100.0, SIG)
    pre = _bet("pre", s - 300, "UP", "10.00", 1, 1)
    live1 = _bet("l1", s + 300, "UP", "10.00", 2, 2)
    live2 = _bet("l2", s + 300.5, "UP", "10.00", 3, 3)
    rep = markets.replay([pre, live1, live2], start=s, pricer=pricer)
    r0, r1, r2 = rep
    assert r0["shares"] == r0["lmsr_shares"] and r0.get("p_model") is None     # pre-start: no spread
    p_avg = markets.CTX.divide(Decimal("10.00"), r1["lmsr_shares"])
    assert r1["price_paid"] == markets.CTX.add(p_avg, markets.spread_for(r1["tau"]))
    assert r1["spread"] > config.MARKETS_V3_SPREAD                        # the latency part is on
    # the defence grows as the end nears: more spread with 3 minutes left than with 10
    assert markets.spread_for(Decimal(180)) > markets.spread_for(Decimal(600)) > config.MARKETS_V3_SPREAD
    assert r1["shares"] == markets.CTX.divide(Decimal("10.00"), r1["price_paid"]).quantize(markets.SHARE_Q, rounding="ROUND_DOWN")
    assert r1["shares"] < r1["lmsr_shares"]
    # the second UP bet half a second later pays more: the first one moved the market
    assert r2["price_paid"] > r1["price_paid"] and r2["p_model"] == r1["p_model"]
    # and what the front end quotes is what the next bet pays
    p_now = markets.p_model(Decimal(100), Decimal(100), SIG, Decimal(e - (s + 301)) - 30)
    q = markets.market_price_up(rep, start=s, p_now=p_now)
    rep2 = markets.replay([pre, live1, live2, _bet("l3", s + 301, "UP", "1.00", 4, 4)], start=s, pricer=pricer)
    q_after = markets.market_price_up(rep2, start=s, p_now=p_now)
    # the bet pays its LMSR average, between the marginal quote before it and the one after it
    tau = rep2[-1]["tau"]
    assert markets.buy_price("UP", q, live=True, tau_s=tau) <= rep2[-1]["price_paid"] <= markets.buy_price("UP", q_after, live=True, tau_s=tau)
    assert markets.buy_price("DOWN", q, live=False) == markets.CTX.subtract(Decimal(1), q)


def test_a_bet_cannot_buy_the_side_the_tape_already_shows_winning_at_50_50(v3):
    """The whole point: late in the window, far from the target, the winning side is expensive."""
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    rows = _series(s, e, lambda t: 100.0 if t < s else 100.3)                # +30 bps right after the start
    pricer = markets.make_pricer(mid, rows, 100.0, SIG)
    late = markets.replay([_bet("late", e - 200, "UP", "10.00", 1, 1)], start=s, pricer=pricer)[0]
    assert late["p_model"] > Decimal("0.95") and late["shares"] < Decimal("10.2")   # pays ~$10.1 on $10


def test_grading_is_deterministic_and_records_the_live_prices(v3, tmp_path):
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    rows = _series(s, e, lambda t: 100.0 + 0.01 * ((t * 7919) % 13 - 6))
    bets = [_bet("a", s - 500, "UP", "10.00", 1, 1), _bet("b", s + 10, "DOWN", "20.00", 2, 2),
            _bet("c", s + 400, "UP", "5.00", 3, 3), _bet("d", e - 100, "UP", "5.00", 4, 4)]   # d: past the cutoff
    outs = []
    for name in ("one", "two"):
        db = markets._db(str(tmp_path / name))
        for i, b in enumerate(bets):
            db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                       (b["key"], mid, b["account"], b["side"], str(b["dollars"]), b["order"][0], b["order"][1], ENTITY, i))
        markets.grade_market(db, mid, rows, SIG)
        outs.append((db.execute("SELECT * FROM results ORDER BY key").fetchall(),
                     db.execute("SELECT * FROM v3_prices ORDER BY key").fetchall()))
    assert outs[0] == outs[1]
    res = {r[0]: r for r in outs[0][0]}
    assert res["d"][6] == "ignored:outside_entry_window"
    assert {r[0] for r in outs[0][1]} == {"b", "c"}                       # only in-window bets carry a model price
    assert all(r[4] >= (s + 10 + config.MARKETS_V3_FILL_DELAY_S) * 1000 for r in outs[0][1] if r[0] == "b")
    assert Decimal(res["c"][4]) > 0


def test_sigma_for_market_waits_for_the_day_then_falls_back(v3, tmp_path, monkeypatch):
    from sn89_signals import hf_grade
    monkeypatch.setattr(hf_grade, "LOCAL_TICK_SRC", "")
    monkeypatch.setattr(hf_grade, "_fetch_text", lambda url, **k: None)       # nothing fetchable
    db = markets._db(str(tmp_path))
    s = _start()
    assert markets.sigma_for_market(db, "http://x", str(tmp_path / "t"), "BTCUSD", s) is None
    fb = markets.sigma_for_market(db, "http://x", str(tmp_path / "t"), "BTCUSD", s, final=True)
    assert fb == config.MARKETS_V3_SIGMA_FALLBACK["crypto"]
    assert db.execute("SELECT measured FROM v3_sigma WHERE asset='BTCUSD'").fetchone()[0] == 0
    # cached: the fetcher is never asked again
    monkeypatch.setattr(hf_grade, "_fetch_text", lambda url, **k: (_ for _ in ()).throw(AssertionError("refetched")))
    assert markets.sigma_for_market(db, "http://x", str(tmp_path / "t"), "BTCUSD", s) == fb


def test_ingest_stores_an_in_window_bet_only_for_a_v3_market(v3, tmp_path, monkeypatch):
    s = _start()
    e = {"submit": {"hk": ENTITY, "seq": 1, "payload": _payload(s)},
         "receipt": {"t_recv_us": (s + 100) * 1_000_000, "grid_t0_ms": (s + 100) * 1000}}
    assert markets.ingest_entries(markets._db(str(tmp_path / "a")), s * 1000, [e]) == 1
    monkeypatch.setattr(config, "MARKETS_V3_FROM_UNIX", 2 ** 40)
    assert markets.ingest_entries(markets._db(str(tmp_path / "b")), s * 1000, [e]) == 0
