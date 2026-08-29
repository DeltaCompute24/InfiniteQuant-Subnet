"""What the points scheme is doing on this network, right now.

Reads the same functions the validator does -- never re-derives a rule -- because
a diagnostic that reimplements the gate reports a different answer from the chain
while claiming to match it. tools/qualify_report.py did exactly that for two
months, comparing a raw hit-rate against the retired QUALIFY_MIN_HIT while the
validator had moved to the Wilson bound.

RUN
  ssh iq-main 'cd /opt/sn89-signals && set -a && . ./.env.test && set +a && \
               .venv/bin/python ops_points_report.py'
"""
from __future__ import annotations

import os
import sqlite3
import time

from sn89_signals import config, hf, scoring

CACHE = os.path.expanduser(os.getenv("SN89_HF_GRADE_CACHE", "~/.sn89/hf-grade"))
DB = os.path.join(CACHE, "hf_grades.db")
now = time.time()

print("netuid %s · %s" % (config.NETUID, config.NETWORK))
print("points armed: %s   gamma %.2f   window %.0fd   cap %d/day"
      % (config.points_enforced_as_of(now), config.HF_POINTS_GAMMA,
         config.HF_POINTS_WINDOW_S / 86400, config.HF_POINTS_DAILY_CAP))
print("grade cache: %s\n" % DB)

if not os.path.exists(DB):
    raise SystemExit("no grade cache at %s" % DB)

db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
tot, banded, decisive = db.execute(
    "SELECT COUNT(*), SUM(tp_bps IS NOT NULL), "
    "SUM(status IN ('won','lost')) FROM grades").fetchone()
print("grades: %d total · %d carry a band · %d decisive"
      % (tot or 0, banded or 0, decisive or 0))
if not banded:
    print("\nNo graded call carries a band yet, so nothing is priceable. Rows written\n"
          "before the band columns existed are NULL and fall back to the board --\n"
          "correct, but they contribute no points. New calls will carry one.")

# Per-hotkey, priced exactly as the validator prices it.
rows = db.execute(
    "SELECT hk, t0_ms, status, tp_bps, horizon_s, pair FROM grades "
    "WHERE status IN ('won','lost') ORDER BY t0_ms").fetchall()
by_hk: dict[str, list] = {}
for hk, t0_ms, status, tp, hz, pair in rows:
    by_hk.setdefault(hk, []).append(
        (t0_ms / 1000.0, status == "won", False, None, tp, hz, pair))

if by_hk:
    print("\n%-14s %6s %8s %10s %10s  %s"
          % ("hotkey", "decis", "priced", "points", "clamped", "note"))
    for hk, dec in sorted(by_hk.items(),
                          key=lambda kv: -len(kv[1])):
        calls = scoring.qualified_calls(dec, first_seen_unix=0.0,
                                        sigma_for=hf._board_sigma_for)
        tally = scoring.decayed_points_tally(calls, now)
        print("%-14s %6d %8d %10.3f %10.3f  %s"
              % (hk[:12] + "..", len(dec), len(calls), tally, max(0.0, tally),
                 "underwater -- earns nothing until it climbs back"
                 if tally < 0 else ""))
    print("\nclamped is what compute_weights splits the pool by. The unclamped\n"
          "figure stays visible on purpose: the referrer score and every\n"
          "reporting surface need to see how far underwater a miner is.")
