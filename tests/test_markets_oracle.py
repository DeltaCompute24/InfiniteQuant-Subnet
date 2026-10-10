"""Markets V4 oracle record: integrity, hash chain, value in effect, averages and fills."""
from decimal import Decimal

import pytest

from sn89_signals import config, oracle


def R(t, o, a="BTCUSD"):
    return {"t": t, "a": a, "o": o}


def test_window_root_is_order_independent_and_sensitive():
    rows = [R(1000, "1.0"), R(2000, "2.0", "ETHUSD"), R(3000, "3.0")]
    assert oracle.window_root(rows) == oracle.window_root(list(reversed(rows)))
    assert oracle.window_root(rows) != oracle.window_root([R(1000, "1.0"), R(2000, "2.00", "ETHUSD"), R(3000, "3.0")])
    assert oracle.window_root([]) == "00" * 32


def test_chain_verifies_and_breaks_on_any_rewrite():
    W = oracle.WINDOW_MS
    roots = {i * W: oracle.window_root([R(i * W + 5, str(i))]) for i in range(6)}
    batches, prev = [], oracle.GENESIS
    for b in range(2):
        ws = [b * 3 * W + i * W for i in range(3)]
        root = oracle.batch_root([(w, roots[w]) for w in ws])
        link = oracle.chain_link(prev, ws[0], 3, root)
        batches.append({"w0": ws[0], "n": 3, "root": root, "prev": prev, "link": link})
        prev = link
    assert oracle.verify_chain(batches, roots) == (True, "ok")
    bad = dict(roots)
    bad[W] = oracle.window_root([R(W + 5, "999")])
    ok, why = oracle.verify_chain(batches, bad)
    assert not ok and "root" in why
    tampered = [dict(batches[0], root="11" * 32), batches[1]]
    assert not oracle.verify_chain(tampered)[0]
    a = oracle.encode_anchor(batches[-1]["w0"], 3, batches[-1]["link"])
    assert oracle.decode_anchor(a) == {"w0": batches[-1]["w0"], "n": 3, "link": batches[-1]["link"]}
    assert len(a.encode()) <= 128


