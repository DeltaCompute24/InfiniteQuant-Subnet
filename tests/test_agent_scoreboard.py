"""tools/build_agent_scoreboard.py: one row per agent account, test logins hidden."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sn89_signals import config, entity, hf
from sn89_signals.schema import Signal

NOW = 1_800_000_000.0
ENT = "5" + "E" * 47
BOARD = {"BTCUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150}}
spec = importlib.util.spec_from_file_location(
    "build_agent_scoreboard", Path(__file__).parents[1] / "tools" / "build_agent_scoreboard.py")
bas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bas)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 1)
    monkeypatch.setattr(config, "SIGNALS_ENTITY_HOTKEYS", frozenset({ENT}))
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    monkeypatch.setattr(config, "allowed_assets_as_of", lambda t: BOARD)
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)
    p = tmp_path / "v.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE signals (commit_hex TEXT, hotkey TEXT, t0_unix REAL, status TEXT, "
                "is_copy INTEGER, plaintext TEXT, exit_at_ms INTEGER)")
    con.execute("CREATE TABLE hotkey_meta (hotkey TEXT, first_seen_unix REAL, strikes INTEGER)")
    good, test, sig = (f"{ENT}_{config.AGENT_ACCOUNT_MIN + i}" for i in (1, 2, 0))
    sig = f"{ENT}_5"
    for hk, wins, losses in ((good, 45, 10), (test, 45, 10), (sig, 45, 10)):
        con.execute("INSERT INTO hotkey_meta VALUES (?,?,0)", (hk, NOW - 40 * 86400))
        pt = Signal(trade_pair="BTCUSD", direction="LONG", tp_bps=150, sl_bps=150, ts_miner=0,
                    hotkey=hk, asset_class="crypto").canonical_bytes().decode()
        for i in range(wins + losses):
            con.execute("INSERT INTO signals VALUES (?,?,?,?,0,?,NULL)",
                        (f"{hk}-{i}", hk, NOW - 5 * 86400 + i * 3600,
                         "won" if i < wins else "lost", pt))
    con.commit()
    con.close()
    owners = tmp_path / "accounts.json"
    owners.write_text(json.dumps({"web:1": {"n": config.AGENT_ACCOUNT_MIN + 1},
                                  "test:x": {"n": config.AGENT_ACCOUNT_MIN + 2}}))
    monkeypatch.setattr(bas, "OWNERS", str(owners))
    monkeypatch.setattr(bas, "AGENT_DIR", str(tmp_path))
    return str(p)


def test_board_lists_agent_accounts_only_and_hides_test_logins(db):
    b = bas.build(db, NOW)
    assert [r["label"] for r in b["leaderboard"]] == [str(config.AGENT_ACCOUNT_MIN + 1)]
    r = b["leaderboard"][0]
    assert (r["won"], r["lost"], r["decisive"], r["qualified"]) == (45, 10, 55, True)
    assert r["weight_share_pct"] == 100.0
    assert b["gate"] == {"lb_floor_pct": 55, "min_decisive": 40}
    assert b["paid"] is False   # NOW (2027) is after v1 ends: its share moves to `agentbook` at 2026-11-09 08:00
    assert bas.build(db, config.HF_END_FROM_UNIX - 1)["paid"] is False


def test_verdicts_cover_agent_accounts_only(db):
    v = bas.verdicts(db)
    assert v and all(isinstance(x["status"], str) for x in v.values())
    con = sqlite3.connect(db)
    agent = {ch for ch, hk in con.execute("SELECT commit_hex, hotkey FROM signals")
             if entity.is_agent_account(hk)}
    con.close()
    assert set(v) == agent and len(v) == 110          # two agent accounts x 55; _5 excluded


def test_algos_split_by_intake_record_and_publish_only_nicknames(db, tmp_path):
    n = config.AGENT_ACCOUNT_MIN + 1
    hk = f"{ENT}_{n}"
    (tmp_path / "calls").mkdir()
    # Calls 0-29 from algo "secret-folder-a" (nicknamed), 30-54 from "b" (no nickname).
    with open(tmp_path / "calls" / f"{n}.jsonl", "w") as fh:
        for i in range(55):
            fh.write(json.dumps({"id": 9_000_000_000 + i,
                                 "algo_id": "secret-folder-a" if i < 30 else "b"}) + "\n")
    with open(tmp_path / "commits.jsonl", "w") as fh:
        for i in range(55):
            fh.write(json.dumps({"call_id": 9_000_000_000 + i, "commitment": f"{hk}-{i}",
                                 "ok": True}) + "\n")
    (tmp_path / "algo_names.json").write_text(json.dumps({"web:1": {"secret-folder-a": "Night Owl"}}))
    row = bas.build(db, NOW)["leaderboard"][0]
    assert [(a["name"], a["calls"], a["won"], a["lost"]) for a in row["algos"]] == \
        [("Night Owl", 30, 30, 0), (None, 25, 15, 10)]
    assert "secret-folder" not in json.dumps(row)
