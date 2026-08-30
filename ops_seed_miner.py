"""Seed miner for testnet 496 -- give the subnet a live field.

WHY THIS EXISTS
  496 logs burn=1.000 every cycle: nothing earns. The scoring works fine -- two
  miners ARE qualified (5HDiei6siTJ1 at Wilson LB 0.595, 5EbZQenSdykf at 0.516)
  -- but they stopped trading on 2026-08-07 and EMISSION_DECAY_S is 86400 on
  testnet, so their tallies decayed to zero within a day. The subnet is idle, not
  broken. A beta trader submitting into it today sees their weight at zero next
  to a field where everything burns, which reads as the mechanism not working.

WHAT IT IS NOT
  It does not manufacture edge. Direction is a coin flip, so it earns exactly
  what a no-view miner earns -- which under the new symmetric scoring is zero in
  expectation. It is here to produce ACTIVITY, not a winner. Whether it qualifies
  is the gate's business: at QUALIFY_MIN_DECISIVE=2 on testnet a coin flip lands
  two straight wins about a quarter of the time, holds a tally while those wins
  are inside the 1-day decay window, and loses it again. That intermittency is
  the honest behaviour and is worth watching.

HOW
  Writes into sn89_limit_orders, the same table the dashboard writes and the same
  path sn89-limit-watcher already signs and fires. No new signing code, no second
  submission route to drift from the first.

RUN
  ssh iq-main 'cd /opt/sn89-signals && nohup .venv/bin/python ops_seed_miner.py \
               >> /var/log/sn89-seed-miner.log 2>&1 &'
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import hf  # noqa: E402

ADMIN_DB = os.getenv("SN89_ADMIN_DB", "/opt/iq-platform/data/live/iq_admin_dash.db")
SEED_HOTKEY = os.getenv("SN89_SEED_HOTKEY", "5HbkgCR1nx7T9vga9ZCCTTadnefMxWhA3vYFSaic7uA8aGQ2")
NETWORK = os.getenv("SN89_CLOSERS_ORDER_NETWORK", "test")

# HF limits (hf_rules_as_of): 30/day, and ONE OPEN POSITION PER PAIR -- a second
# call on a pair while the first is live is voided as overlapping_open. Cycling
# pairs is what keeps every call countable instead of half of them wasted.
GAP_S = int(os.getenv("SN89_SEED_GAP_S", "420"))     # 7 min between calls
DAILY_CAP = int(os.getenv("SN89_SEED_DAILY_CAP", "24"))


def board_pairs() -> list[str]:
    b = hf.hf_bands_as_of(time.time()) or {}
    return sorted(b)


def submitted_today(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM sn89_limit_orders WHERE hotkey=? AND network=? "
        "AND created_at >= strftime('%Y-%m-%dT00:00:00Z','now')",
        (SEED_HOTKEY, NETWORK)).fetchone()
    return row[0] if row else 0


def main() -> int:
    pairs = board_pairs()
    if not pairs:
        print("no HF board at this time -- nothing to submit against")
        return 1
    print("seed miner · hotkey %s.. · %d pairs · gap %ds · cap %d/day"
          % (SEED_HOTKEY[:12], len(pairs), GAP_S, DAILY_CAP), flush=True)

    i = 0
    while True:
        con = sqlite3.connect(ADMIN_DB, timeout=30)
        try:
            n = submitted_today(con)
            if n >= DAILY_CAP:
                print("daily cap %d reached; idling" % DAILY_CAP, flush=True)
            else:
                # Rotate pairs so the one-open-position-per-pair gate never
                # voids a call for overlapping its own predecessor.
                pair = pairs[i % len(pairs)]
                direction = random.choice(["LONG", "SHORT"])
                payload = {"kind": "hf", "trade_pair": pair, "direction": direction}
                con.execute(
                    "INSERT INTO sn89_limit_orders (hotkey, kind, payload, network) "
                    "VALUES (?,?,?,?)",
                    (SEED_HOTKEY, "hf", json.dumps(payload), NETWORK))
                con.commit()
                print("[%s] queued %s %s (%d/%d today)"
                      % (time.strftime("%H:%M:%S"), direction, pair, n + 1, DAILY_CAP),
                      flush=True)
                i += 1
        finally:
            con.close()
        time.sleep(GAP_S)


if __name__ == "__main__":
    raise SystemExit(main())