def test_value_in_effect_and_max_age(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_ORACLE_MAX_AGE_S", 90)
    rows = [R(10_000, "100.0"), R(13_000, "101.5")]
    assert oracle.value_at(rows, 12_999) == (10_000, Decimal("100.0"))
    assert oracle.value_at(rows, 13_000) == (13_000, Decimal("101.5"))
    assert oracle.value_at(rows, 9_999) is None
    assert oracle.value_at(rows, 13_000 + 90_001) is None          # recorder went quiet


def test_twap_weights_by_time_and_voids_on_a_gap(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_ORACLE_MAX_AGE_S", 90)
    rows = [R(0, "100"), R(30_000, "110"), R(45_000, "130")]
    # 0-30 s at 100, 30-45 at 110, 45-60 at 130
    assert oracle.twap(rows, 0, 60_000) == Decimal(100 * 30 + 110 * 15 + 130 * 15) / 60
    assert oracle.twap(rows, 10_000, 20_000) == Decimal(100)
    gap = [R(0, "100"), R(100_000, "101")]
    assert oracle.twap(gap, 0, 120_000) is None
    assert oracle.twap([R(0, "100")], 10_000, 70_000) == Decimal(100)
    assert oracle.twap([R(0, "100")], 60_000, 120_000) is None     # the end is past max age


def test_resolve_and_fill(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_ORACLE_MAX_AGE_S", 90)
    monkeypatch.setattr(config, "MARKETS_ORACLE_FILL_DELAY_S", 3)
    monkeypatch.setattr(config, "MARKETS_ORACLE_FILL_WAIT_S", 30)
    monkeypatch.setattr(config, "MARKETS_AVG_S", 60)
    start, end = 1_000, 1_900
    rows = [R(t * 1000, "100") for t in range(start - 70, start + 1, 30)]
    rows += [R(t * 1000, "101") for t in range(start + 3, end + 1, 3)]
    out, target, settle = oracle.resolve(rows, start, end)
    assert out == "UP" and target == 100.0 and settle == 101.0
    # flat at 101 after start+3: no change within the wait, so the value at receipt + 30 s
    assert oracle.fill_at(rows, (start + 10) * 1000) == ((start + 40) * 1000, 101.0)
    moving = [R(0, "100"), R(5_000, "100"), R(9_000, "100"), R(14_000, "102"), R(20_000, "103")]
    assert oracle.fill_at(moving, 4_000) == (14_000, 102.0)      # first change at/after receipt + 3 s
    assert oracle.fill_at(moving, 12_000) == (20_000, 103.0)
    assert oracle.fill_at([R(0, "100")], 70_000) is None            # nothing in effect at the wait
    assert oracle.resolve([R((start - 61) * 1000, "1")], start, end)[0] == "VOID"


def test_coin_map_is_gated_by_the_arm(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", 0)
    assert config.markets_oracle_coin("BTCUSD", 2_000_000_000) is None
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", 1_800_000_000)
    assert config.markets_oracle_coin("BTCUSD", 1_799_999_999) is None
    assert config.markets_oracle_coin("BTCUSD", 1_800_000_000) == "BTC"
    assert config.markets_oracle_coin("XAUUSD", 1_800_000_000) == "xyz:GOLD"
    assert config.markets_oracle_coin("AUDUSD", 1_800_000_000) is None   # no oracle: stays on mid


def test_load_window_refuses_a_mismatched_root(tmp_path, monkeypatch):
    W = oracle.WINDOW_MS * 10
    rows = [R(W + 1, "1.0"), R(W + 2, "2.0")]
    good = {"w": W, "n": 2, "root": oracle.window_root(rows)}
    pages = {}

    def fetch(url):
        return pages.get(url)
    monkeypatch.setattr(oracle, "_fetch", fetch)
    import json
    pages[f"B/{W}/oracle.json"] = json.dumps(dict(good, root="22" * 32))
    pages[f"B/{W}/oracle.jsonl"] = "".join(json.dumps(r) + "\n" for r in rows)
    assert oracle.load_window("B", str(tmp_path), W) is None
    pages[f"B/{W}/oracle.json"] = json.dumps(good)
    assert oracle.load_window("B", str(tmp_path), W) == rows
    pages.clear()                                            # cached after verifying
    assert oracle.load_window("B", str(tmp_path), W) == rows


def test_markets_dispatch_oracle_markets_only(monkeypatch, tmp_path):
    from sn89_signals import markets
    monkeypatch.setattr(config, "MARKETS_ORACLE_MAX_AGE_S", 90)
    monkeypatch.setattr(config, "MARKETS_ORACLE_FILL_DELAY_S", 3)
    monkeypatch.setattr(config, "MARKETS_ORACLE_FILL_WAIT_S", 30)
    start = (config.MARKETS_V3_FROM_UNIX // 900 + 400) * 900
    end = start + 900
    btc, aud = f"UD:BTCUSD:15m:{start}", f"UD:AUDUSD:15m:{start}"
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", start + 900)
    assert not markets.uses_oracle(btc)                      # starts before the arm
    monkeypatch.setattr(config, "MARKETS_ORACLE_FROM_UNIX", start)
    assert markets.uses_oracle(btc) and not markets.uses_oracle(aud)
    rows = [R(t * 1000, "100") for t in range(start - 90, start + 1, 30)]
    rows += [R(t * 1000, "99") for t in range(start + 3, end + 1, 3)]
    assert markets.market_target(btc, rows) == 100.0
    assert markets.market_spot(btc, rows, (start + 10) * 1000) == ((start + 9) * 1000, 99.0)
    pricer = markets.make_pricer(btc, rows, 100.0, Decimal("0.0001"))
    q = pricer({"order": (0, (start + 100) * 1_000_000, "", 0)})
    assert q["tick_t"] == (start + 130) * 1000 and q["tick_mark"] == 99.0 and q["p"] < Decimal("0.5")
    db = markets._db(str(tmp_path))
    assert markets.grade_market(db, btc, rows, Decimal("0.0001")) == "DOWN"
    assert db.execute("SELECT target, settle FROM outcomes WHERE market_id=?", (btc,)).fetchone() == (100.0, 99.0)
