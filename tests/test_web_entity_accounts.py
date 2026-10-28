"""Web workspace orders and the HF board for signals entity accounts.

An account (<entity_hk>_<n>) holds no key. A workspace HF order is signed by the
entity with `account` in the payload; a workspace LF order becomes a submissions
row that the multiplexer's batcher carries (the entity has one commitment slot).
The HF board aggregates an account's calls under the account, like the grader.
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import types

import pytest

from sn89_signals import config, entity, hf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1_800_000_000.0
ENT = "5" + "E" * 47
ACCT = f"{ENT}_7"


def _load(rel, name):
    try:
        import bittensor_wallet  # noqa: F401
    except ImportError:          # the watcher only needs the name at import
        sys.modules.setdefault("bittensor_wallet", types.SimpleNamespace(Keypair=object))
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 1)
    monkeypatch.setattr(config, "SIGNALS_ENTITY_HOTKEYS", frozenset({ENT}))


@pytest.fixture
def lw(monkeypatch, armed):
    mod = _load("scripts/limit_watcher.py", "limit_watcher_under_test")
    monkeypatch.setattr(mod, "NETWORK_SCOPE", "test")     # no registration cache
    return mod


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE signals_users (id INTEGER PRIMARY KEY, sn89_hotkey TEXT);
        CREATE TABLE signals_submissions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, signals_user_id INTEGER, asset TEXT,
          asset_class TEXT, direction TEXT, entry_method TEXT, tp_bps INTEGER,
          sl_bps INTEGER, horizon_hours INTEGER, status TEXT, venue_mode TEXT,
          mechanism INTEGER, raw_input TEXT, submitted_at TEXT);
    """)
    c.execute("INSERT INTO signals_users (id, sn89_hotkey) VALUES (42, ?)", (ACCT,))
    return c


def _row(kind, payload, hotkey=ACCT):
    return {"id": 1, "hotkey": hotkey, "kind": kind, "payload": json.dumps(payload)}


def test_account_lf_order_is_queued_for_the_batcher(lw, con, monkeypatch):
    sig = types.SimpleNamespace(trade_pair="BTCUSD", asset_class="crypto",
                                direction="LONG", tp_bps=40, sl_bps=40, horizon_h=4)
    built = []
    miner = types.ModuleType("neurons.miner")
    miner.build_signal = lambda hk, pair, d: built.append((hk, pair, d)) or sig
    monkeypatch.setitem(sys.modules, "neurons.miner", miner)
    res = lw.fire(_row("lf", {"trade_pair": "BTCUSD", "direction": "LONG"}), {}, {}, con)
    assert res["kind"] == "lf.queued"
    assert built == [(ACCT, "BTCUSD", "LONG")]     # built for the account, not the entity
    r = con.execute("SELECT signals_user_id, raw_input FROM signals_submissions "
                    "WHERE id=?", (res["submission_id"],)).fetchone()
    # Not 'workspace-lf': the feed hides that tag, and this row must reach the multiplexer.
    assert (r["signals_user_id"], r["raw_input"]) == (42, "workspace-lf-entity")


def test_account_lf_order_honours_the_quota(lw, con, monkeypatch):
    monkeypatch.setattr(lw, "quota_block", lambda c, uid: "lf_daily_cap:3/3")
    res = lw.fire(_row("lf", {"trade_pair": "BTCUSD", "direction": "LONG"}), {}, {}, con)
    assert res == {"kind": "error", "reason": "lf_daily_cap:3/3"}
    assert con.execute("SELECT COUNT(*) FROM signals_submissions").fetchone()[0] == 0


def test_account_hf_order_is_signed_by_the_entity_with_the_account(lw, con, monkeypatch):
    kp = object()
    monkeypatch.setattr(lw, "entity_keypair", lambda owner: kp if owner == ENT else None)
    monkeypatch.setattr(lw, "_stamp_hf", lambda p: {"trade_pair": p["trade_pair"],
                                                    "direction": p["direction"]})
    sent = []
    replies = [{"kind": "hf.reject", "reason": "stale_seq:99"},
               {"kind": "hf.receipt", "seq": 100}]

    async def fake_ws(k, payload):
        sent.append((k, dict(payload)))
        return replies.pop(0)
    monkeypatch.setattr(lw, "fire_ws", fake_ws)
    res = lw.fire(_row("hf", {"trade_pair": "ETHUSD", "direction": "SHORT"}), {}, {}, con)
    assert res == {"kind": "hf.receipt", "seq": 100}
    # one stale_seq from the shared entity counter is retried once
    assert len(sent) == 2
    assert all(k is kp and p["account"] == ACCT for k, p in sent)


def test_account_closers_vote_is_refused(lw, con):
    res = lw.fire(_row("closers", {"kind": "closers"}), {}, {}, con)
    assert res == {"kind": "error", "reason": "closers_not_supported_for_accounts"}


def test_account_order_before_the_arm_is_refused(lw, con, monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 0)
    res = lw.fire(_row("hf", {"trade_pair": "ETHUSD", "direction": "SHORT"}), {}, {}, con)
    assert res == {"kind": "error", "reason": "entity_not_armed"}


def test_plain_hotkey_orders_are_untouched(lw, con, monkeypatch):
    called = []
    monkeypatch.setattr(lw, "fire_lf", lambda *a: called.append(a) or {"kind": "lf.commit"})
    res = lw.fire(_row("lf", {"trade_pair": "BTCUSD", "direction": "LONG"}, hotkey=ENT),
                  {}, {}, con)
    assert res == {"kind": "lf.commit"} and called


def test_hf_board_keys_an_account_call_by_the_account(armed, monkeypatch, tmp_path):
    mod = _load("tools/build_hf_scoreboard.py", "hf_board_under_test")
    live, logs = tmp_path / "live", tmp_path / "logs"
    live.mkdir()
    logs.mkdir()
    t0 = int(NOW * 1000)
    with open(logs / "123.jsonl", "w") as fh:
        for seq, p in ((1, {"trade_pair": "BTCUSD", "direction": "LONG", "account": ACCT}),
                       (2, {"trade_pair": "BTCUSD", "direction": "LONG"})):
            fh.write(json.dumps({"submit": {"hk": ENT, "seq": seq, "payload": p},
                                 "receipt": {"grid_t0_ms": t0}}) + "\n")
    monkeypatch.setattr(mod, "LIVE_DIR", str(live))
    monkeypatch.setattr(mod, "LOG_DIR", str(logs))
    calls = mod._accepted_calls()
    # the dict key stays signer:seq (what grades keys on); the board row is the account
    assert calls[(ENT, 1)]["score_hk"] == ACCT
    assert calls[(ENT, 2)]["score_hk"] == ENT
