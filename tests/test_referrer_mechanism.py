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

    def test_withheld_pair_contributes_nothing(self):
        # The recruiter is shadowing R1, so that pair pays them nothing — the
        # recruiter half of the pair no-copy gate, and the ONLY place it bites
        # them (the in-band 20% retired 2026-08-03). R2 is unaffected.
        pairs = [(A, R1), (A, R2), (B, R3)]
        tallies = {R1: 3.0, R2: 1.0, R3: 2.0}
        s = scoring.referrer_scores(pairs, tallies, withheld_recruits={R1})
        assert s == {A: 1.0, B: 2.0}

    def test_withholding_every_pair_drops_the_recruiter_entirely(self):
        s = scoring.referrer_scores([(A, R1)], {R1: 3.0}, withheld_recruits={R1})
        assert s == {}

    def test_cold_recruits_score_zero(self):
        # a big base of non-earning recruits pays nothing — the mechanism
        # rewards recruit PERFORMANCE, never list size
        s = scoring.referrer_scores([(A, R1), (A, R2)], {})
        assert s == {}

    def test_weights_pro_rata_capped(self):
        # Pin the instant: the cap is time-varying now, so a test that reads a
        # module constant and calls a function that resolves as-of `time.time()`
        # passes or fails depending on what day it is run.
        NOW = 1_785_000_000.0
        w = scoring.referrer_weights({A: 3.0, B: 1.0}, {A: 5, B: 7}, burn_uid=0,
                                     now_unix=NOW)
        cap = config.miner_emission_cap_as_of(NOW)
        assert w[5] == pytest.approx(cap * 0.75)
        assert w[7] == pytest.approx(cap * 0.25)
        assert w[0] == pytest.approx(1 - cap)
        assert sum(w.values()) == pytest.approx(1.0)

    def test_unregistered_referrer_earns_nothing(self):
        NOW = 1_785_000_000.0
        # no UID → no weight; their score doesn't dilute registered referrers
        w = scoring.referrer_weights({A: 3.0, B: 1.0}, {B: 7}, burn_uid=0,
                                     now_unix=NOW)
        assert w[7] == pytest.approx(config.miner_emission_cap_as_of(NOW))

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


class TestReferrerSuccession:
    """§ referrer succession — a recruiter whose hotkey lost its UID is credited
    on its attested successor."""
    PAIRS = [(A, R1), (A, R2), (B, R3)]

    def test_dead_recruiter_moves_to_successor(self):
        uid = {B: 1, C: 2, R1: 10, R2: 11, R3: 12}          # A has no UID
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5}], uid)
        assert out == sorted([(C, R1), (C, R2), (B, R3)])

    def test_live_recruiter_never_moves(self):
        uid = {A: 0, B: 1, C: 2}
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5}], uid)
        assert out == sorted(self.PAIRS)

    def test_chains_through_repeated_rerolls(self):
        uid = {B: 1, D: 3}                                    # A and C both dead
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5},
                         {"from_hk": C, "to_hk": D, "commit_block": 9}], uid)
        assert (D, R1) in out and (D, R2) in out

    def test_latest_observation_wins(self):
        uid = {B: 1, C: 2, D: 3}
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5},
                         {"from_hk": A, "to_hk": D, "commit_block": 9}], uid)
        assert (D, R1) in out and not any(r == C for r, _ in out)

    def test_dead_end_chain_leaves_pair_in_place(self):
        uid = {B: 1}                                          # successor C also dead
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5}], uid)
        assert out == sorted(self.PAIRS)

    def test_cycle_is_safe(self):
        uid = {B: 1}
        out = scoring.apply_referrer_succession(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 5},
                         {"from_hk": C, "to_hk": A, "commit_block": 6}], uid)
        assert out == sorted(self.PAIRS)

    def test_successor_that_is_the_recruit_is_dropped(self):
        uid = {R1: 10, B: 1}
        out = scoring.apply_referrer_succession(
            [(A, R1)], [{"from_hk": A, "to_hk": R1, "commit_block": 5}], uid)
        assert out == []

    def test_after_transfer(self):
        # A's base was transferred to C; C then deregistered and re-rolled to D
        moved = scoring.apply_referral_transfers(
            self.PAIRS, [{"from_hk": A, "to_hk": C, "commit_block": 100}])
        out = scoring.apply_referrer_succession(
            moved, [{"from_hk": C, "to_hk": D, "commit_block": 200}], {B: 1, D: 3})
        assert (D, R1) in out and (D, R2) in out

    def test_gate_off_before_activation(self):
        assert not config.referrer_succession_active(
            config.REFERRER_SUCCESSION_FROM_UNIX - 1)
        assert config.referrer_succession_active(config.REFERRER_SUCCESSION_FROM_UNIX)

    def test_replay_pays_successor_and_not_before_activation(self):
        from sn89_signals import replay
        t = config.REFERRER_SUCCESSION_FROM_UNIX + 10
        refs = [{"recruiter_hk": A, "recruit_hk": R1, "commit_block": 1,
                 "recruit_reg_block": 100}]
        succ = [{"from_hk": A, "to_hk": C, "commit_block": 5}]
        uid = {C: 7, R1: 10}
        sigs, meta = [], {}
        # no recruit tally -> burn either way; the remap itself is what we check
        w_on = replay.referrer_weights_from_journal(
            sigs, meta, uid, t, referrals=refs, referrer_successions=succ)
        assert set(w_on) <= {config.BURN_UID, 7}


