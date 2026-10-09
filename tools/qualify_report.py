"""How close is each testnet miner to the emission qualify gate?

Calls scoring._qualifies() -- the SAME dispatch the validator uses -- rather than
re-deriving the rule. It previously compared a raw hit-rate against
config.QUALIFY_MIN_HIT, which is the LEGACY gate: with CONFIDENCE_SCORING=1 the
validator qualifies on the Wilson lower bound instead, so this tool reported a
different answer from the chain while its docstring claimed they matched.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, scoring  # noqa: E402

db = sqlite3.connect(os.getenv("SN89_DB_PATH", "/root/.sn89/validator.db"))
now = time.time()

_gate = ("Wilson LB(z=%.4f) >= %.2f" % (config.QUALIFY_Z, config.QUALIFY_LB_FLOOR)
         if config.CONFIDENCE_SCORING else "raw hit >= %.0f%%" % (config.QUALIFY_MIN_HIT * 100))
print(f"gate: rep_decisive >= {config.QUALIFY_MIN_DECISIVE}  AND  {_gate}")
print(f"rep window: trailing {config.HIT_RATE_WINDOW_S/86400:.0f}d, "
      f"capped at most-recent {config.HIT_RATE_WINDOW_TRADES} decisive; "
      f"immunity {config.IMMUNITY_S/3600:.0f}h; "
      f"emission window {config.SCORE_WINDOW_S/86400:.0f}d")

print("\n=== overall signal status counts ===")
for st, n in db.execute("SELECT status, COUNT(*) FROM signals GROUP BY status ORDER BY 2 DESC"):
    print(f"  {st:10s} {n}")

print("\n=== per-hotkey qualification ===")
rows = db.execute(
    "SELECT hotkey, first_seen_unix, COALESCE(strikes,0), eliminated_t0 FROM hotkey_meta"
).fetchall()
report = []
for hk, first_seen, strikes, elim in rows:
    decisive = [(t0, bool(won), bool(cp)) for t0, won, cp in db.execute(
        "SELECT t0_unix, status='won', COALESCE(is_copy,0) FROM signals "
        "WHERE hotkey=? AND status IN ('won','lost')", (hk,)).fetchall()]
    rep_w, rep_d, tw_all, tw_orig, copies, td = scoring.score_inputs(decisive, first_seen, now)
    hit = (rep_w / rep_d) if rep_d else 0.0
    immune = (now - first_seen) < config.IMMUNITY_S
    # The validator's own dispatch. Do not re-derive it here.
    qualified = (elim is None and strikes < config.STRIKE_LIMIT
                 and scoring._qualifies(rep_w, rep_d))
    lb = scoring.confident_edge(rep_w, rep_d)
    # also count sealed/pending in flight (not yet decisive)
    inflight = db.execute(
        "SELECT COUNT(*) FROM signals WHERE hotkey=? AND status IN ('sealed','revealed','pending')",
        (hk,)).fetchone()[0]
    report.append((hk, rep_d, hit, tw_all, immune, strikes, elim, qualified,
                   inflight, len(decisive), lb))

# sort: qualified first, then by rep_decisive desc
report.sort(key=lambda r: (-r[7], -r[1], -r[2]))
print(f"{'hotkey':12s} {'decis':>5s} {'hit':>5s} {'wilsonLB':>8s} {'twin':>4s} "
      f"{'inflt':>5s} {'tot_dec':>7s} {'state':>9s}  gap-to-gate")
for hk, rep_d, hit, tw, immune, strikes, elim, qual, inflight, totdec, lb in report:
    if elim is not None:
        state = "ELIM"
    elif strikes >= config.STRIKE_LIMIT:
        state = "STRUCK"
    elif qual:
        state = "QUALIFIED"
    elif immune:
        state = "immune"
    else:
        state = "no"
    need_d = max(0, config.QUALIFY_MIN_DECISIVE - rep_d)
    if need_d:
        gap = f"need {need_d} more decisive"
    elif qual:
        gap = "MEETS gate"
    elif config.CONFIDENCE_SCORING:
        gap = f"LB {lb:.3f} < {config.QUALIFY_LB_FLOOR:.2f}"
    else:
        gap = f"hit {hit:.0%} < {config.QUALIFY_MIN_HIT:.0%}"
    print(f"{hk[:12]:12s} {rep_d:5d} {hit:5.0%} {lb:8.3f} {tw:4d} {inflight:5d} "
          f"{totdec:7d} {state:>9s}  {gap}")
