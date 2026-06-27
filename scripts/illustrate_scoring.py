#!/usr/bin/env python3
"""Illustrative validation for the confidence-based scoring rework (SN89 spec §8).

Prints old-vs-new behaviour across four views so a reviewer can see the change,
and asserts the load-bearing facts. Pure W/L only — same inputs as consensus.

    python scripts/illustrate_scoring.py [path/to/journal_export.jsonl] [--check]

If a journal export is supplied (or found at $SN89_JOURNAL_EXPORT), views 1 and
1b reclassify and re-run elimination over REAL hotkeys from it; otherwise the
leaderboard fixture is used and views 2–4 carry the (synthetic) parameter sweeps.

--check exits non-zero if any load-bearing fact fails on the real data (see
check_real_data): a clearly-good trader eliminated/de-qualified, or the new
elimination rule removing anyone the legacy rule would have kept.

Export format: one JSON object per line, at least
    {"hotkey": "...", "wins": <int>, "losses": <int>}
optionally with real decisive history for the elimination view
    {"hotkey": "...", "outcomes": [[<t0_unix>, <won bool>], ...]}
(when "outcomes" is present, wins/losses are derived from it if omitted;
"name"/"label" used for display).
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, scoring   # noqa: E402

DAY = 86_400.0

# ── fixture: the visible leaderboard (wins, losses) ──────────────────────────
LEADERBOARD = [
    ("GoodGirlNaCute", 28, 10),
    ("haroldyeah902",  43, 20),
    ("frednursoy",     23, 13),
    ("Romeo/Venice",    9,  4),
    ("champoiy",       22, 15),
    ("yogz099",        36, 28),
    ("fareedah0x",     21, 16),
]


def _tier_name(mult: float) -> str:
    return {2.0: "WOLF", 1.2: "SHARP", 1.0: "QUALIFIED", 0.0: "—"}.get(mult, f"{mult:g}x")


def _export_path() -> str | None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    return (args[0] if args else None) or os.getenv("SN89_JOURNAL_EXPORT")


def load_records() -> tuple[list[dict], bool]:
    """Returns (records, is_real). Each record: {name, wins, losses, outcomes?}."""
    path = _export_path()
    if not path:
        print("(fixture: visible leaderboard — pass a journal export for REAL data)")
        return ([{"name": n, "wins": w, "losses": l} for n, w, l in LEADERBOARD], False)
    records = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            name = str(d.get("name") or d.get("label") or d.get("hotkey", "?"))[:16]
            outcomes = d.get("outcomes")
            if outcomes is not None:
                outcomes = [(float(t), bool(won)) for t, won in outcomes]
                wins = int(d.get("wins", sum(1 for _, won in outcomes if won)))
                losses = int(d.get("losses", sum(1 for _, won in outcomes if not won)))
            else:
                wins, losses = int(d["wins"]), int(d["losses"])
            records.append({"name": name, "wins": wins, "losses": losses,
                            "outcomes": outcomes})
    n_with = sum(1 for r in records if r["outcomes"])
    print(f"(REAL data: {len(records)} hotkeys from {path}; "
          f"{n_with} carry timestamped outcomes for elimination)")
    return (records, True)


# ── view 1: leaderboard reclassification (old gate/tier vs new) ──────────────
def view_reclassification(records: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("VIEW 1 — qualify gate + tier: LEGACY raw-hit  vs  NEW confidence")
    print("=" * 80)
    print(f"{'trader':16} {'W/L':>7} {'n':>3} {'hit%':>6} | "
          f"{'OLD gate':>8} {'OLD tier':>9} | {'Wil90L':>7} {'NEW gate':>8} "
          f"{'shrunk':>7} {'NEW tier':>9}")
    flips = 0
    for r in records:
        w, l = r["wins"], r["losses"]
        n = w + l
        if n == 0:
            continue
        hit = w / n
        old_q = scoring.is_qualified_legacy(w, n)
        old_t = _tier_name(scoring.win_multiplier(hit)) if old_q else "—"
        new_q = scoring.is_qualified(w, n)
        new_t = _tier_name(scoring.tier_multiplier(w, n)) if new_q else "—"
        lb = scoring.confident_edge(w, n)
        sh = scoring.shrunk_hit_rate(w, n)
        flips += int(old_q != new_q)
        print(f"{r['name']:16} {f'{w}/{l}':>7} {n:>3} {hit*100:5.1f}% | "
              f"{('QUAL' if old_q else 'hold'):>8} {old_t:>9} | "
              f"{lb*100:6.1f}% {('QUAL' if new_q else 'hold'):>8} "
              f"{sh:6.3f} {new_t:>9}")
    print(f"\n{flips} trader(s) change qualification status under the new gate.")


# ── view 1b: REAL elimination — run the actual rule over real sequences ──────
def _legacy_elim(outcomes):
    o = config.CONFIDENCE_SCORING
    config.CONFIDENCE_SCORING = False
    try:
        return scoring.elimination_t0(outcomes)
    finally:
        config.CONFIDENCE_SCORING = o


def view_real_elimination(records: list[dict]) -> dict:
    rows = [r for r in records if r.get("outcomes")]
    print("\n" + "=" * 80)
    print("VIEW 1b — elimination over REAL decisive sequences: LEGACY vs NEW")
    print("=" * 80)
    if not rows:
        print("(no records carry timestamped outcomes — skipping)")
        return {"legacy": set(), "new": set(), "rows": []}
    print(f"{'trader':16} {'W/L':>7} {'n':>4} {'hit%':>6} | "
          f"{'OLD elim':>8} | {'NEW elim':>8}")
    legacy_elim, new_elim, table = set(), set(), []
    for r in sorted(rows, key=lambda r: -(r['wins'] + r['losses'])):
        o = sorted(r["outcomes"])
        n = len(o)
        w = sum(1 for _, won in o if won)
        old = _legacy_elim(o) is not None
        new = scoring.elimination_t0(o) is not None
        if old:
            legacy_elim.add(r["name"])
        if new:
            new_elim.add(r["name"])
        table.append({"name": r["name"], "w": w, "n": n, "old": old, "new": new})
        print(f"{r['name']:16} {f'{w}/{n-w}':>7} {n:>4} {w/n*100:5.1f}% | "
              f"{('ELIM' if old else '—'):>8} | {('ELIM' if new else '—'):>8}")
    print(f"\nlegacy eliminates {len(legacy_elim)}, new eliminates {len(new_elim)} "
          f"of {len(rows)} traders with real history.")
    return {"legacy": legacy_elim, "new": new_elim, "rows": table}


# ── view 2: false-elimination Monte-Carlo, old vs new ────────────────────────
def _p_eliminated(p_true: float, days: int, per_day: float, trials: int,
                  legacy: bool, seed: int) -> float:
    rng = random.Random(seed)
    spacing = DAY / per_day
    n_tr = int(round(days * per_day))
    elim = 0
    fn = scoring._elimination_t0_legacy if legacy else scoring.elimination_t0
    orig = config.CONFIDENCE_SCORING
    config.CONFIDENCE_SCORING = not legacy   # elimination_t0 dispatches on the flag
    try:
        for _ in range(trials):
            dec = [((i + 1) * spacing, rng.random() < p_true) for i in range(n_tr)]
            if fn(dec) is not None:
                elim += 1
    finally:
        config.CONFIDENCE_SCORING = orig
    return elim / trials


def view_false_elimination(trials: int = 4000) -> None:
    print("\n" + "=" * 80)
    print("VIEW 2 — P(permanent elimination) over a 120-day life: LEGACY vs NEW")
    print("=" * 80)
    print(f"{'true hit':>9} {'cadence':>9} | {'OLD floor<40%':>13} | {'NEW lifetime UB':>15}")
    rows = [(0.55, 1.0), (0.55, 2.0), (0.58, 1.0), (0.58, 2.0), (0.35, 1.0)]
    out = {}
    for i, (p, f) in enumerate(rows):
        old = _p_eliminated(p, 120, f, trials, legacy=True, seed=1000 + i)
        new = _p_eliminated(p, 120, f, trials, legacy=False, seed=2000 + i)
        out[(p, f)] = (old, new)
        print(f"{p*100:7.0f}% {f:6.1f}/day | {old*100:11.1f}% | {new*100:13.1f}%")
    # spec: good traders ~<2% under new; a true-35% still reliably eliminated
    for (p, f), (_old, new) in out.items():
        if p >= 0.55:
            assert new < 0.05, f"true-{p:.0%} false-elim {new:.1%} should be <5% under new"
    assert out[(0.35, 1.0)][1] > 0.90, "true-35% must still be eliminated >90% under new"


# ── view 3: calm WOLF vs high-volume grinder (the win-cap inversion) ─────────
def view_cap_inversion() -> None:
    print("\n" + "=" * 80)
    print("VIEW 3 — emission weight: calm WOLF vs grinder, LINEAR vs WIN_CAP")
    print("=" * 80)
    # calm WOLF: ~30 decisive/mo @ 80% -> 24 wins (80% needed to reach WOLF after
    # shrinkage at K=12; a 68% calm trader lands SHARP — the §10 cosmetic effect).
    calm_w, calm_n, calm_trail = 24, 30, 24
    # grinder: ~150 decisive/mo @ ~57% -> 85 wins. QUALIFIES (clears the gate) and
    # out-volumes the calm trader 4:1 — so any inversion here is the CAP, not the gate.
    grnd_w, grnd_n, grnd_trail = 85, 150, 85
    calm_tier = scoring.tier_multiplier(calm_w, calm_n)
    grnd_tier = scoring.tier_multiplier(grnd_w, grnd_n)
    lin_calm, lin_grnd = calm_trail * calm_tier, grnd_trail * grnd_tier
    cap_calm = min(calm_trail, config.WIN_CAP) * calm_tier
    cap_grnd = min(grnd_trail, config.WIN_CAP) * grnd_tier
    print(f"  calm WOLF: {calm_w}/{calm_n} ({calm_w/calm_n:.0%}) shrunk={scoring.shrunk_hit_rate(calm_w,calm_n):.3f} "
          f"tier={_tier_name(calm_tier)} qual={scoring.is_qualified(calm_w,calm_n)} trailing_wins={calm_trail}")
    print(f"  grinder  : {grnd_w}/{grnd_n} ({grnd_w/grnd_n:.0%}) shrunk={scoring.shrunk_hit_rate(grnd_w,grnd_n):.3f} "
          f"tier={_tier_name(grnd_tier)} qual={scoring.is_qualified(grnd_w,grnd_n)} trailing_wins={grnd_trail}")
    print(f"  effective_wins  LINEAR (no cap): calm={lin_calm:6.1f}   grinder={lin_grnd:6.1f}"
          f"   -> grinder wins {lin_grnd/lin_calm:.2f}x")
    print(f"  effective_wins  WIN_CAP={config.WIN_CAP:<3}     : calm={cap_calm:6.1f}   grinder={cap_grnd:6.1f}"
          f"   -> calm wins {cap_calm/max(cap_grnd,1e-9):.2f}x")
    assert scoring.is_qualified(grnd_w, grnd_n), "grinder must qualify (else gate, not cap, is the cause)"
    assert lin_grnd > lin_calm, "without the cap the grinder out-earns the calm trader"
    assert cap_calm > cap_grnd, "WIN_CAP must invert ranking: calm WOLF > grinder"


# ── view 4: time-to-qualify by true edge ─────────────────────────────────────
def view_time_to_qualify() -> None:
    print("\n" + "=" * 80)
    print("VIEW 4 — decisive trades for confident_edge to clear the gate, by edge")
    print("=" * 80)
    ref = {0.72: 8, 0.68: 13, 0.65: 15, 0.60: 36, 0.58: 58}
    print(f"{'true edge':>9} | {'n to qualify':>12} | {'spec ref':>8}")
    last = 0
    for p in (0.72, 0.68, 0.65, 0.60, 0.58):
        n_q = None
        for n in range(config.QUALIFY_MIN_DECISIVE, 400):
            wins = round(p * n)
            if scoring.confident_edge(wins, n) >= config.QUALIFY_LB_FLOOR:
                n_q = n
                break
        print(f"{p*100:7.0f}% | {(n_q if n_q else '>400'):>12} | {ref[p]:>8}")
        if n_q:
            assert n_q >= last, "time-to-qualify must be monotonic in edge"
            last = n_q
    print("\n(stronger edges qualify in a handful of trades; the 54–58% band needs"
          "\n ~50–150+ — the information-theory wall of a binary ±1R channel.)")


def check_math() -> None:
    """Math invariants (hold regardless of data) — keeps the script self-verifying."""
    assert not scoring.is_qualified(36, 64), "56% over 64 must NOT qualify"
    assert scoring.is_qualified(28, 38), "74% over 38 must qualify"
    assert scoring.tier_multiplier(9, 13) < 2.0, "thin-sample 9/4 must not reach WOLF"
    assert scoring.shrunk_hit_rate(6, 10) == pytest_approx(0.545)
    assert scoring.shrunk_hit_rate(60, 100) == pytest_approx(0.589)


def pytest_approx(x, tol=1e-3):
    class _A:
        def __eq__(self, o): return abs(o - x) < tol
    return _A()


def check_real_data(records: list[dict], elim: dict) -> bool:
    """PASS/FAIL on the load-bearing facts. A 'good' trader = raw hit ≥ 0.55 over
    n ≥ 40; a 'clearly-strong' trader = raw hit ≥ 0.62 over n ≥ 30."""
    print("\n" + "=" * 80)
    print("CHECK — pass/fail on real data")
    print("=" * 80)
    fails, warns = [], []
    legacy_elim, new_elim = elim.get("legacy", set()), elim.get("new", set())

    # 1. the new elimination rule must not remove a clearly-good trader
    for r in elim.get("rows", []):
        good = r["n"] >= 40 and r["w"] / r["n"] >= 0.55
        if good and r["new"]:
            fails.append(f"good trader {r['name']} ({r['w']}/{r['n']-r['w']}) ELIMINATED by new rule")
    # 2. new elimination must be a subset of legacy (never kill someone legacy kept)
    for name in new_elim - legacy_elim:
        fails.append(f"{name} eliminated by NEW but not LEGACY (new rule should be more conservative)")
    # 3. clearly-strong traders must still qualify under the new gate
    strong_held = 0
    for r in records:
        n = r["wins"] + r["losses"]
        if n >= 30 and n > 0 and r["wins"] / n >= 0.62:
            if not scoring.is_qualified(r["wins"], n):
                fails.append(f"clearly-strong {r['name']} ({r['wins']}/{r['losses']}) FAILS new gate")
                strong_held += 1
    # context (not failures): qualification churn + implied early burn
    flipped = [r["name"] for r in records
               if (r["wins"] + r["losses"]) > 0
               and scoring.is_qualified_legacy(r["wins"], r["wins"] + r["losses"])
               and not scoring.is_qualified(r["wins"], r["wins"] + r["losses"])]
    n_new_qual = sum(1 for r in records if scoring.is_qualified(r["wins"], r["wins"] + r["losses"]))
    if flipped:
        warns.append(f"{len(flipped)} marginal trader(s) drop from QUALIFIED (expected): "
                     + ", ".join(flipped[:8]) + ("…" if len(flipped) > 8 else ""))
    warns.append(f"{n_new_qual} of {len(records)} traders qualify under the new gate "
                 f"(the rest earn 0 / burn until samples build — expected early on)")

    for w in warns:
        print(f"  NOTE  {w}")
    for f in fails:
        print(f"  FAIL  {f}")
    ok = not fails
    print("\n" + ("CHECK PASSED — safe to merge on this data." if ok
                  else f"CHECK FAILED — {len(fails)} blocking issue(s)."))
    return ok


def main() -> None:
    do_check = "--check" in sys.argv
    print("SN89 confidence-scoring illustration — constants in play:")
    print(f"  QUALIFY_Z={config.QUALIFY_Z}  QUALIFY_LB_FLOOR={config.QUALIFY_LB_FLOOR} "
          f" QUALIFY_MIN_DECISIVE={config.QUALIFY_MIN_DECISIVE}  TIER_PRIOR_K={config.TIER_PRIOR_K}")
    print(f"  WIN_CAP={config.WIN_CAP}  ELIM_Z={config.ELIM_Z}  ELIM_UB_CEIL={config.ELIM_UB_CEIL} "
          f" ELIM_MIN_DECISIVE={config.ELIM_MIN_DECISIVE}")
    check_math()
    records, is_real = load_records()
    view_reclassification(records)
    elim = view_real_elimination(records) if is_real else {"legacy": set(), "new": set(), "rows": []}
    # synthetic parameter sweeps (rate-true, not data-derived) — labelled as such
    print("\n--- synthetic parameter sweeps (true-rate Monte-Carlo, not real data) ---")
    view_false_elimination()
    view_cap_inversion()
    view_time_to_qualify()
    if is_real:
        ok = check_real_data(records, elim)
        if do_check and not ok:
            sys.exit(1)
    elif do_check:
        print("\n--check requested but no real journal export supplied — nothing to gate on.")
        sys.exit(2)
    print("\nAll illustration assertions passed.")


if __name__ == "__main__":
    main()
