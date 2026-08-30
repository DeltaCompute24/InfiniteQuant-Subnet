"""R17 — would a validator rebuilding its grade cache reach OUR verdicts?

Rebuilds a grade cache FROM SCRATCH in a throwaway directory, from the same
public receipts any third party can read, then diffs it key-by-key against the
cache the live validator has accumulated.

This is the check that would have caught the index cap of § 9a: index.json served
a hardcoded wins[:2000] -- a COUNT governing gates measured in DAYS -- which had
decayed to 6 days against an 8-day eligibility gate. Any validator rebuilding its
cache produced an EMPTY HF vector and burned the whole mechanism share. It was
invisible because chef's cache accumulated incrementally and we hold ~73% of
validator stake, so nobody else's rebuild was ever compared to ours.

Incremental agreement proves nothing about that. Only a cold rebuild does.

VOID IS REPORTED SEPARATELY FROM OUTCOME, and they are different failures:
  * an outcome disagreement (won vs lost vs wash) means the two caches priced the
    same call differently -- a grading defect,
  * a void disagreement means they disagreed on whether the call was VALID at all
    -- an admission defect, which silently changes who is in the field.
Collapsing them into one percentage hides the second inside the first.

  ssh iq-main 'cd /opt/sn89-signals && set -a && . ./.env.test && set +a && \
               .venv/bin/python ops_grade_parity.py'
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time

from sn89_signals import config, hf, hf_grade

LIVE = os.path.expanduser(os.getenv("SN89_HF_GRADE_CACHE", "~/.sn89/hf-grade"))
KEEP = os.getenv("SN89_PARITY_KEEP") == "1"
ALLOW_REMOTE = os.getenv("SN89_PARITY_ALLOW_REMOTE") == "1"


def guard_remote_base(base: str) -> None:
    """A cold rebuild fetches EVERY published window. Against a local mirror that
    is free; against the live public host it is a sustained ~2.5 hour load on the
    same process that serves the miner dashboard.

    That is not hypothetical. On 2026-08-30 a manual mainnet run of THIS SCRIPT
    saturated partner-webhook for ~2.5 h: /api/sn89/closers/series stopped
    responding, so ClosersPanel fell through to its `candles.length < 2` sentinel
    and every miner's chart rendered BLANK with a 0-to-1 axis. A trader reported
    it in the group before we noticed. File routes on the same process went from
    ~20 ms to 25 s. Killing the job restored everything within seconds.

    So a remote base now has to be asked for out loud. The scheduled timer reads
    a LOCAL mirror and is unaffected.
    """
    if ALLOW_REMOTE:
        return
    host = base.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    raise SystemExit(
        "REFUSING: %s is not local.\n"
        "  A cold rebuild fetches every published window and will saturate the\n"
        "  process that also serves the miner dashboard -- it blanked every\n"
        "  chart for ~2.5 h on 2026-08-30.\n"
        "  Run it against a local mirror, or set SN89_PARITY_ALLOW_REMOTE=1 and\n"
        "  WATCH /api/sn89/closers/series while it runs." % base)


def rows(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {}
    db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        return {k: (st, rsn, hk, t0, pr) for k, st, rsn, hk, t0, pr in
                db.execute("SELECT key, status, reason, hk, t0_ms, pair "
                           "FROM grades")}
    finally:
        db.close()


def main() -> int:
    now = time.time()
    live = rows(os.path.join(LIVE, "hf_grades.db"))
    if not live:
        print("no live grade cache at %s -- nothing to compare" % LIVE)
        return 0

    guard_remote_base(hf.HF_PUBLIC_BASE)
    scratch = tempfile.mkdtemp(prefix="sn89-parity-")
    print("netuid %s · rebuilding from %s" % (config.NETUID, hf.HF_PUBLIC_BASE))
    print("live cache: %d graded calls" % len(live))
    t = time.time()
    try:
        hf_grade.sync_and_grade(hf.HF_PUBLIC_BASE, scratch, now)
        rebuilt = rows(os.path.join(scratch, "hf_grades.db"))
    finally:
        if not KEEP:
            shutil.rmtree(scratch, ignore_errors=True)
    print("rebuild: %d graded calls in %.0fs" % (len(rebuilt), time.time() - t))

    if not rebuilt:
        print("\nFAIL: a cold rebuild produced NOTHING. This is the § 9a failure "
              "mode exactly -- our vector would be the only one on the network.")
        return 2

    # A key the rebuild lacks is NOT automatically a defect: the live cache holds
    # history whose windows may have aged out of the published index. Report it,
    # and never fold it into the disagreement rate.
    only_live = [k for k in live if k not in rebuilt]
    only_new = [k for k in rebuilt if k not in live]
    both = [k for k in live if k in rebuilt]

    out_bad, void_bad = [], []
    for k in both:
        a, b = live[k][0], rebuilt[k][0]
        if a == b:
            continue
        (void_bad if "void" in (a, b) else out_bad).append((k, a, b))

    print("\nshared keys:        %d" % len(both))
    print("live-only:          %d  (window aged out of the public index)"
          % len(only_live))
    print("rebuild-only:       %d  (graded on replay, absent live)" % len(only_new))
    n = len(both) or 1
    print("\noutcome disagree:   %d  (%.3f%%)  won/lost/wash priced differently"
          % (len(out_bad), len(out_bad) / n * 100))
    print("void disagree:      %d  (%.3f%%)  disagreed the call was VALID"
          % (len(void_bad), len(void_bad) / n * 100))

    # Print the hotkey AND t0 AND pair, never a truncated composite key. The key
    # is "<hotkey>:<t0_ns>" and a hotkey is 48 chars, so truncating identified the
    # miner and never the call -- which is exactly the row you need to chase.
    for label, bad in (("OUTCOME", out_bad), ("VOID", void_bad)):
        for k, a, b in bad[:8]:
            hk, t0, pr = live[k][2], live[k][3], live[k][4]
            print("  %-7s %s… %s t0=%s  live=%-5s rebuild=%-5s\n           key=%s"
                  % (label, hk[:10], pr, t0, a, b, k))
        if len(bad) > 8:
            print("  %-7s ... and %d more" % (label, len(bad) - 8))

    if out_bad or void_bad:
        print("\nNOT CLEAN. A third party replaying the public log reaches "
              "different verdicts than the validator committed.")
        return 1
    print("\nCLEAN — a cold rebuild reproduces every shared verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
