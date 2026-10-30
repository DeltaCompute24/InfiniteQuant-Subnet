"""Agent competition scoreboard (config.AGENT_*): one row per hosted IQ Agent account.

Reads the validator journal read-only and writes SN89_AGENT_SCOREBOARD_PATH, served by the
partner API at /api/signals/agent-scoreboard. `weight_share_pct` is the account's share of
the Agent vector, computed by the same replay the validator commits (each account on its own
pseudo-uid, as replay.entity_split does). The pool is paid only once the agent share starts.

    SN89_DB_PATH=/root/.sn89/validator-main.db python tools/build_agent_scoreboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sn89_signals import config, entity, replay, scoring  # noqa: E402

OUT = os.getenv("SN89_AGENT_SCOREBOARD_PATH",
                "/opt/iq-platform/data/live/sn89-agent-scoreboard.json")
# The intake account map; accounts opened by an internal `test:` login stay off the board.
OWNERS = os.getenv("SN89_AGENT_ACCOUNTS_PATH", "/opt/iq-platform/data/live/sn89-agent/accounts.json")


def _test_numbers() -> set:
    try:
        with open(OWNERS, encoding="utf-8") as fh:
            return {int(v["n"]) for k, v in json.load(fh).items() if k.startswith("test:")}
    except (OSError, ValueError, KeyError):
        return set()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def build(db_path: str, now: float) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    sig_rows = [
        {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st, "is_copy": int(cp or 0),
         "plaintext": pt, "exit_at_ms": ex}
        for ch, hk, t0, st, cp, pt, ex in con.execute(
            "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext, exit_at_ms FROM signals")]
    meta = {hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
            for hk, fs, sk in con.execute("SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")}
    con.close()

    hidden = _test_numbers()
    accounts = sorted(hk for hk in meta
                      if entity.is_agent_account(hk) and entity.parse(hk)[1] not in hidden)
    pseudo = {a: -(i + 1) for i, a in enumerate(accounts)}
    with scoring.qualify_gate(config.AGENT_QUALIFY_LB_FLOOR, config.AGENT_QUALIFY_MIN_DECISIVE):
        vec = replay.weights_from_journal(sig_rows, meta, dict(pseudo), now, scope="agent")

    rows, weights = [], {}
    for a in accounts:
        mine = [s for s in sig_rows if s["hotkey"] == a and s["t0_unix"] >= config.AGENT_COMP_FROM_UNIX]
        c = {k: sum(1 for s in mine if s["status"] == k)
             for k in ("won", "lost", "washed", "void")}
        pending = sum(1 for s in mine if s["status"] not in ("won", "lost", "washed", "void"))
        dec = c["won"] + c["lost"]
        lb = scoring.confident_edge(c["won"], dec) if dec else None
        qualified = bool(dec >= config.AGENT_QUALIFY_MIN_DECISIVE
                         and lb is not None and lb >= config.AGENT_QUALIFY_LB_FLOOR)
        # Before an account qualifies, its weight is only the new-miner dust floor, not a share.
        weights[a] = vec.get(pseudo[a], 0.0) if qualified else 0.0
        rows.append({
            "label": f"Agent {entity.parse(a)[1] - config.AGENT_ACCOUNT_MIN}",
            "account": a, "calls": len(mine), "won": c["won"], "lost": c["lost"],
            "washed": c["washed"], "pending": pending, "void": c["void"], "decisive": dec,
            "hit_rate_pct": round(100 * c["won"] / dec, 1) if dec else None,
            "lb_pct": round(100 * lb, 1) if lb is not None else None,
            "qualified": qualified, "weight_share_pct": None,
            "last_call_at": _iso(max((s["t0_unix"] for s in mine), default=None)),
        })
    total = sum(weights.values())
    for r in rows:
        if total > 0 and weights[r["account"]] > 0:
            r["weight_share_pct"] = round(100 * weights[r["account"]] / total, 2)
    rows.sort(key=lambda r: (-(r["weight_share_pct"] or 0), -(r["lb_pct"] or 0), -r["calls"]))
    paid = config.comp_weights_as_of(now).get("agent", 0) > 0
    return {
        "snapshot_at": _iso(now), "opened_at": _iso(config.AGENT_COMP_FROM_UNIX),
        "paid_from": _iso(config.HF_END_FROM_UNIX), "paid": paid,
        "gate": {"lb_floor_pct": round(100 * config.AGENT_QUALIFY_LB_FLOOR),
                 "min_decisive": config.AGENT_QUALIFY_MIN_DECISIVE},
        "daily_cap": config.submission_rules_as_of(now)[0],
        "min_gap_h": config.submission_rules_as_of(now)[1] / 3600,
        "n_accounts": len(rows), "n_qualified": sum(1 for r in rows if r["qualified"]),
        "leaderboard": rows,
    }


if __name__ == "__main__":
    board = build(config.DB_PATH, time.time())
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(board, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
    print(f"agent scoreboard: {board['n_accounts']} account(s), {board['n_qualified']} qualified -> {OUT}")
