"""Mainnet cutover for on-chain Markets: 2026-10-09 00:00:00 UTC (Whit, 2026-10-08).

Markets takes ALL of Closers' mecid-0 share (0.125) at T and Closers retires. Everything before T
must resolve exactly as it did; the arm lives in source so every validator replaying master agrees;
bets on the first markets are taken before T (one window ahead) and must be accepted.
"""
import json
import os
import subprocess
import sys

import pytest

from sn89_signals import competitions, config, hf, markets

T = 1791504000
ENTITY = "5DPGU1Lw8zVHs6mCC6Q1MHr4cXEwwBf8mBXc2xNAxLn6EbV1"
OLD = {"lf": 0.4375, "hf": 0.4375, "closers": 0.125}
NEW = {"lf": 0.4375, "hf": 0.4375, "markets": 0.125}

# The committed history as it stood before this change, so a pre-T replay is provably unchanged.
PRE_T_HISTORY = (
    (0, "lf:0.5,hf:0.5,closers:0.0"),
    (1785790800, "lf:0.375,hf:0.375,closers:0.25"),
    (1785791400, "lf:0.30,hf:0.30,closers:0.20,reserve:0.20"),
    (1785816600, "lf:0.35,hf:0.35,closers:0.10,reserve:0.20"),
    (1785866700, "lf:0.4375,hf:0.4375,closers:0.125"),
    (1785967200, "lf:0.35,hf:0.35,closers:0.10,reserve:0.20"),
    (1786327200, "lf:0.4375,hf:0.4375,closers:0.125"),
)


def _source_defaults() -> bool:
    return not os.getenv("SN89_COMP_WEIGHTS") and not os.getenv("SN89_MARKETS_FROM")


pytestmark = pytest.mark.skipif(not _source_defaults(),
                                reason="env overrides active (testnet shell); these test the source")


def test_history_before_t_is_untouched():
    assert config.COMP_WEIGHTS_HISTORY[:len(PRE_T_HISTORY)] == PRE_T_HISTORY
    assert config.COMP_WEIGHTS_HISTORY[len(PRE_T_HISTORY):] == ((T, "lf:0.4375,hf:0.4375,markets:0.125"),)


@pytest.mark.parametrize("t", [1786327200, 1791000000, T - 1])
def test_shares_before_t_are_todays(t):
    assert config.comp_weights_as_of(t) == pytest.approx(OLD)


@pytest.mark.parametrize("t", [T, T + 1, T + 86400 * 30])
def test_markets_takes_closers_share_from_t(t):
    s = config.comp_weights_as_of(t)
    assert s == pytest.approx(NEW) and "closers" not in s


def test_closers_vector_is_ignored_after_t():
    """Closers still computes harmlessly, but with no share it moves nothing and burns nothing."""
    vec = {"lf": {1: 1.0}, "hf": {2: 1.0}, "closers": {3: 1.0}, "markets": {4: 1.0}}
    before = competitions.combine(vec, config.comp_weights_as_of(T - 1))
    after = competitions.combine(vec, config.comp_weights_as_of(T))
    assert before.get(3) == pytest.approx(0.125) and before.get(4) is None
    assert after.get(3) is None and after.get(4) == pytest.approx(0.125)


def test_arm_is_in_source():
    assert config.MARKETS_FROM_UNIX == T
    assert not config.markets_active_as_of(T - 1) and config.markets_active_as_of(T)
    # bets on the first markets come in up to one hour (the 1h open lead) before T
    assert config.markets_accepting_as_of(T - 3600) and not config.markets_accepting_as_of(T - 3601)


def _payload(mid, asset="BTCUSD"):
    return {"kind": "mk.bet", "market_id": mid, "side": "UP", "dollars": "10.00",
            "account": f"{ENTITY}_1", "trade_pair": asset}


def test_first_mainnet_15m_market_takes_bets_before_t():
    mid = markets.market_id("BTCUSD", "15m", T)
    assert markets.market_exists(mid)
    for t in (T - 900, T - 300, T - 61):
        markets.validate_bet(_payload(mid), ENTITY, t)
    with pytest.raises(hf.HFRejected, match="market_closed"):
        markets.validate_bet(_payload(mid), ENTITY, T - 60)
    with pytest.raises(hf.HFRejected, match="market_not_open"):
        markets.validate_bet(_payload(mid), ENTITY, T - 901)


def test_first_mainnet_1h_market_takes_bets_from_t_minus_3600():
    mid = markets.market_id("BTCUSD", "1h", T)
    markets.validate_bet(_payload(mid), ENTITY, T - 3600)
    with pytest.raises(hf.HFRejected, match="markets_not_live"):
        markets.validate_bet(_payload(mid), ENTITY, T - 3601)


def test_no_market_starts_before_t():
    early = markets.market_id("BTCUSD", "15m", T - 900)
    assert not markets.market_exists(early)
    with pytest.raises(hf.HFRejected, match="no_such_market"):
        markets.validate_bet(_payload(early), ENTITY, T - 1200)


def _entry(t_us, seq, payload):
    return {"submit": {"hk": ENTITY, "seq": seq, "payload": payload},
            "receipt": {"t_recv_us": t_us, "grid_t0_ms": t_us // 1000}}


def test_replay_across_t_fresh_matches_incremental(tmp_path):
    """Bets received before T on the first market grade the same whether the cache is rebuilt
    from scratch or fed in a different arrival order."""
    mid = markets.market_id("BTCUSD", "15m", T)
    entries = [_entry((T - 600 + i * 30) * 1_000_000, i + 1,
                      dict(_payload(mid), side="UP" if i % 2 else "DOWN",
                           account=f"{ENTITY}_{i % 3}", dollars=f"{5 + i}.00")) for i in range(8)]
    def build(cache, chunks):
        db = markets._db(str(cache))
        stored = 0
        for es in chunks:
            stored += markets.ingest_entries(db, (T - 900) * 1000, es)
        db.commit()
        rows = sorted(db.execute("SELECT key, account, side, dollars FROM bets").fetchall())
        db.close()
        return stored, rows

    a = build(tmp_path / "a", [entries])
    b = build(tmp_path / "b", [entries[4:], entries[:4]])
    assert a == b and a[0] == 8


def test_testnet_env_override_keeps_testnet_shares():
    """A fresh interpreter with .env.test's values resolves testnet's shares and arm, unchanged."""
    env = dict(os.environ, SN89_COMP_WEIGHTS="lf:0.375,hf:0.375,closers:0.15,markets:0.10",
               SN89_MARKETS_FROM="1791480740")
    code = ("import json; from sn89_signals import config as c; "
            "print(json.dumps([c.MARKETS_FROM_UNIX, c.comp_weights_as_of(1791504000 + 100)]))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__)), check=True).stdout
    arm, shares = json.loads(out.strip().splitlines()[-1])
    assert arm == 1791480740
    assert shares == pytest.approx({"lf": 0.375, "hf": 0.375, "closers": 0.15, "markets": 0.10})
