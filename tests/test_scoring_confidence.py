"""Confidence-based scoring rework (SN89 spec §9).

Covers the NEW path (CONFIDENCE_SCORING on, the default): Wilson lower-bound
qualify gate, beta-shrunk tier, win cap, and lifetime upper-bound elimination —
plus the determinism / golden-vector guard the consensus path depends on.

Imports only config + scoring (no bittensor / timelock), so it runs anywhere:

    pytest tests/test_scoring_confidence.py -q
"""
from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, scoring   # noqa: E402

DAY = 86_400.0


def _state(uid, first_seen, rw, rd, tw):
    return scoring.MinerState(hotkey=f"hk{uid}", uid=uid, first_seen_unix=first_seen,
                              rep_wins=rw, rep_decisive=rd, trailing_wins=tw)


@pytest.fixture(autouse=True)
def _confidence_on(monkeypatch):
    monkeypatch.setattr(config, "CONFIDENCE_SCORING", True)


# ── unit: statistics helpers ─────────────────────────────────────────────────
class TestWilson:
    def test_empty_sample(self):
        assert scoring._wilson(0, 0, 1.2816) == (0.0, 1.0)

    def test_bounds_bracket_point_estimate_and_clamp(self):
        eps = 1e-9   # tolerate FP noise at the w=0 / w=n corners
        for w, n in [(1, 1), (0, 5), (5, 5), (3, 10), (28, 38), (50, 100)]:
            lo, hi = scoring._wilson(w, n, 1.2816)
            p = w / n
            assert -eps <= lo <= p + eps
            assert p - eps <= hi <= 1.0 + eps

    def test_known_reference_values(self):
        # spot-checks computed by hand from the Wilson formula (z=1.2816)
        lo, hi = scoring._wilson(28, 38, 1.2816)
        assert lo == pytest.approx(0.6369, abs=1e-3)
        assert hi == pytest.approx(0.8172, abs=1e-3)
        lo2, _ = scoring._wilson(36, 64, 1.2816)
        assert lo2 == pytest.approx(0.4824, abs=1e-3)

    def test_confident_edge_monotone_in_n_at_fixed_rate(self):
        # same observed rate, more evidence ⇒ lower bound never decreases
        prev = -1.0
        for n in (10, 20, 40, 80, 160):
            lb = scoring.confident_edge(round(0.60 * n), n)
            assert lb >= prev - 1e-12
            prev = lb

    def test_shrunk_pulls_toward_half(self):
        assert scoring.shrunk_hit_rate(6, 10) == pytest.approx(0.545, abs=1e-3)
        assert scoring.shrunk_hit_rate(60, 100) == pytest.approx(0.589, abs=1e-3)
        # a thin-sample hot streak is pulled well below its raw rate
        assert scoring.shrunk_hit_rate(9, 13) < 9 / 13


# ── gate + tier behaviour ────────────────────────────────────────────────────
class TestGateAndTier:
    def test_strong_qualifies_marginal_does_not(self):
        assert scoring.is_qualified(28, 38)        # 74% over 38 — real edge
        assert not scoring.is_qualified(36, 64)    # 56% over 64 — a coin flip
        assert not scoring.is_qualified(6, 7)      # below the decisive floor

    def test_thin_sample_wolf_demoted(self):
        # 9/4 (69% over 13) shrinks and does NOT grab WOLF; a big sample is firm
        assert scoring.tier_multiplier(9, 13) == 1.2     # SHARP, not 2.0 WOLF
        assert scoring.tier_multiplier(75, 100) == 2.0   # 75% over 100 — true WOLF

    def test_non_qualifier_tier_is_zero_floor(self):
        assert scoring.tier_multiplier(5, 10) == 0.0     # 50% shrunk < 0.55


# ── win cap (conviction over grind) ──────────────────────────────────────────
class TestWinCap:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def test_cap_inverts_calm_vs_grinder(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 24, 30, 24),    # calm WOLF: 80% over 30, 24 trailing wins
            _state(2, self.OLD, 85, 150, 85),   # grinder: 57% over 150, 85 trailing wins
        ], self.NOW)
        assert w[1] > w[2]                       # capped calm WOLF out-earns the grinder

    def test_cap_binds_only_above_threshold(self):
        # two equal-tier miners, one above the cap: capped at WIN_CAP
        cap = config.WIN_CAP
        w = scoring.compute_weights([
            _state(1, self.OLD, 30, 40, cap),       # exactly at cap
            _state(2, self.OLD, 30, 40, cap + 40),  # well above — clipped to cap
        ], self.NOW)
        assert w[1] == pytest.approx(w[2])


