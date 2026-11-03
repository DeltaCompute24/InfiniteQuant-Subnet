"""Sanity simulation for sn89_signals.agent_books (Agent competition v2). Offline, synthetic.

Random books (a fresh uniform target in [-0.25, 0.25] per pair every 4 h) against edge
books (±0.25 per pair every 4 h, on the sign of the coming 4 h return with probability q)
on synthetic GBM ticks, scored by the real engine for N weeks. Prints mean weekly paid
gain, P(paid > 0) per book-week, the elimination rate, and the edge books' realised
Sharpe (annualised from daily EOD equity).

    python tools/sim_agent_books.py [--weeks 12] [--books 100] [--q 0.53] [--pairs 1] [--seed 7]
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sn89_signals import agent_books as ab  # noqa: E402

MON = 1793577600000          # 2026-11-02 00:00 UTC, a Monday
STEP_MS = 5 * 60_000
REBAL_MS = 4 * 3_600_000
ALL_PAIRS = ("BTCUSD", "ETHUSD", "SOLUSD")
PAIRS = ALL_PAIRS


def gbm_ticks(weeks, vol, half_spread_bps, rng):
    n = weeks * 7 * 288 + 1
    dt = STEP_MS / (365 * 86_400_000)
    s = vol * math.sqrt(dt)
    paths, ticks = {}, []
    for a in PAIRS:
        p, path = 100.0, []
        for i in range(n):
            path.append(p)
            p *= math.exp(-0.5 * s * s + s * rng.gauss(0, 1))
        paths[a] = path
    for i in range(n):
        t = MON + i * STEP_MS
        for a in PAIRS:
            p = paths[a][i]
            h = p * half_spread_bps / 1e4
            ticks.append({"a": a, "t": t, "p": p, "b": p - h, "k": p + h})
    return ticks, paths


def receipts(kind, acct, weeks, paths, q, rng):
    out, seq = [], 0
    steps = REBAL_MS // STEP_MS
    t = MON + 60_000
    while t < MON + weeks * ab.WEEK_MS - REBAL_MS:
        i = (t - MON) // STEP_MS + 1                 # fill tick index (+3 s -> next tick)
        tg = {}
        for a in PAIRS:
            if kind == "random":
                tg[a] = round(rng.uniform(-0.25, 0.25), 4)
            else:
                path = paths[a]
                j = min(i + steps, len(path) - 1)
                up = path[j] > path[i]
                right = rng.random() < q
                tg[a] = 0.25 if up == right else -0.25
        seq += 1
        out.append({"account": acct, "seq": seq, "t_recv_ms": t, "targets": tg})
        t += REBAL_MS
    return out


def sharpe(res):
    eod = [1.0] + [d["eod_equity"] for d in res["daily"]]
    r = [eod[i + 1] / eod[i] - 1 for i in range(len(eod) - 1)]
    if len(r) < 2 or statistics.pstdev(r) == 0:
        return 0.0
    return statistics.mean(r) / statistics.pstdev(r) * math.sqrt(365)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--books", type=int, default=100)
    ap.add_argument("--q", type=float, default=0.53)
    ap.add_argument("--vol", type=float, default=0.6)
    ap.add_argument("--half-spread-bps", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pairs", type=int, default=1, help="1 = a 0.25x book, 3 = 0.75x gross")
    a = ap.parse_args()
    global PAIRS
    PAIRS = ALL_PAIRS[:a.pairs]
    rng = random.Random(a.seed)
    uni = {p: "crypto" for p in PAIRS}
    end = MON + a.weeks * ab.WEEK_MS
    out = {}
    for kind in ("random", "edge"):
        paid, pos, elim, sh = [], 0, 0, []
        for b in range(a.books):
            ticks, paths = gbm_ticks(a.weeks, a.vol, a.half_spread_bps, rng)
            rs = receipts(kind, f"{kind}{b}", a.weeks, paths, a.q, rng)
            res = ab.replay_book(f"{kind}{b}", rs, ticks, uni, start_ms=MON, end_ms=end,
                                 ticks_sorted=True)
            for w in res["weeks"]:
                paid.append(w["paid"])
                pos += w["paid"] > 0
            elim += res["eliminated_at_ms"] is not None
            sh.append(sharpe(res))
        out[kind] = statistics.mean(paid)
        print(f"{kind:6s} books={a.books} weeks={a.weeks}  mean paid/week={statistics.mean(paid):.5f}"
              f"  P(paid>0)={pos / len(paid):.3f}  eliminated={elim / a.books:.2%}"
              f"  median Sharpe={statistics.median(sh):+.2f}")
    if out["edge"] > 0:
        print(f"random / edge mean paid = {out['random'] / out['edge']:.2%}")


if __name__ == "__main__":
    main()
