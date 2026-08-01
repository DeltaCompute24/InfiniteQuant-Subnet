"""Unified multi-competition blending — the merge that frees the second
on-chain mechanism slot. The load-bearing property is NORMALIZE-THEN-WEIGHT:
a specialist's payout under the blend must equal their payout under a
dedicated mechanism at the same emission split."""
import pytest

from sn89_signals import competitions, config


class TestParseShares:
    def test_parses_and_normalizes(self):
        s = competitions.parse_shares("lf:0.375,hf:0.375,closers:0.25")
        assert s == {"lf": 0.375, "hf": 0.375, "closers": 0.25}

    def test_renormalizes_a_sloppy_spec(self):
        # a hand-edited spec summing to 0.99 must not leak emission
        s = competitions.parse_shares("lf:0.5,hf:0.49")
        assert sum(s.values()) == pytest.approx(1.0)

    def test_rejects_negative_and_duplicate(self):
        with pytest.raises(ValueError):
            competitions.parse_shares("lf:-0.1,hf:1.1")
        with pytest.raises(ValueError):
            competitions.parse_shares("lf:0.5,lf:0.5")


class TestCombine:
    def test_specialist_equivalence(self):
        """An HF-only miner under the blend earns exactly share_hf × their HF
        share — identical to a dedicated mechanism at that split. This is the
        claim 'for the user miners it's the same', as an executable check."""
        lf = {1: 0.6, 0: 0.4}            # uid 0 = burn
        hf = {2: 1.0}                    # uid 2 is an HF specialist
        out = competitions.combine({"lf": lf, "hf": hf},
                                   {"lf": 0.5, "hf": 0.5}, burn_uid=0)
        assert out[2] == pytest.approx(0.5 * 1.0)
        assert out[1] == pytest.approx(0.5 * 0.6)
        assert sum(out.values()) == pytest.approx(1.0)

    def test_dead_competition_burns_its_own_share(self):
        # never redistributed: an attacker who can stall one competition's feed
        # must not be able to inflate the others' payouts
        lf = {1: 1.0}
        out = competitions.combine({"lf": lf, "hf": None},
                                   {"lf": 0.5, "hf": 0.5}, burn_uid=0)
        assert out[0] == pytest.approx(0.5)
        assert out[1] == pytest.approx(0.5)

    def test_unnormalized_vector_is_renormalized(self):
        # defensive: a compute() that returns raw scores cannot skew the blend
        out = competitions.combine({"lf": {1: 3.0, 2: 1.0}},
                                   {"lf": 1.0}, burn_uid=0)
        assert out[1] == pytest.approx(0.75)

    def test_vector_without_a_share_is_ignored(self):
        # a competition staged dark (no share granted) must pay nothing
        out = competitions.combine({"lf": {1: 1.0}, "closers": {2: 1.0}},
                                   {"lf": 1.0}, burn_uid=0)
        assert 2 not in out

    def test_all_dead_burns_everything(self):
        out = competitions.combine({"lf": None, "hf": {}},
                                   {"lf": 0.5, "hf": 0.5}, burn_uid=0)
        assert out == {0: 1.0}

    def test_fourth_competition_is_one_entry(self):
        # the scalability claim: adding a competition is a shares entry + a
        # vector — no structural change
        out = competitions.combine(
            {"lf": {1: 1.0}, "hf": {2: 1.0}, "closers": {3: 1.0}, "x4": {4: 1.0}},
            {"lf": 0.25, "hf": 0.25, "closers": 0.25, "x4": 0.25}, burn_uid=0)
        assert out == {1: pytest.approx(0.25), 2: pytest.approx(0.25),
                       3: pytest.approx(0.25), 4: pytest.approx(0.25)}

    def test_default_shares_come_from_config(self):
        assert sum(config.COMP_WEIGHTS.values()) == pytest.approx(1.0)
        assert set(config.COMP_WEIGHTS) == {"lf", "hf", "closers"}


class TestVersionGate:
    def test_env_flag_forces_active(self, monkeypatch):
        monkeypatch.setattr(config, "COMBINED_WEIGHTS", True)
        monkeypatch.setattr(config, "COMBINED_WEIGHTS_FROM_UNIX", 0)
        assert config.combined_weights_active(0)

    def test_timestamp_gate_flips_at_cutover(self, monkeypatch):
        # the mainnet flip: a committed timestamp, so every validator that has
        # pulled master flips in the same tempo with no env coordination
        monkeypatch.setattr(config, "COMBINED_WEIGHTS", False)
        monkeypatch.setattr(config, "COMBINED_WEIGHTS_FROM_UNIX", 1_800_000_000)
        assert not config.combined_weights_active(1_799_999_999)
        assert config.combined_weights_active(1_800_000_000)

    def test_zero_means_never(self, monkeypatch):
        monkeypatch.setattr(config, "COMBINED_WEIGHTS", False)
        monkeypatch.setattr(config, "COMBINED_WEIGHTS_FROM_UNIX", 0)
        assert not config.combined_weights_active(2**40)


class TestSharesHistory:
    def test_as_of_resolves_the_era(self, monkeypatch):
        monkeypatch.setattr(config, "COMP_WEIGHTS_HISTORY", (
            (0, "lf:0.5,hf:0.5,closers:0.0"),
            (1_800_000_000, "lf:0.375,hf:0.375,closers:0.25"),
        ))
        early = config.comp_weights_as_of(1_799_999_999)
        late = config.comp_weights_as_of(1_800_000_001)
        assert early["closers"] == 0.0
        assert late["closers"] == 0.25
        # an auditor replaying an old epoch gets the old shares — never today's

    def test_launch_era_is_payout_neutral(self):
        # the committed genesis entry must carry closers at 0: the merge ships
        # neutral and the ramp is a LATER history entry, never an edit
        assert config.comp_weights_as_of(0).get("closers", 0.0) == 0.0
