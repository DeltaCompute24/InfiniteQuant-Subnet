#!/usr/bin/env python3
"""Export the IQ Signals grading journal to the format illustrate_scoring.py reads.

READ-ONLY. Opens the signals DB in ro mode, introspects the schema (so column
names need not be hard-coded), and emits one JSON line per trader:

    {"name": <handle>, "hotkey": <handle>, "wins": W, "losses": L,
     "outcomes": [[<t0_unix>, <won bool>], ...]}   # decisive (won/lost) only

Usage (on iq-main):
    python3 export_signals_journal.py [out.jsonl] [--db /path/to/iq_admin_dash.db]

Default DB: /opt/iq-platform/data/live/iq_admin_dash.db
Default out: /tmp/sn89_journal_export.jsonl
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

DEFAULT_DB = "/opt/iq-platform/data/live/iq_admin_dash.db"
DEFAULT_OUT = "/tmp/sn89_journal_export.jsonl"

NAME_COLS = ["handle", "x_handle", "tg_handle", "telegram_username", "username",
             "display_name", "name"]
TIME_COLS = ["fired_at", "graded_at", "resolved_at", "t0_unix", "entry_ts",
             "ts_fired", "created_at", "updated_at", "ts"]


def cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def to_unix(v):
    """Best-effort: epoch seconds/ms, or ISO-8601 string → float seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e12 else float(v)
    s = str(v).strip()
    try:
        return float(s) / 1000.0 if float(s) > 1e12 else float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def main():
    args = sys.argv[1:]
    out_path = next((a for a in args if not a.startswith("-")), DEFAULT_OUT)
    db_path = DEFAULT_DB
    if "--db" in args:
        db_path = args[args.index("--db") + 1]

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    sub_cols = cols(con, "signals_submissions")
    usr_cols = cols(con, "signals_users")
    time_col = next((c for c in TIME_COLS if c in sub_cols), None)
    name_col = next((c for c in NAME_COLS if c in usr_cols), None)
    order = time_col or "ss.rowid"
    if not time_col:
        print("WARNING: no timestamp column found in signals_submissions; using "
              "rowid order. The new (lifetime) elimination is order-only so it is "
              "exact; the legacy rolling-30d view will be approximate.", file=sys.stderr)

    name_expr = f"su.{name_col}" if name_col else "su.telegram_user_id"
    time_expr = f"ss.{time_col}" if time_col else "ss.rowid"
    rows = con.execute(
        f"SELECT su.id AS uid, {name_expr} AS name, ss.status AS status, "
        f"{time_expr} AS t "
        f"FROM signals_submissions ss JOIN signals_users su "
        f"ON su.id = ss.signals_user_id "
        f"WHERE ss.status IN ('won','lost') "
        f"ORDER BY su.id, {order}"
    ).fetchall()

    by_user: dict = {}
    for uid, name, status, t in rows:
        nm = str(name) if name not in (None, "") else f"user_{uid}"
        by_user.setdefault((uid, nm), []).append((to_unix(t), status == "won"))

    n_users = n_decisive = 0
    with open(out_path, "w") as fh:
        for (uid, nm), seq in by_user.items():
            seq = [(t if t is not None else float(i), won)
                   for i, (t, won) in enumerate(seq)]
            wins = sum(1 for _, won in seq if won)
            losses = len(seq) - wins
            fh.write(json.dumps({
                "name": nm[:16], "hotkey": str(uid),
                "wins": wins, "losses": losses,
                "outcomes": [[t, won] for t, won in seq],
            }) + "\n")
            n_users += 1
            n_decisive += len(seq)
    con.close()
    print(f"wrote {out_path}: {n_users} traders, {n_decisive} decisive outcomes "
          f"(name_col={name_col}, time_col={time_col})")


if __name__ == "__main__":
    main()
