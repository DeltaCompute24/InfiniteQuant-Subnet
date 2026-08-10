"""MINER_EMISSION_CAP as a time-varying consensus constant.

Until 2026-08-10 the cap was a bare module constant, so every change to it
silently rewrote the replay of every past block — an auditor rebuilding a
2026-07-15 vector with an August checkout burned 60% where the chain had burned
20%. These tests pin the recovered eras and the retirement.
"""
import pytest

from sn89_signals import config, hf, scoring

# era boundaries recovered from git (commit instants)
PRE_CAP = 1_783_000_000      # before 2026-07-12
ERA_20 = 1_784_000_000       # inside 0.20
ERA_30 = 1_784_500_000       # inside 0.30
ERA_60 = 1_785_000_000       # inside 0.60
RETIRED = 1_786_400_000      # after 2026-08-10 02:30 UTC


class TestCapHistory:
    def test_eras(self):
        assert config.miner_emission_cap_as_of(PRE_CAP) == 1.0
        assert config.miner_emission_cap_as_of(ERA_20) == 0.20
        assert config.miner_emission_cap_as_of(ERA_30) == 0.30
        assert config.miner_emission_cap_as_of(ERA_60) == 0.60
        assert config.miner_emission_cap_as_of(RETIRED) == 1.0

    def test_monotonic_boundaries(self):
        """A history whose instants are out of order resolves to whichever row
        happens to be last, which is a silent mispay rather than an error."""
        ts = [t for t, _ in config.MINER_EMISSION_CAP_HISTORY]
        assert ts == sorted(ts)
        assert ts[0] == 0, "history must cover all time back to the epoch"

    def test_hf_follows_mecid0(self):
        for t in (PRE_CAP, ERA_20, ERA_30, ERA_60, RETIRED):
            assert hf.hf_miner_emission_cap_as_of(t) == \
                config.miner_emission_cap_as_of(t)


class TestRetirementPaysTheField:
    """The whole point of retiring the cap: the field receives what the chain
    split allocates to it, instead of a fraction of it."""

    OLD = 1_700_000_000.0

    def _ms(self, uid, now, qwins):
        return scoring.MinerState(
            hotkey=f"hk{uid}", uid=uid, first_seen_unix=self.OLD,
            rep_wins=30, rep_decisive=40, trailing_wins=30, qwins=qwins)

    def test_capped_era_burns_the_remainder(self):
        w = scoring.compute_weights([self._ms(1, ERA_60, [(ERA_60, 2.0)])], ERA_60)
        field = sum(v for u, v in w.items() if u != config.BURN_UID)
        assert field == pytest.approx(0.60, abs=1e-6)
        assert w[config.BURN_UID] == pytest.approx(0.40, abs=1e-6)

    def test_retired_era_pays_the_whole_vector(self):
        w = scoring.compute_weights([self._ms(1, RETIRED, [(RETIRED, 2.0)])], RETIRED)
        field = sum(v for u, v in w.items() if u != config.BURN_UID)
        assert field == pytest.approx(1.0, abs=1e-6)
        assert w.get(config.BURN_UID, 0.0) == pytest.approx(0.0, abs=1e-6)

    def test_empty_field_still_burns_after_retirement(self):
        """Retiring the cap must not invent earners. With nobody qualified the
        whole vector still burns — otherwise an idle competition would pay out."""
        w = scoring.compute_weights([], RETIRED)
        assert w == {config.BURN_UID: 1.0}

    def test_referrer_pool_uncapped_after_retirement(self):
        w = scoring.referrer_weights({"A": 3.0, "B": 1.0}, {"A": 5, "B": 7},
                                     burn_uid=0, now_unix=RETIRED)
        assert w[5] == pytest.approx(0.75)
        assert w[7] == pytest.approx(0.25)
        assert w.get(0, 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_referrer_pool_still_burns_with_no_scores(self):
        assert scoring.referrer_weights({}, {}, burn_uid=0,
                                        now_unix=RETIRED) == {0: 1.0}
