#!/usr/bin/env python3
"""Runtime of one weekly freeze at realistic size: N synthetic books over one REAL week of
sealed HF tick shards. Prints the tick pass, the replay, and the frozen file's size.

    python tools/bench_agent_books.py [--books 300] [--week 2026-10-26] [--every-min 120]
        [--pairs 4] [--tick-src /var/lib/sn89-hf/ticks]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sn89_signals import agent_books as ab  # noqa: E402
from sn89_signals import agent_books_io as abio  # noqa: E402
from sn89_signals import config  # noqa: E402

ENT = sorted(config.SIGNALS_ENTITY_HOTKEYS)[0]
LIQUID = ("BTCUSD", "ETHUSD", "SOLUSD", "HYPEUSD", "XRPUSD", "DOGEUSD", "ZECUSD", "TAOUSD",
          "BNBUSD", "AVAXUSD", "NVDAUSD", "TSLAUSD", "METAUSD", "XAUUSD", "XAGUSD",
          "SP500USD", "WTIUSD", "AAVEUSD", "LTCUSD", "SUIUSD")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", type=int, default=300)
    ap.add_argument("--week", default="2026-10-26")
    ap.add_argument("--every-min", type=int, default=120, help="mean minutes between receipts")
    ap.add_argument("--pairs", type=int, default=4, help="pairs per book")
    ap.add_argument("--tick-src", default="/var/lib/sn89-hf/ticks")
    a = ap.parse_args()
    import datetime as dt
    ws = int(dt.datetime.strptime(a.week, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc).timestamp() * 1000)
    we = ws + ab.WEEK_MS
    uni = abio.book_universe_through(we / 1000)
    pool = [p for p in LIQUID if p in uni]
    rng = random.Random(1)
    receipts = []
    for n in range(a.books):
        acct = f"{ENT}_{config.AGENT_ACCOUNT_MIN + n}"
        pairs = rng.sample(pool, min(a.pairs, len(pool)))
        t, seq = ws + rng.randrange(0, 3_600_000), 0
        while t < we:
            seq += 1
            tg = {p: round(rng.uniform(-0.25, 0.25), 3) for p in pairs}
            receipts.append({"v": 1, "entity": ENT, "account": acct, "seq": seq,
                             "t_recv_ms": t, "targets": tg, "sig": "00"})
            t += int(rng.expovariate(1 / (a.every_min * 60_000))) + 1000
    print(f"{a.books} books, {len(receipts)} receipts ({len(receipts) / a.books / 7:.1f}/book/day), "
          f"{len(pool)} pairs in the pool")
    t0 = time.time()
    ticks, missing = abio.load_thin_range(
        ws, we, receipts, uni, lambda w: abio.window_lines_local(a.tick_src, w))
    t1 = time.time()
    res = abio.replay_all(receipts, abio.PairIndex(ticks), uni, start_ms=ws, end_ms=we)
    t2 = time.time()
    doc = abio.build_week(res, ws, {ENT: "x"}, {ENT: 1e5}, receipts)
    data = abio.dumps_week(doc)
    t3 = time.time()
    fills = sum(len(r["fills"]) for r in res.values())
    print(f"tick pass {t1 - t0:.1f}s ({len(ticks)} thin ticks kept, {len(missing)} missing windows)")
    print(f"replay {t2 - t1:.1f}s ({fills} fills)  build+dump {t3 - t2:.2f}s  "
          f"file {len(data) / 1024:.0f} KiB  total {t3 - t0:.1f}s")
    import resource
    print(f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
