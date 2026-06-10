"""Collateral unit tests — elimination floor scan, collateral-gated weights,
withdraw-request signatures, ss58→H160 ledger keys.

    pytest tests/test_collateral.py -x -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import config, scoring

DAY = 86_400
RAO = 10**9


def _events(pattern: str, start: float = 0.0, spacing: float = 4 * 3600):
    """'W'/'L' string → [(t0, won)] spaced evenly from start."""
    return [(start + i * spacing, c == "W") for i, c in enumerate(pattern)]


# ── elimination floor scan (scoring.elimination_t0) ───────────────────────────
class TestEliminationFloor:
    def test_clean_history_survives(self):
        # 60% hit over 30 decisive — comfortably above the 0.40 floor
        assert scoring.elimination_t0(_events("WWWLL" * 6)) is None

    def test_sustained_failure_eliminates(self):
        # 20 wins clear the lifetime gate; once they slide out of the trailing
        # window, a pure-loss run crosses the floor at the ELIM_MIN_TRAILING-th
        # loss
        wins = _events("W" * config.ELIM_MIN_DECISIVE)
        losses = _events("L" * config.ELIM_MIN_TRAILING,
                         start=wins[-1][0] + config.SCORE_WINDOW_S + DAY)
        assert scoring.elimination_t0(wins + losses) == losses[-1][0]

    def test_floor_needs_lifetime_sample(self):
        # all losses but fewer than ELIM_MIN_DECISIVE — floor can't trigger
        ev = _events("L" * (config.ELIM_MIN_DECISIVE - 1))
        assert scoring.elimination_t0(ev) is None

    def test_floor_needs_trailing_sample(self):
        # 20 old wins, then a SINGLE recent loss long after the window slid:
        # trailing sample is 1 (< ELIM_MIN_TRAILING) so no elimination
        ev = _events("W" * config.ELIM_MIN_DECISIVE)
        ev.append((ev[-1][0] + config.SCORE_WINDOW_S + DAY, False))
        assert scoring.elimination_t0(ev) is None

    def test_verdict_is_evaluation_time_independent(self):
        # the scan looks only at t0s, so the same journal gives the same
        # verdict no matter when (or how often) it is evaluated
        ev = _events("W" * 20 + "L" * 20)
        assert scoring.elimination_t0(ev) == scoring.elimination_t0(list(reversed(ev)))

    def test_elimination_t0_is_first_crossing(self):
        ev = _events("W" * 20 + "L" * 40)
        t0 = scoring.elimination_t0(ev)
        # recovery after the first crossing must not move the verdict
        later = ev + _events("W" * 50, start=ev[-1][0] + DAY)
        assert scoring.elimination_t0(later) == t0

    def test_boundary_exact_floor_survives(self):
        # exactly at the floor is NOT below it
        n = config.ELIM_MIN_TRAILING
        wins = int(config.ELIM_FLOOR_HIT * n)
        assert wins / n == config.ELIM_FLOOR_HIT  # pattern hits the boundary
        pad = "W" * config.ELIM_MIN_DECISIVE
        ev = _events(pad)
        tail = _events("W" * wins + "L" * (n - wins),
                       start=ev[-1][0] + config.SCORE_WINDOW_S + DAY)
        assert scoring.elimination_t0(ev + tail) is None


# ── collateral-gated weights ──────────────────────────────────────────────────
def _state(uid: int, collateral_alpha: float, wins=10, decisive=15,
           lifetime=25, age_s=30 * DAY):
    return scoring.MinerState(
        hotkey=f"hk{uid}", uid=uid, first_seen_unix=1_000_000.0,
        lifetime_decisive=lifetime, trailing_wins=wins,
        trailing_decisive=decisive, collateral_rao=int(collateral_alpha * RAO))


class TestCollateralGate:
    NOW = 1_000_000.0 + 30 * DAY
    MIN = 100 * RAO

    def test_gating_off_ignores_collateral(self):
        w = scoring.compute_weights([_state(1, 0)], self.NOW, min_collateral_rao=0)
        assert w[1] > 0.9

    def test_unfunded_gets_dust(self):
        w = scoring.compute_weights([_state(1, 0), _state(2, 100)], self.NOW,
                                    min_collateral_rao=self.MIN)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)
        assert w[2] > 0.9

    def test_partially_funded_gets_dust(self):
        w = scoring.compute_weights([_state(1, 99.9), _state(2, 100)], self.NOW,
                                    min_collateral_rao=self.MIN)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_all_unfunded_burns(self):
        w = scoring.compute_weights([_state(1, 0)], self.NOW,
                                    min_collateral_rao=self.MIN)
        assert w[config.BURN_UID] > 0.99


# ── withdraw request signatures ───────────────────────────────────────────────
class TestWithdrawRequest:
    @pytest.fixture()
    def keypair(self):
        bt = pytest.importorskip("bittensor")
        return bt.Keypair.create_from_uri("//Alice")

    def _signed(self, keypair, **overrides):
        import time as _t
        from sn89_signals import collateral
        req = {"amount_rao": 5 * RAO, "coldkey": keypair.ss58_address,
               "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
               "nonce": "abc123", "timestamp_ms": int(_t.time() * 1000)}
        req.update(overrides)
        msg = collateral.withdraw_request_message(
            req["amount_rao"], req["coldkey"], req["hotkey"],
            req["nonce"], req["timestamp_ms"])
        req["signature"] = keypair.sign(msg).hex()
        return req

    def test_valid_request_passes(self, keypair):
        from sn89_signals import collateral
        assert collateral.verify_withdraw_request(self._signed(keypair)) is None

    def test_tampered_amount_rejected(self, keypair):
        from sn89_signals import collateral
        req = self._signed(keypair)
        req["amount_rao"] += 1
        assert collateral.verify_withdraw_request(req) == "bad signature"

    def test_stale_timestamp_rejected(self, keypair):
        from sn89_signals import collateral
        req = self._signed(keypair, timestamp_ms=1)
        assert "stale" in collateral.verify_withdraw_request(req)

    def test_wrong_coldkey_rejected(self, keypair):
        import bittensor as bt
        from sn89_signals import collateral
        req = self._signed(keypair)
        req["coldkey"] = bt.Keypair.create_from_uri("//Bob").ss58_address
        assert collateral.verify_withdraw_request(req) is not None


# ── ledger keys ───────────────────────────────────────────────────────────────
class TestLedgerKeys:
    def test_ss58_to_h160_is_account_id_prefix(self):
        pytest.importorskip("web3")
        pytest.importorskip("scalecodec")
        from scalecodec.utils.ss58 import ss58_decode
        from sn89_signals import collateral
        ss58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        h160 = collateral.ss58_to_h160(ss58)
        assert h160.lower() == "0x" + ss58_decode(ss58)[:40].lower()

    def test_distinct_hotkeys_distinct_keys(self):
        pytest.importorskip("web3")
        pytest.importorskip("scalecodec")
        from sn89_signals import collateral
        a = collateral.ss58_to_h160("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
        b = collateral.ss58_to_h160("5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
        assert a != b