class TestSuccessionCommitment:
    OLD = "5HbkgCR1nx7T9vga9ZCCTTadnefMxWhA3vYFSaic7uA8aGQ2"
    NEW = "5HBg742kVS1KXhKQGJhEMsyNcxatQzpyttipazfqGJSBS98o"

    def test_roundtrip_and_fits_budget(self):
        data = chain.encode_referrer_succession(self.OLD, self.NEW)
        assert len(data.encode()) <= 128
        assert chain.decode_referrer_succession(data) == {"from": self.OLD, "to": self.NEW}

    def test_bad_checksum_dropped(self):
        bad = self.NEW[:-1] + ("3" if self.NEW[-1] != "3" else "4")
        assert chain.decode_referrer_succession(f"sn89refs:1:{self.OLD}:{bad}") is None

    def test_self_succession_dropped(self):
        assert chain.decode_referrer_succession(f"sn89refs:1:{self.OLD}:{self.OLD}") is None

    def test_decode_any_kind(self):
        d = chain._decode_any(chain.encode_referrer_succession(self.OLD, self.NEW))
        assert d and d["kind"] == "referrer_succession"
        assert d["from"] == self.OLD and d["to"] == self.NEW

    def test_not_confused_with_transfer(self):
        assert chain.decode_referral_transfer(
            chain.encode_referrer_succession(self.OLD, self.NEW)) is None


class TestSuccessionJournal:
    """Validator journals sn89refs ONLY from the attestor, latest block wins."""

    def _v(self):
        import importlib.util, pathlib, sqlite3, types
        path = pathlib.Path(__file__).resolve().parents[1] / "neurons" / "validator.py"
        spec = importlib.util.spec_from_file_location("_validator_under_test", path)
        V = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(V)
        db = sqlite3.connect(":memory:")
        db.executescript(V.SCHEMA)
        v = types.SimpleNamespace(db=db)
        v.j = lambda c, b: V.Validator._journal_referrer_succession(v, c, b, 0.0)
        return v

    def test_only_attestor_counts(self):
        v = self._v()
        v.j({"hotkey": "someone", "from": A, "to": B, "commit_block": 5}, 6)
        assert v.db.execute("SELECT COUNT(*) FROM referrer_successions").fetchone()[0] == 0
        v.j({"hotkey": config.SUCCESSION_ATTESTOR_HK, "from": A, "to": B,
             "commit_block": 5}, 6)
        assert v.db.execute("SELECT from_hk, to_hk, commit_block FROM "
                            "referrer_successions").fetchall() == [(A, B, 5)]

    def test_reattest_moves_block_forward_only(self):
        v = self._v()
        at = config.SUCCESSION_ATTESTOR_HK
        v.j({"hotkey": at, "from": A, "to": B, "commit_block": 5}, 6)
        v.j({"hotkey": at, "from": A, "to": C, "commit_block": 7}, 8)
        v.j({"hotkey": at, "from": A, "to": B, "commit_block": 9}, 10)
        v.j({"hotkey": at, "from": A, "to": B, "commit_block": 3}, 11)   # stale replay
        rows = dict(((f, t), cb) for f, t, cb in v.db.execute(
            "SELECT from_hk, to_hk, commit_block FROM referrer_successions"))
        assert rows == {(A, B): 9, (A, C): 7}
        succ = [{"from_hk": f, "to_hk": t, "commit_block": cb}
                for (f, t), cb in rows.items()]
        assert scoring.apply_referrer_succession([(A, R1)], succ, {B: 1, C: 2}) == [(B, R1)]
