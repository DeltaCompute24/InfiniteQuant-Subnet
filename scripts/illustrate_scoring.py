#!/usr/bin/env python3
"""Illustrative validation for the confidence-based scoring rework (SN89 spec §8).

Prints old-vs-new behaviour across four views so a reviewer can see the change,
and asserts the load-bearing facts. Pure W/L only — same inputs as consensus.

    python scripts/illustrate_scoring.py [path/to/journal_export.jsonl]

If a journal export is supplied (or found at $SN89_JOURNAL_EXPORT), view 1
reclassifies REAL hotkeys from it; otherwise it uses the leaderboard fixture.
Export format: one JSON object per line with at least
    {"hotkey": "...", "wins": <int>, "losses": <int>}
(extra keys ignored; "name"/"label" used for display if present).
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


def load_fixture() -> list[tuple[str, int, int]]:
    path = (sys.argv[1] if len(sys.argv) > 1 else None) or os.getenv("SN89_JOURNAL_EXPORT")
    if not path:
        print("(view 1 fixture: visible leaderboard — pass a journal export to use real data)")
        return LEADERBOARD
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            name = str(d.get("name") or d.get("label") or d.get("hotkey", "?"))[:16]
            rows.append((name, int(d["wins"]), int(d["losses"])))
    print(f"(view 1 fixture: {len(rows)} real hotkeys from {path})")
    return rows


# ── view 1: leaderboard reclassification (old gate/tier vs new) ──────────────
def view_reclassification(rows: list[tuple[str, int, int]]) -> None:
    print("\n" + "=" * 80)
    print("VIEW 1 — qualify gate + tier: LEGACY raw-hit  vs  NEW confidence")
    print("=" * 80)
    print(f"{'trader':16} {'W/L':>7} {'n':>3} {'hit%':>6} | "
          f"{'OLD gate':>8} {'OLD tier':>9} | {'Wil90L':>7} {'NEW gate':>8} "
          f"{'shrunk':>7} {'NEW tier':>9}")
    flips = 0
    for name, w, l in rows:
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
        print(f"{name:16} {f'{w}/{l}':>7} {n:>3} {hit*100:5.1f}% | "
              f"{('QUAL' if old_q else 'hold'):>8} {old_t:>9} | "
              f"{lb*100:6.1f}% {('QUAL' if new_q else 'hold'):>8} "
              f"{sh:6.3f} {new_t:>9}")
    print(f"\n{flips} trader(s) change qualification status under the new gate.")
    # spec assertions: a marginal coin-flip drops; a clearly-strong stays/qualifies
    assert not scoring.is_qualified(36, 64), "yogz099 (56% over 64) must NOT qualify"
    assert scoring.is_qualified(28, 10 + 28), "GoodGirl (74% over 38) must qualify"
    assert scoring.tier_multiplier(9, 13) < 2.0, "thin-sample 9/4 must not reach WOLF"


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


def main() -> None:
    print("SN89 confidence-scoring illustration — constants in play:")
    print(f"  QUALIFY_Z={config.QUALIFY_Z}  QUALIFY_LB_FLOOR={config.QUALIFY_LB_FLOOR} "
          f" QUALIFY_MIN_DECISIVE={config.QUALIFY_MIN_DECISIVE}  TIER_PRIOR_K={config.TIER_PRIOR_K}")
    print(f"  WIN_CAP={config.WIN_CAP}  ELIM_Z={config.ELIM_Z}  ELIM_UB_CEIL={config.ELIM_UB_CEIL} "
          f" ELIM_MIN_DECISIVE={config.ELIM_MIN_DECISIVE}")
    view_reclassification(load_fixture())
    view_false_elimination()
    view_cap_inversion()
    view_time_to_qualify()
    print("\nAll illustration assertions passed.")


if __name__ == "__main__":
    main()