# ── elimination: lifetime upper bound ────────────────────────────────────────
def _p_eliminated(p_true, days, per_day, trials, seed):
    rng = random.Random(seed)
    spacing = DAY / per_day
    n_tr = int(round(days * per_day))
    elim = 0
    for _ in range(trials):
        dec = [((i + 1) * spacing, rng.random() < p_true) for i in range(n_tr)]
        if scoring.elimination_t0(dec) is not None:
            elim += 1
    return elim / trials


class TestElimination:
    def test_good_trader_almost_never_eliminated(self):
        assert _p_eliminated(0.55, 120, 1.0, 3000, seed=1) < 0.05

    def test_genuine_bad_actor_reliably_eliminated(self):
        assert _p_eliminated(0.35, 120, 1.0, 2000, seed=2) > 0.90

    def test_terminal_returns_first_crossing(self):
        # all losses after the min sample: eliminates at the first eligible t0
        dec = [(float(i), False) for i in range(config.ELIM_MIN_DECISIVE + 5)]
        t0 = scoring.elimination_t0(dec)
        assert t0 == float(config.ELIM_MIN_DECISIVE - 1)

    def test_below_min_decisive_never_eliminates(self):
        dec = [(float(i), False) for i in range(config.ELIM_MIN_DECISIVE - 1)]
        assert scoring.elimination_t0(dec) is None


# ── sybil resistance: new gate skims less than the legacy gate ───────────────
class TestSybilShare:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def _sybil_share(self, legacy, monkeypatch):
        monkeypatch.setattr(config, "CONFIDENCE_SCORING", not legacy)
        states = [
            _state(1, self.OLD, 30, 40, 12),   # honest edge: 75% over 40
            _state(2, self.OLD, 9, 16, 6),     # sybil: 56% over 16 (lucky streak)
            _state(3, self.OLD, 10, 18, 7),    # sybil: 56% over 18 (lucky streak)
        ]
        w = scoring.compute_weights(states, self.NOW)
        return w.get(2, 0.0) + w.get(3, 0.0)

    def test_new_gate_skims_no_more_than_legacy(self, monkeypatch):
        new = self._sybil_share(legacy=False, monkeypatch=monkeypatch)
        old = self._sybil_share(legacy=True, monkeypatch=monkeypatch)
        assert new <= old
        assert new == pytest.approx(0.0)   # lucky 56%-over-~17 sybils don't clear the Wilson gate


# ── determinism / golden vector (consensus-critical) ─────────────────────────
class TestDeterminism:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    FIXTURE = [
        (1, OLD, 30, 40, 12),   # WOLF-ish
        (2, OLD, 22, 36, 9),    # SHARP-ish
        (3, OLD, 36, 64, 10),   # marginal — must not qualify
        (4, NOW - 86_400, 0, 0, 0),  # immune dust
    ]

    def _states(self):
        return [_state(*row) for row in self.FIXTURE]

    def test_repeated_runs_byte_identical(self):
        a = scoring.compute_weights(self._states(), self.NOW)
        b = scoring.compute_weights(self._states(), self.NOW)
        assert a == b

    def test_input_order_invariant(self):
        a = scoring.compute_weights(self._states(), self.NOW)
        shuffled = self._states()
        random.Random(7).shuffle(shuffled)
        b = scoring.compute_weights(shuffled, self.NOW)
        assert a == b

    def test_golden_values(self):
        w = scoring.compute_weights(self._states(), self.NOW)
        assert 3 not in w                                   # marginal never qualifies
        assert w[4] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)
        assert sum(w.values()) == pytest.approx(1.0)
        # uid1 (WOLF tier × 12 wins) outweighs uid2 (SHARP tier × 9 wins)
        assert w[1] > w[2] > 0.0
