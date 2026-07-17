"""Referral / recruiter incentive (§ referral).

Covers the three consensus layers:
  * valid_referral_pairs — chain-anchored validity (lead blocks, front-run
    margin, one-recruiter-per-recruit, breadth cap);
  * referral_pair_suspended_until — the strict pair-scoped no-copy gate
    (sharp episodes, live-overlap episodes, TTL self-clear);
  * compute_weights / weights_from_journal — the bonus math (mutual-conditional,
    recruiter cap, base-not-effective chaining, zero-sum within the pool, and
    byte-parity when the feature is dark).

    pytest tests/test_referral.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, replay, scoring  # noqa: E402
from sn89_signals.schema import Signal  # noqa: E402

NOW = 10_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
OLD = NOW - 40 * DAY
LEAD = config.REFERRAL_MIN_LEAD_BLOCKS


def _ref(recruiter, recruit, commit_block=1000, reg_block=None):
    return {"recruiter_hk": recruiter, "recruit_hk": recruit,
            "commit_block": commit_block,
            "recruit_reg_block": (commit_block + LEAD if reg_block == "valid"
                                  else reg_block)}


def _state(hk, uid, tally=1.0, first_seen=OLD, rep=(30, 40)):
    """MinerState with an exact base tally: one qualified win at t0=NOW decays
    by factor 1.0, so qwins=[(NOW, tally)] ⇒ decayed_qwin_tally == tally."""
    qwins = [(NOW, tally)] if tally > 0 else []
    rw, rd = rep if tally > 0 else (0, 0)   # zero-tally states: never qualified,
    return scoring.MinerState(              # so the probation floor stays out of the way
        hotkey=hk, uid=uid, first_seen_unix=first_seen,
        rep_wins=rw, rep_decisive=rd, trailing_wins=0, qwins=qwins)


def _row(hk, t0, pair="BTCUSD", direction="LONG", status="won", horizon_h=8):
    return scoring.GradedRow(hotkey=hk, trade_pair=pair, direction=direction,
                             t0_unix=t0, status=status, horizon_h=horizon_h)


@pytest.fixture
def lit(monkeypatch):
    monkeypatch.setattr(config, "REFERRAL_ENABLED", True)


@pytest.fixture(autouse=True)
def _no_emission_cap(monkeypatch):
    # Shares are easier to hand-check without the burn cap; the cap interaction
    # has its own test class below (which re-enables it).
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)


# ── on-chain payload codec ─────────────────────────────────────────────────────
class TestCodec:
    def _addr(self):
        from scalecodec.utils.ss58 import ss58_encode
        return ss58_encode(bytes(range(32)), 42)

    def test_roundtrip(self):
        from sn89_signals import chain
        addr = self._addr()
        assert chain.decode_referral(chain.encode_referral(addr)) == {"recruit": addr}

    def test_bad_checksum_rejected(self):
        from sn89_signals import chain
        addr = self._addr()
        bad = addr[:-1] + ("a" if addr[-1] != "a" else "b")
        assert chain.decode_referral(chain.encode_referral(bad)) is None

    def test_kinds_disjoint(self):
        from sn89_signals import chain
        addr = self._addr()
        sig_payload = f"sn89:1:{'0' * 64}:12345:{'0' * 16}"
        assert chain.decode_referral(sig_payload) is None
        assert chain.decode_commitment(chain.encode_referral(addr)) is None
        assert chain._decode_any(sig_payload)["kind"] == "signal"
        assert chain._decode_any(chain.encode_referral(addr))["kind"] == "referral"


# ── validity: valid_referral_pairs ─────────────────────────────────────────────
class TestValidity:
    def test_lead_block_boundary(self):
        ok = _ref("A", "B", commit_block=1000, reg_block=1000 + LEAD)
        late = _ref("A", "C", commit_block=1000, reg_block=1000 + LEAD - 1)
        assert scoring.valid_referral_pairs([ok, late]) == [("A", "B")]

    def test_unregistered_recruit_never_valid(self):
        assert scoring.valid_referral_pairs([_ref("A", "B", reg_block=None)]) == []

    def test_already_registered_recruit_dropped(self):
        # reg block BEFORE the commit — the recruit existed first (or the claim
        # was laundered); permanently invalid.
        assert scoring.valid_referral_pairs([_ref("A", "B", 1000, 900)]) == []

    def test_self_referral_dropped(self):
        assert scoring.valid_referral_pairs([_ref("A", "A", reg_block="valid")]) == []

    def test_one_recruit_one_recruiter_earliest_commit_wins(self):
        r1 = _ref("A", "X", commit_block=1000, reg_block=2000)
        r2 = _ref("B", "X", commit_block=999, reg_block=2000)
        assert scoring.valid_referral_pairs([r1, r2]) == [("B", "X")]

    def test_same_block_tiebreak_is_lexical(self):
        r1 = _ref("Zed", "X", commit_block=1000, reg_block=2000)
        r2 = _ref("Abe", "X", commit_block=1000, reg_block=2000)
        assert scoring.valid_referral_pairs([r1, r2]) == [("Abe", "X")]

    def test_breadth_cap_keeps_earliest_recruits(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRAL_MAX_RECRUITS", 2)
        refs = [_ref("A", f"R{i}", commit_block=1000 + i, reg_block=5000)
                for i in range(4)]
        assert scoring.valid_referral_pairs(refs) == [("A", "R0"), ("A", "R1")]


# ── pair no-copy gate: referral_pair_suspended_until ───────────────────────────
class TestPairNoCopy:
    def test_two_sharp_episodes_suspend_one_does_not(self):
        one = [_row("A", NOW - DAY), _row("B", NOW - DAY + 60)]
        assert scoring.referral_pair_suspended_until(one, "A", "B", NOW) is None
        two = one + [_row("A", NOW - DAY + 2 * HOUR, pair="ETHUSD"),
                     _row("B", NOW - DAY + 2 * HOUR + 60, pair="ETHUSD")]
        until = scoring.referral_pair_suspended_until(two, "A", "B", NOW)
        assert until == pytest.approx(NOW - DAY + 2 * HOUR + 60
                                      + config.REFERRAL_PAIR_TTL_S)

    def test_burst_collapses_to_one_episode(self):
        # 6 sharp follows inside COPY_EPISODE_S = ONE decision, not six.
        t = NOW - DAY
        rows = []
        for i, pair in enumerate(["BTCUSD", "ETHUSD", "SOLUSD",
                                  "XRPUSD", "XAUUSD", "EURUSD"]):
            rows += [_row("A", t + i * 240, pair=pair),
                     _row("B", t + i * 240 + 60, pair=pair)]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None

    def test_either_direction_trips(self):
        # here the RECRUITER shadows the RECRUIT — still suspends
        rows = [_row("B", NOW - DAY), _row("A", NOW - DAY + 60),
                _row("B", NOW - DAY + 2 * HOUR, pair="ETHUSD"),
                _row("A", NOW - DAY + 2 * HOUR + 60, pair="ETHUSD")]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is not None

    def test_overlap_episodes_trip_without_sharp_lag(self):
        # follows ~1h behind (outside COPY_SHARP_LAG_S) but inside the leader's
        # live 8h horizon, on 3 distinct occasions ⇒ overlap trigger.
        rows = []
        for i in range(3):
            t = NOW - 2 * DAY + i * 10 * HOUR
            rows += [_row("A", t), _row("B", t + HOUR)]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is not None

    def test_ttl_self_clears(self, monkeypatch):
        # widen the window so the events still COUNT, but their TTL has expired:
        # the trip is computed, yet the suspension has already self-cleared.
        monkeypatch.setattr(config, "REFERRAL_PAIR_WINDOW_S", int(60 * DAY))
        t = NOW - config.REFERRAL_PAIR_TTL_S - 2 * DAY
        rows = [_row("A", t), _row("B", t + 60),
                _row("A", t + 2 * HOUR, pair="ETHUSD"),
                _row("B", t + 2 * HOUR + 60, pair="ETHUSD")]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None

    def test_outside_window_ignored(self):
        # events older than REFERRAL_PAIR_WINDOW_S never count at all
        t = NOW - config.REFERRAL_PAIR_WINDOW_S - 2 * DAY
        rows = [_row("A", t), _row("B", t + 60),
                _row("A", t + 2 * HOUR, pair="ETHUSD"),
                _row("B", t + 2 * HOUR + 60, pair="ETHUSD")]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None

    def test_third_party_rows_ignored(self):
        rows = [_row("A", NOW - DAY), _row("C", NOW - DAY + 60),
                _row("A", NOW - DAY + 2 * HOUR, pair="ETHUSD"),
                _row("C", NOW - DAY + 2 * HOUR + 60, pair="ETHUSD")]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None


# ── bonus math: compute_weights ────────────────────────────────────────────────
class TestBonusMath:
    def _shares(self, states, pairs, excluded=None):
        w = scoring.compute_weights(states, NOW, excluded_uids=excluded,
                                    referral_pairs=pairs)
        assert sum(w.values()) == pytest.approx(1.0)
        return w

    def test_bonus_shifts_shares_and_is_zero_sum(self, lit):
        states = [_state("A", 1), _state("B", 2), _state("D", 3)]
        w = self._shares(states, [("A", "B")])
        # eff: A = 1 + 0.1·1 = 1.1, B = 1.1, D = 1.0
        assert w[1] == pytest.approx(1.1 / 3.2)
        assert w[2] == pytest.approx(1.1 / 3.2)
        assert w[3] == pytest.approx(1.0 / 3.2)

    def test_mutual_conditional_recruiter_idle_kills_both(self, lit):
        states = [_state("A", 1, tally=0.0), _state("B", 2), _state("D", 3)]
        w = self._shares(states, [("A", "B")])
        assert 1 not in w                       # idle recruiter earns nothing
        assert w[2] == pytest.approx(w[3])      # …and the recruit's bonus lapsed

    def test_mutual_conditional_recruit_idle_kills_recruiter_bonus(self, lit):
        states = [_state("A", 1), _state("B", 2, tally=0.0), _state("D", 3)]
        w = self._shares(states, [("A", "B")])
        assert w[1] == pytest.approx(w[3])      # no bonus off an idle recruit

    def test_immune_recruit_dust_only_no_bonus(self, lit):
        states = [_state("A", 1), _state("B", 2, tally=0.0, first_seen=NOW - DAY),
                  _state("D", 3)]
        w = self._shares(states, [("A", "B")])
        assert w[2] == pytest.approx(config.DUST_WEIGHT, rel=0.01)  # warming up
        assert w[1] == pytest.approx(w[3])      # recruiter bonus needs an EARNING recruit

    def test_multiple_recruits_sum(self, lit):
        states = [_state("A", 1), _state("R1", 2), _state("R2", 3), _state("D", 4)]
        w = self._shares(states, [("A", "R1"), ("A", "R2")])
        # eff: A = 1 + 0.1 + 0.1 = 1.2, recruits 1.1 each, D 1.0
        assert w[1] == pytest.approx(1.2 / 4.4)
        assert w[2] == pytest.approx(1.1 / 4.4)
        assert w[4] == pytest.approx(1.0 / 4.4)

    def test_recruiter_cap_binds(self, lit):
        # tiny recruiter, three big recruits: raw bonus 0.1·3.0 = 0.3 but the cap
        # is REFERRAL_MAX_X (1.0) × own base (0.1) = 0.1.
        states = [_state("A", 1, tally=0.1), _state("R1", 2), _state("R2", 3),
                  _state("R3", 4)]
        w = self._shares(states, [("A", "R1"), ("A", "R2"), ("A", "R3")])
        total = 0.2 + 3 * 1.1
        assert w[1] == pytest.approx(0.2 / total)   # 0.1 base + 0.1 capped bonus

    def test_chain_does_not_compound(self, lit):
        # A→B and B→C: every bonus computed from BASE tallies, so A earns off
        # B's base (1.0), not B's boosted 1.2.
        states = [_state("A", 1), _state("B", 2), _state("C", 3)]
        w = self._shares(states, [("A", "B"), ("B", "C")])
        # eff: A = 1.1, B = 1 + 0.1(recruit of A) + 0.1(recruiter of C) = 1.2, C = 1.1
        total = 1.1 + 1.2 + 1.1
        assert w[1] == pytest.approx(1.1 / total)
        assert w[2] == pytest.approx(1.2 / total)
        assert w[3] == pytest.approx(1.1 / total)

    def test_excluded_party_kills_bonus(self, lit):
        states = [_state("A", 1), _state("B", 2), _state("D", 3)]
        w = self._shares(states, [("A", "B")], excluded={2})
        assert 2 not in w                       # excluded recruit earns nothing
        assert w[1] == pytest.approx(w[3])      # …and generates no recruiter bonus

    def test_dark_flag_ignores_pairs(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRAL_ENABLED", False)
        states = [_state("A", 1), _state("B", 2), _state("D", 3)]
        w_dark = scoring.compute_weights(states, NOW, referral_pairs=[("A", "B")])
        w_none = scoring.compute_weights(states, NOW)
        assert w_dark == w_none


# ── emission-cap interaction: bonuses stay inside the pool ─────────────────────
class TestEmissionCap:
    def test_zero_sum_within_cap(self, lit, monkeypatch):
        monkeypatch.setattr(config, "MINER_EMISSION_CAP", 0.30)
        states = [_state("A", 1), _state("B", 2), _state("D", 3)]
        w_ref = scoring.compute_weights(states, NOW, referral_pairs=[("A", "B")])
        w_no = scoring.compute_weights(states, NOW)
        for w in (w_ref, w_no):
            assert sum(w.values()) == pytest.approx(1.0)
            miner_total = sum(v for u, v in w.items() if u != config.BURN_UID)
            assert miner_total == pytest.approx(0.30)
        # burn identical — the bonus only re-splits the capped miner pool
        assert w_ref[config.BURN_UID] == pytest.approx(w_no[config.BURN_UID])
        assert w_ref[1] > w_no[1] and w_ref[2] > w_no[2] and w_ref[3] < w_no[3]


# ── end-to-end through replay.weights_from_journal ─────────────────────────────
def _pt(hk, pair, direction="LONG"):
    return Signal(trade_pair=pair, direction=direction, tp_bps=150, sl_bps=150,
                  ts_miner=0, hotkey=hk, asset_class="crypto").canonical_bytes().decode()


def _signals(hk, pair, wins, losses, start=NOW - 5 * DAY, step=HOUR):
    out, t = [], start
    for i in range(wins + losses):
        out.append({"commit_hex": f"{hk}-{i}", "hotkey": hk, "t0_unix": t,
                    "status": "won" if i < wins else "lost",
                    "plaintext": _pt(hk, pair)})
        t += step
    return out


def _meta(*hks, first_seen=OLD):
    return {hk: {"first_seen_unix": first_seen, "strikes": 0} for hk in hks}


class TestReplayEndToEnd:
    UIDS = {"A": 1, "B": 2, "D": 3}

    def _journal(self):
        return (_signals("A", "BTCUSD", 20, 8) + _signals("B", "ETHUSD", 20, 8)
                + _signals("D", "SOLUSD", 20, 8))

    def test_dark_byte_parity_with_referrals_present(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRAL_ENABLED", False)
        sigs = self._journal()
        refs = [_ref("A", "B", reg_block="valid")]
        w_with = replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS,
                                             NOW, referrals=refs)
        w_none = replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS, NOW)
        assert w_with == w_none

    def test_lit_bonus_matches_hand_derivation(self, lit):
        sigs = self._journal()
        refs = [_ref("A", "B", reg_block="valid")]
        w = replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS,
                                        NOW, referrals=refs)
        base = {}
        for hk in ("A", "B", "D"):
            dec = [(s["t0_unix"], s["status"] == "won", False)
                   for s in sigs if s["hotkey"] == hk]
            base[hk] = scoring.decayed_qwin_tally(
                scoring.qualified_wins(dec, OLD, False), NOW)
        assert base["A"] > 0 and base["A"] == pytest.approx(base["B"])
        eff = {"A": base["A"] + 0.1 * base["B"],
               "B": 1.1 * base["B"], "D": base["D"]}
        total = sum(eff.values())
        for hk, uid in self.UIDS.items():
            assert w[uid] == pytest.approx(eff[hk] / total)

    def test_pair_copy_suspension_drops_bonus_keeps_base(self, lit):
        sigs = self._journal()
        # recruit B shadows recruiter A twice, hours apart ⇒ 2 sharp episodes
        t1, t2 = NOW - 2 * DAY, NOW - 2 * DAY + 5 * HOUR
        sigs += [
            {"commit_hex": "A-s1", "hotkey": "A", "t0_unix": t1,
             "status": "washed", "plaintext": _pt("A", "XRPUSD")},
            {"commit_hex": "B-s1", "hotkey": "B", "t0_unix": t1 + 60,
             "status": "washed", "plaintext": _pt("B", "XRPUSD")},
            {"commit_hex": "A-s2", "hotkey": "A", "t0_unix": t2,
             "status": "washed", "plaintext": _pt("A", "XAUUSD")},
            {"commit_hex": "B-s2", "hotkey": "B", "t0_unix": t2 + 60,
             "status": "washed", "plaintext": _pt("B", "XAUUSD")},
        ]
        refs = [_ref("A", "B", reg_block="valid")]
        w_sus = replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS,
                                            NOW, referrals=refs)
        w_no = replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS, NOW)
        assert w_sus == w_no        # bonus suspended; BASE emission untouched

    def test_purity_referrals_input_untouched(self, lit):
        sigs = self._journal()
        refs = [_ref("A", "B", reg_block="valid")]
        snap = [dict(r) for r in refs]
        replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS,
                                    NOW, referrals=refs)
        assert refs == snap
