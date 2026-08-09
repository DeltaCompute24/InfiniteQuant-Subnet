"""§ referrer multicomp — scoring a recruiter across LF + HF + Closers.

Before 2026-08-11 a recruiter was paid only for their recruits' LF tally, so a
recruit who was excellent at HF earned their recruiter nothing. These tests pin
the properties that make the wider score safe rather than merely wider:

  * normalize-then-weight, so a competition's natural scale cannot decide the split
  * RAW tallies only — a weight vector carries the immunity dust floor, and
    paying a recruiter for dust makes registering idle hotkeys profitable
  * `reserve` is a burn placeholder, never a competition a recruit can score in
  * the flip is timestamped, so a replay of a pre-flip block is unchanged
"""
import pytest

from sn89_signals import config, replay, scoring


class TestBlendedTallies:
    # LF and Closers on wildly different natural scales: if the blend summed raw
    # numbers, Closers (thousands) would drown LF (single digits) and the shares
    # would be decorative.
    LF = {"a": 3.0, "b": 1.0}
    CL = {"a": 1000.0, "b": 3000.0}

    def test_normalizes_within_each_competition(self):
        out = scoring.blended_recruit_tallies(
            {"lf": self.LF, "closers": self.CL}, {"lf": 0.5, "closers": 0.5})
        # a: 0.5*0.75 + 0.5*0.25 = 0.5 ;  b: 0.5*0.25 + 0.5*0.75 = 0.5
        assert out["a"] == pytest.approx(0.5)
        assert out["b"] == pytest.approx(0.5)

    def test_share_scales_a_competition(self):
        out = scoring.blended_recruit_tallies(
            {"lf": self.LF, "closers": self.CL}, {"lf": 0.9, "closers": 0.1})
        assert out["a"] == pytest.approx(0.9 * 0.75 + 0.1 * 0.25)

    def test_field_sums_to_total_share(self):
        out = scoring.blended_recruit_tallies(
            {"lf": self.LF, "closers": self.CL}, {"lf": 0.35, "closers": 0.10})
        assert sum(out.values()) == pytest.approx(0.45)

    def test_empty_competition_contributes_nothing(self):
        out = scoring.blended_recruit_tallies(
            {"lf": self.LF, "hf": {}}, {"lf": 0.5, "hf": 0.5})
        assert sum(out.values()) == pytest.approx(0.5)
        assert out["a"] == pytest.approx(0.375)

    def test_specialist_in_one_competition_still_scores(self):
        out = scoring.blended_recruit_tallies(
            {"lf": {"a": 1.0}, "hf": {"b": 1.0}}, {"lf": 0.5, "hf": 0.5})
        assert out["a"] == pytest.approx(0.5)
        assert out["b"] == pytest.approx(0.5)

    def test_zero_and_negative_tallies_ignored(self):
        out = scoring.blended_recruit_tallies(
            {"lf": {"a": 1.0, "b": 0.0, "c": -5.0}}, {"lf": 1.0})
        assert out == {"a": pytest.approx(1.0)}


class TestReferrerShares:
    def test_drops_reserve_and_renormalizes(self):
        out = scoring.referrer_shares(
            {"lf": 0.35, "hf": 0.35, "closers": 0.10, "reserve": 0.20})
        assert "reserve" not in out
        assert sum(out.values()) == pytest.approx(1.0)
        # relative weighting preserved: lf:hf:closers = 35:35:10
        assert out["lf"] == pytest.approx(0.35 / 0.80)
        assert out["closers"] == pytest.approx(0.10 / 0.80)

    def test_all_reserve_yields_nothing(self):
        assert scoring.referrer_shares({"reserve": 1.0}) == {}


class TestArming:
    """The flip is a consensus event, so it must be a timestamp and a pre-flip
    replay must be byte-identical whether or not HF/Closers are supplied."""

    SIGNALS = [
        {"commit_hex": "c1", "hotkey": "R1", "t0_unix": 1000.0,
         "status": "won", "is_copy": 0, "plaintext": "{}"},
    ]
    META = {"R1": {"first_seen_unix": 0.0, "strikes": 0}}
    REFS = [{"recruiter_hk": "A", "recruit_hk": "R1",
             "commit_block": 10, "recruit_reg_block": 100}]

    def _call(self, now, extra):
        return replay.referrer_weights_from_journal(
            self.SIGNALS, self.META, {"A": 5, "R1": 6}, now,
            referrals=self.REFS, extra_tallies=extra)

    def test_extra_tallies_ignored_before_flip(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRER_MULTICOMP", False)
        monkeypatch.setattr(config, "REFERRER_MULTICOMP_FROM_UNIX", 2_000_000_000)
        now = 1_000_000_000.0
        assert self._call(now, None) == self._call(
            now, {"hf": {"R1": 999.0}, "closers": {"R1": 999.0}})

    def test_hf_only_recruit_scores_after_flip(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRER_MULTICOMP", True)
        # A recruit with NO LF history at all. Before the flip this recruiter
        # earned nothing; the whole point of the change is that they now do.
        w = replay.referrer_weights_from_journal(
            [], {}, {"A": 5, "R1": 6}, 1_000_000_000.0,
            referrals=self.REFS, extra_tallies={"hf": {"R1": 4.0}})
        assert w.get(5, 0.0) > 0

    def test_no_pairs_still_all_burn(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRER_MULTICOMP", True)
        w = replay.referrer_weights_from_journal(
            [], {}, {"A": 5}, 1_000_000_000.0, referrals=[],
            extra_tallies={"hf": {"R1": 4.0}})
        assert w == {config.BURN_UID: 1.0}


class TestDustIsNotEarning:
    """The sybil property. A recruit sitting on the immunity dust floor has a
    nonzero weight and `emissions_active` true, and must still score its
    recruiter exactly nothing — otherwise the cheapest strategy in the
    mechanism is registering hotkeys that never trade."""

    def test_dust_weight_is_not_a_tally(self):
        # what a dusted miner looks like in a VECTOR
        vector = {"R1": config.DUST_WEIGHT}
        # what it looks like as a TALLY — which is what the blend consumes
        tally = {"R1": 0.0}
        assert scoring.blended_recruit_tallies({"lf": tally}, {"lf": 1.0}) == {}
        # and the guard is that we never hand it the vector
        assert scoring.blended_recruit_tallies({"lf": vector}, {"lf": 1.0}) != {}, (
            "sanity: a vector WOULD score — which is why callers must pass tallies")
