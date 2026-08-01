"""Referrer mechanism (mecid 1) + one-time referral-base transfers (sn89refx).

The transfer rules are the security surface: once-ever per original hotkey,
earliest-block wins, non-chaining, self-inert. Every test here traces to a way
a referral base could otherwise be stolen or double-counted."""
import pytest

from sn89_signals import chain, config, scoring

A, B, C, D = "hkA", "hkB", "hkC", "hkD"
R1, R2, R3 = "recruit1", "recruit2", "recruit3"


class TestTransferCommitment:
    VALID = "5HbkgCR1nx7T9vga9ZCCTTadnefMxWhA3vYFSaic7uA8aGQ2"

    def test_roundtrip(self):
        data = chain.encode_referral_transfer(self.VALID)
        assert chain.decode_referral_transfer(data) == {"to": self.VALID}

    def test_bad_checksum_dropped(self):
        assert chain.decode_referral_transfer(
            "sn89refx:1:5HbkgCR1nx7T9vga9ZCCTTadnefMxWhA3vYFSaic7uA8aGQ3") is None

    def test_decode_any_kinds(self):
        d = chain._decode_any(chain.encode_referral_transfer(self.VALID))
        assert d and d["kind"] == "referral_transfer" and d["to"] == self.VALID


class TestApplyTransfers:
    PAIRS = [(A, R1), (A, R2), (B, R3)]

    def test_remaps_whole_base(self):
        out = scoring.apply_referral_transfers(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 100}])
        assert (C, R1) in out and (C, R2) in out and (B, R3) in out
        assert not any(r == A for r, _ in out)

    def test_once_only_earliest_block_wins(self):
        # a second transfer (even later, even elsewhere) is permanently inert
        out = scoring.apply_referral_transfers(
            self.PAIRS, [{"from_hk": A, "to_hk": D, "commit_block": 200},
                         {"from_hk": A, "to_hk": C, "commit_block": 100}])
        assert (C, R1) in out and not any(r == D for r, _ in out)

    def test_non_chaining(self):
        # A→C then C→D: A's recruits land on C and STAY there — C's transfer
        # moves only C's own original pairs (there are none here)
        out = scoring.apply_referral_transfers(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 100},
                         {"from_hk": C, "to_hk": D, "commit_block": 150}])
        assert (C, R1) in out and not any(r == D for r, _ in out)

    def test_self_transfer_inert(self):
        out = scoring.apply_referral_transfers(
            self.PAIRS, [{"from_hk": A, "to_hk": A, "commit_block": 100}])
        assert (A, R1) in out


class TestReferrerScores:
    def test_score_is_sum_of_recruit_tallies(self):
        pairs = [(A, R1), (A, R2), (B, R3)]
        tallies = {R1: 3.0, R2: 1.0, R3: 2.0}
        s = scoring.referrer_scores(pairs, tallies)
        assert s == {A: 4.0, B: 2.0}

    def test_cold_recruits_score_zero(self):
        # a big base of non-earning recruits pays nothing — the mechanism
        # rewards recruit PERFORMANCE, never list size
        s = scoring.referrer_scores([(A, R1), (A, R2)], {})
        assert s == {}

    def test_weights_pro_rata_capped(self):
        w = scoring.referrer_weights({A: 3.0, B: 1.0}, {A: 5, B: 7}, burn_uid=0)
        cap = config.MINER_EMISSION_CAP
        assert w[5] == pytest.approx(cap * 0.75)
        assert w[7] == pytest.approx(cap * 0.25)
        assert w[0] == pytest.approx(1 - cap)
        assert sum(w.values()) == pytest.approx(1.0)

    def test_unregistered_referrer_earns_nothing(self):
        # no UID → no weight; their score doesn't dilute registered referrers
        w = scoring.referrer_weights({A: 3.0, B: 1.0}, {B: 7}, burn_uid=0)
        assert w[7] == pytest.approx(config.MINER_EMISSION_CAP)

    def test_empty_field_burns(self):
        assert scoring.referrer_weights({}, {}, burn_uid=0) == {0: 1.0}


class TestBonusRetirement:
    def test_recruiter_bonus_gated_by_flag(self, monkeypatch):
        """When mecid-1 pays referrers, the in-band 20% recruiter share-shift
        must retire (same referral paid from two pools otherwise). The
        recruit's own entry bonus stays."""
        states = [
            scoring.MinerState(hotkey=A, uid=1, first_seen_unix=0,
                               rep_wins=10, rep_decisive=12, trailing_wins=10,
                               qwins=[(1_000_000.0, 5.0)]),
            scoring.MinerState(hotkey=B, uid=2, first_seen_unix=0,
                               rep_wins=10, rep_decisive=12, trailing_wins=10,
                               qwins=[(1_000_000.0, 5.0)]),
        ]
        now = 1_000_000.0 + 3600
        monkeypatch.setattr(config, "REFERRER_MECID1", False)
        w_off = scoring.compute_weights(states, now, referral_pairs=[(A, B)])
        monkeypatch.setattr(config, "REFERRER_MECID1", True)
        w_on = scoring.compute_weights(states, now, referral_pairs=[(A, B)])
        # with the flag on the recruiter (uid 1) loses its bonus edge over the
        # recruit-boosted uid 2; recruit bonus still applies either way
        assert w_on[1] < w_off[1]
        assert w_on[2] > w_on[1] * 0.99  # recruit keeps its 10% boost
