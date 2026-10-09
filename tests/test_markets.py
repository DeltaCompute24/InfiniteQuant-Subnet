"""On-chain Markets (sn89_signals/markets.py): pricing, grading, scoring, ingest, blend."""
import time
from decimal import Decimal

import pytest

from sn89_signals import competitions, config, hf, markets

ENTITY = "5DPGU1Lw8zVHs6mCC6Q1MHr4cXEwwBf8mBXc2xNAxLn6EbV1"   # syntactically valid ss58 shape
B = Decimal(100)


def _start(window="15m"):
    """A recent aligned start on which BTCUSD is on the HF board."""
    secs = config.MARKETS_WINDOWS[window]
    return (int(time.time()) // secs - 4) * secs


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_FROM_UNIX", 1)
    return config


# ── LMSR ─────────────────────────────────────────────────────────────────────
def test_spending_buys_shares_whose_cost_is_the_stake():
    sh = markets.shares_for(Decimal("10.00"), "UP", Decimal(0), Decimal(0), B)
    cost = markets._cost(sh, Decimal(0), B) - markets._cost(Decimal(0), Decimal(0), B)
    assert Decimal("9.9999") < cost <= Decimal("10.00")       # quantized down: never more than paid
    assert Decimal(19) < sh < Decimal(21)                     # ~ $10 at 50c


def _bets(order):
    raw = {"a": ("UP", "40.00"), "b": ("DOWN", "25.00"), "c": ("UP", "10.00")}
    return [{"key": k, "account": f"{ENTITY}_{i}", "side": raw[k][0], "dollars": Decimal(raw[k][1]),
             "order": (0, i, ENTITY, i)} for i, k in enumerate(order)]


def test_replay_is_deterministic_and_order_matters():
    one = markets.replay(_bets("abc"), B)
    two = markets.replay(_bets("abc"), B)
    assert [(r["key"], r["shares"]) for r in one] == [(r["key"], r["shares"]) for r in two]
    other = {r["key"]: r["shares"] for r in markets.replay(_bets("cba"), B)}
    first = {r["key"]: r["shares"] for r in one}
    assert first["a"] != other["a"]          # who bets first gets the better price


def test_per_market_cap_ignores_the_excess_in_order(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_MAX_PER_MARKET", Decimal(50))
    bets = [{"key": f"k{i}", "account": f"{ENTITY}_1", "side": "UP", "dollars": Decimal(30),
             "order": (0, i, ENTITY, i)} for i in range(3)]
    st = [r["status"] for r in markets.replay(bets, B)]
    assert st == ["ok", "ignored:per_market_cap", "ignored:per_market_cap"]


def test_a_hedged_pair_in_one_entity_cannot_net_a_profit():
    """No pair of opposite bets in one entity is profitable in BOTH outcomes (LMSR prices are
    coherent); a true hedge (equal shares each side) nets <= 0, rounding favouring the maker."""
    for d_up, d_down in [(50, 50), (10, 90), (90, 10), (1, 1), (100, 30)]:
        bets = [{"key": "u", "account": f"{ENTITY}_1", "side": "UP", "dollars": Decimal(d_up), "order": (0, 1, ENTITY, 1)},
                {"key": "d", "account": f"{ENTITY}_2", "side": "DOWN", "dollars": Decimal(d_down), "order": (0, 2, ENTITY, 2)}]
        r = markets.replay(bets, B)
        nets = [sum(markets.bet_pnl(x["side"], x["shares"], x["dollars"], o) for x in r) for o in ("UP", "DOWN")]
        assert min(nets) <= 0, (d_up, d_down, nets)
    # equal shares on both sides: buy UP, then buy exactly as many DOWN shares
    up = markets.shares_for(Decimal(50), "UP", Decimal(0), Decimal(0), B)
    cost_down = markets._cost(up, up, B) - markets._cost(up, Decimal(0), B)
    down = markets.shares_for(cost_down.quantize(Decimal("0.01")), "DOWN", up, Decimal(0), B)
    for o in ("UP", "DOWN"):
        payout = up if o == "UP" else down
        assert payout - (Decimal(50) + cost_down.quantize(Decimal("0.01"))) <= Decimal("0.01")


# ── grading ──────────────────────────────────────────────────────────────────
def _ticks(start, end, open_px, close_px):
    rows = [{"a": "BTCUSD", "t": (start - 60 + i) * 1000, "b": open_px - 1, "k": open_px + 1, "p": open_px}
            for i in range(0, 61, 5)]
    rows += [{"a": "BTCUSD", "t": (end - 60 + i) * 1000, "b": None, "k": None, "p": close_px}
             for i in range(0, 61, 5)]
    return rows


def test_tie_is_up_and_missing_minute_is_void():
    s = _start(); e = s + 900
    assert markets.resolve(_ticks(s, e, 100.0, 100.0), s, e)[0] == "UP"
    assert markets.resolve(_ticks(s, e, 100.0, 99.0), s, e)[0] == "DOWN"
    only_open = [r for r in _ticks(s, e, 100.0, 101.0) if r["t"] <= s * 1000]
    assert markets.resolve(only_open, s, e)[0] == "VOID"


def test_average_matches_the_testnet_rule():
    s = _start()
    rows = [{"a": "BTCUSD", "t": (s - 30) * 1000, "b": 10.0, "k": 12.0, "p": 99.0},   # mid 11.0
            {"a": "BTCUSD", "t": (s - 10) * 1000, "b": 0, "k": 0, "p": 13.0}]          # trade 13.0
    assert markets.average_marks(rows, (s - 60) * 1000, s * 1000) == markets.round_px((11.0 + 13.0) / 2)
    assert markets.round_px(2719.33417) == 2719.33


# ── bet validity ─────────────────────────────────────────────────────────────
def _payload(start, side="UP", dollars="10.00", account=None, pair="BTCUSD", window="15m"):
    return {"kind": "mk.bet", "market_id": markets.market_id(pair, window, start), "side": side,
            "dollars": dollars, "account": account or f"{ENTITY}_3", "trade_pair": pair}


def test_unarmed_network_refuses_every_bet(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_FROM_UNIX", 0)
    s = _start()
    with pytest.raises(hf.HFRejected, match="markets_not_live"):
        markets.validate_bet(_payload(s), ENTITY, s + 10)


def test_bet_validity_rules(armed):
    s = _start()
    markets.validate_bet(_payload(s), ENTITY, s - 300)
    markets.validate_bet(_payload(s, account=ENTITY), ENTITY, s - 300)         # direct miner
    from bittensor_wallet import Keypair
    other = Keypair.create_from_uri("//SomeoneElse").ss58_address
    bad = [(_payload(s, account=f"{other}_1"), "account_not_signer"),
           (_payload(s, account="not-an-address"), "bad_account"),
           (_payload(s, side="SIDEWAYS"), "bad_side"),
           (_payload(s, dollars="10.001"), "bad_dollars"),
           (_payload(s, dollars="1000.00"), "dollars_out_of_range"),
           ({**_payload(s), "market_id": f"UD:BTCUSD:15m:{s + 1}"}, "market_not_aligned"),
           ({**_payload(s), "trade_pair": "ETHUSD"}, "trade_pair_mismatch")]
    for p, why in bad:
        with pytest.raises(hf.HFRejected, match=why):
            markets.validate_bet(p, ENTITY, s - 300)


def test_entry_window_is_before_the_start_and_closes_before_the_averaging_minute(armed):
    """Every bet is a pure forecast: open one window before the start, closed from start - 60 s."""
    s = _start()
    assert markets.entry_interval(markets.market_id("BTCUSD", "15m", s)) == (s - 900, s - 60)
    assert markets.entry_interval(markets.market_id("BTCUSD", "1h", s // 3600 * 3600)) == \
        (s // 3600 * 3600 - 3600, s // 3600 * 3600 - 60)
    markets.validate_bet(_payload(s), ENTITY, s - 900)                         # opens inclusive
    markets.validate_bet(_payload(s), ENTITY, s - 61)                          # last full second counts
    for t, why in [(s - 901, "market_not_open"), (s - 59, "market_closed"), (s - 60, "market_closed"),
                   (s, "market_closed"), (s + 10, "market_closed"), (s + 899, "market_closed")]:
        with pytest.raises(hf.HFRejected, match=why):
            markets.validate_bet(_payload(s), ENTITY, t)


def test_replay_skips_bets_outside_the_entry_window(armed, tmp_path):
    """Replay decides: a bet stored by a lenient ingest is still skipped if it is outside the window."""
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    db = markets._db(str(tmp_path))
    rows = [("ok", s - 61), ("late", s - 59), ("during", s + 10), ("early", s - 901)]
    for i, (k, t) in enumerate(rows):
        db.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?)",
                   (k, mid, f"{ENTITY}_1", "UP", "10.00", (t * 1000) // 180_000 * 180_000, t * 1_000_000, ENTITY, i))
    markets.grade_market(db, mid, _ticks(s, e, 100.0, 101.0))
    st = dict(db.execute("SELECT key, status FROM results").fetchall())
    assert st == {"ok": "ok", "late": "ignored:outside_entry_window",
                  "during": "ignored:outside_entry_window", "early": "ignored:outside_entry_window"}
    pnl = dict(db.execute("SELECT key, pnl FROM results").fetchall())
    assert Decimal(pnl["ok"]) > 0 and all(Decimal(pnl[k]) == 0 for k in ("late", "during", "early"))
    # and the ingest-side re-check never stores them in the first place
    db2 = markets._db(str(tmp_path / "b"))
    stored = markets.ingest_entries(db2, s * 1000, [
        _entry(0, t * 1_000_000, i + 1, _payload(s)) for i, (_k, t) in enumerate(rows)])
    assert stored == 1


# ── scoring ──────────────────────────────────────────────────────────────────
def _res(account, pnl, dollars="10", outcome="UP"):
    return {"account": account, "pnl": Decimal(pnl), "dollars": Decimal(dollars), "outcome": outcome}


def test_gate_and_entity_rollup(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_MIN_RESOLVED", 20)
    skilled = [_res(f"{ENTITY}_1", "8") for _ in range(18)] + [_res(f"{ENTITY}_1", "-10") for _ in range(4)]
    skilled2 = [_res(f"{ENTITY}_2", "5") for _ in range(25)]
    coin = [_res("5Coin" + ENTITY[5:], "10" if i % 2 else "-10") for i in range(40)]
    few = [_res("5Few" + ENTITY[4:], "10") for _ in range(5)]
    s = markets.score_accounts(skilled + skilled2 + coin + few)
    assert set(s) == {ENTITY}                                   # both subaccounts roll up; others gated out
    assert s[ENTITY] == pytest.approx(18 * 8 - 40 + 25 * 5)
    voids = [_res(f"{ENTITY}_9", "0", outcome="VOID") for _ in range(50)]
    assert markets.score_accounts(voids) == {}


def test_weights_burn_the_remainder():
    w = markets.weights_from_scores({ENTITY: 10.0, "5X": 30.0}, {ENTITY: 7}, time.time())
    assert set(w) == {7, config.BURN_UID} and sum(w.values()) == pytest.approx(1.0)
    assert markets.weights_from_scores({}, {}, time.time()) == {config.BURN_UID: 1.0}


# ── the validator path: entries -> graded results, replay-safe ───────────────
def _entry(w, t_us, seq, payload):
    return {"submit": {"hk": ENTITY, "seq": seq, "payload": payload},
            "receipt": {"t_recv_us": t_us, "grid_t0_ms": t_us // 1000}}


def test_rebuild_from_scratch_matches_incremental(armed, tmp_path):
    s = _start(); e = s + 900
    mid = markets.market_id("BTCUSD", "15m", s)
    entries = [_entry(s * 1000, (s - 300 + i) * 1_000_000, i + 1,
                      _payload(s, side="UP" if i % 3 else "DOWN", account=f"{ENTITY}_{i % 4}",
                               dollars=f"{5 + i}.00")) for i in range(9)]
    ticks = _ticks(s, e, 100.0, 101.0)

    def build(cache, chunks):
        db = markets._db(str(cache))
        for w, es in chunks:
            markets.ingest_entries(db, w, es)
        markets.grade_market(db, mid, ticks)
        db.commit()
        rows = sorted(db.execute("SELECT key, shares, pnl, status FROM results").fetchall())
        db.close()
        return rows

    a = build(tmp_path / "a", [(s * 1000, entries)])
    b = build(tmp_path / "b", [(s * 1000, entries[5:]), (s * 1000, entries[:5])])   # arrival order differs
    assert a == b and len(a) == 9


def test_ingest_entries_ignores_other_kinds_and_invalid_bets(armed, tmp_path):
    s = _start()
    db = markets._db(str(tmp_path))
    hfcall = {"submit": {"hk": ENTITY, "seq": 1, "payload": {"trade_pair": "BTCUSD"}},
              "receipt": {"t_recv_us": (s + 5) * 1_000_000}}
    late = _entry(s * 1000, (s + 5) * 1_000_000, 2, _payload(s))
    ok = _entry(s * 1000, (s - 120) * 1_000_000, 3, _payload(s))
    assert markets.ingest_entries(db, s * 1000, [hfcall, late, ok]) == 1


# ── HF readers skip bets; blend gives markets its share ──────────────────────
def test_hf_readers_skip_markets_and_closers():
    assert hf.is_hf_call({"trade_pair": "BTCUSD"})
    assert not hf.is_hf_call({"kind": "mk.bet"}) and not hf.is_hf_call({"kind": "closers"})


def test_markets_share_appears_only_at_the_mainnet_cutover():
    """Every row before 2026-10-09 00:00Z is untouched; Markets enters only at that instant."""
    for eff, spec in config.COMP_WEIGHTS_HISTORY:
        assert ("markets" in spec) == (eff >= 1791504000)


def test_blend_with_a_markets_share():
    vec = {"lf": {1: 1.0}, "hf": {2: 1.0}, "closers": None, "markets": {3: 1.0}}
    w = competitions.combine(vec, {"lf": 0.4, "hf": 0.4, "closers": 0.1, "markets": 0.1})
    assert w[3] == pytest.approx(0.1) and w[config.BURN_UID] == pytest.approx(0.1)
    w0 = competitions.combine(vec, {"lf": 0.5, "hf": 0.5})                      # no share: ignored
    assert 3 not in w0


# ── ingest ───────────────────────────────────────────────────────────────────
class TestIngestMarkets:
    def _ingest(self, registered):
        import importlib
        from bittensor_wallet import Keypair
        hi = importlib.import_module("neurons.hf_ingest")
        ing = object.__new__(hi.Ingest)
        ing.kp = Keypair.create_from_uri("//IngestTestKey")
        ing.last_seq, ing.sent_ms, ing.windows = {}, {}, {}
        ing.closers_sent_ms, ing.markets_sent_ms = {}, {}
        ing.lock_index, ing._locks_loaded_at = {}, 9e18
        ing.registered, ing._reg_loaded_at = set(registered), 9e18
        ing.open_calls, ing.last_px = {}, {}
        ing._tick_ok_at = 0.0
        return ing

    def _frame(self, kp, payload, seq=1):
        ts = int(time.time() * 1000)
        sb = hf.submit_signing_bytes(kp.ss58_address, seq, "n" * 32, payload, ts)
        return {"v": 1, "kind": "hf.submit", "hk": kp.ss58_address, "seq": seq, "nonce": "n" * 32,
                "ts_miner": ts, "payload": payload, "sig": kp.sign(sb).hex()}

    def _open_payload(self, kp, monkeypatch=None, **kw):
        # Clock-independent: bet on the window after next with a 30-minute open lead, so the
        # entry window [start - 1800, start - 60) always contains "now".
        if monkeypatch is not None:
            monkeypatch.setattr(config, "MARKETS_OPEN_LEAD_S", 1800)
        start = (int(time.time()) // 900 + 2) * 900
        return {**_payload(start, account=f"{kp.ss58_address}_1", **kw)}

    def test_accepts_a_valid_bet_when_armed(self, armed, monkeypatch):
        from bittensor_wallet import Keypair
        ent = Keypair.create_from_uri("//MarketsEntity")
        ing = self._ingest({ent.ss58_address})
        out = ing.handle(self._frame(ent, self._open_payload(ent, monkeypatch)))
        assert out["kind"] == "hf.receipt", out.get("reason")
        assert ing.markets_sent_ms[f"{ent.ss58_address}_1"]
        assert not ing.lock_index and not ing.open_calls             # no HF pair lock, no open call

    def test_refuses_when_unarmed(self, monkeypatch):
        monkeypatch.setattr(config, "MARKETS_FROM_UNIX", 0)
        from bittensor_wallet import Keypair
        ent = Keypair.create_from_uri("//MarketsEntity")
        ing = self._ingest({ent.ss58_address})
        start = (int(time.time()) // 900 + 1) * 900
        out = ing.handle(self._frame(ent, _payload(start, account=f"{ent.ss58_address}_1")))
        assert out["kind"] == "hf.reject" and out["reason"] == "markets_not_live"

    def test_refuses_a_bet_on_the_running_window(self, armed, monkeypatch):
        from bittensor_wallet import Keypair
        # Pre-V3 rule. Pinned so the test does not flip the moment the V3 stamp passes (it did,
        # at 2026-10-09 15:00 UTC): under V3 a running window takes bets until its cutoff.
        monkeypatch.setattr(config, "MARKETS_V3_FROM_UNIX", 2**62)
        ent = Keypair.create_from_uri("//MarketsEntity")
        ing = self._ingest({ent.ss58_address})
        running = int(time.time()) // 900 * 900                 # started already: closed to bets
        out = ing.handle(self._frame(ent, _payload(running, account=f"{ent.ss58_address}_1")))
        assert out["kind"] == "hf.reject" and out["reason"] == "market_closed"

    def test_refuses_betting_for_someone_elses_account(self, armed, monkeypatch):
        from bittensor_wallet import Keypair
        ent = Keypair.create_from_uri("//MarketsEntity")
        other = Keypair.create_from_uri("//OtherEntity")
        ing = self._ingest({ent.ss58_address})
        p = self._open_payload(ent, monkeypatch)
        p["account"] = f"{other.ss58_address}_1"
        out = ing.handle(self._frame(ent, p))
        assert out["reason"] == "account_not_signer"


def _publish(root, start, end, entries_by_w, ticks):
    """Lay out a public HF feed (index.json + <w>/receipts.jsonl + <w>/ticks.jsonl) on disk."""
    import json
    W = 180_000
    ws = list(range(((start - 1200) * 1000) // W * W, (end * 1000) // W * W + W, W))
    for w in ws:
        d = root / str(w)
        d.mkdir(parents=True, exist_ok=True)
        (d / "receipts.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries_by_w.get(w, [])))
        (d / "ticks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ticks if w <= r["t"] < w + W))
    (root / "index.json").write_text(json.dumps({"windows": ws}))
    return "file://" + str(root)


def test_validator_path_replays_identically_from_the_public_feed(armed, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKETS_MIN_RESOLVED", 3)
    monkeypatch.setattr(hf_grade_mod(), "LOCAL_TICK_SRC", "")
    s = _start(); e = s + 900
    W = 180_000
    entries = {}
    for i in range(6):
        t = s - 600 + i * 60
        p = _payload(s, side="UP", account=f"{ENTITY}_1", dollars="10.00")
        entries.setdefault((t * 1000) // W * W, []).append(_entry(0, t * 1_000_000, i + 1, p))
    base = _publish(tmp_path / "feed", s, e, entries, _ticks(s, e, 100.0, 101.0))
    uid = {ENTITY: 11}
    now = e + config.MARKETS_GRADE_SETTLE_S + 5
    w1 = markets.markets_weights(uid, now, base, str(tmp_path / "c1"))
    # an incremental validator: synced once before grading was possible, then again later
    markets.markets_weights(uid, e - 30, base, str(tmp_path / "c2"))
    w2 = markets.markets_weights(uid, now, base, str(tmp_path / "c2"))
    assert w1 == w2 and w1.get(11, 0) > 0


def hf_grade_mod():
    from sn89_signals import hf_grade
    return hf_grade
