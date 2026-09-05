"""Referral / recruiter incentive (§ referral).

Covers the three consensus layers:
  * valid_referral_pairs — chain-anchored validity (lead blocks, front-run
    margin, one-recruiter-per-recruit, breadth cap);
  * referral_pair_followers — the strict pair-scoped no-copy gate, DIRECTION-
    AWARE (sharp episodes, live-overlap episodes, TTL self-clear, and which
    SIDE forfeits its bonus);
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


# Pairs kept off BTCUSD/ETHUSD/SOLUSD so shadow rows never collide with the
# base journal in TestReplayEndToEnd.
SHADOW_PAIRS = ["XRPUSD", "XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"]


def _sharp_rows(leader, follower, n, start=NOW - 5 * DAY):
    """n SHARP follow episodes — each on its own pair, spaced well beyond
    COPY_EPISODE_S so the collapse counts them separately. Sized off the config
    threshold by every caller, so widening the gate never silently invalidates
    the test the way a hardcoded 2 did (2026-08-18)."""
    rows = []
    for i in range(n):
        t = start + i * 2 * HOUR
        pair = SHADOW_PAIRS[i % len(SHADOW_PAIRS)]
        rows += [_row(leader, t, pair=pair), _row(follower, t + 60, pair=pair)]
    return rows


@pytest.fixture
def lit(monkeypatch):
    monkeypatch.setattr(config, "REFERRAL_ENABLED", True)


@pytest.fixture(autouse=True)
def _no_emission_cap(monkeypatch):
    # Shares are easier to hand-check without the burn cap; the cap interaction
    # has its own test class below (which re-enables it).
    monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 1.0),))


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
    def test_sharp_episodes_trip_at_threshold_not_below(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        below = _sharp_rows("A", "B", n - 1)
        assert scoring.referral_pair_suspended_until(below, "A", "B", NOW) is None
        at = _sharp_rows("A", "B", n)
        until = scoring.referral_pair_suspended_until(at, "A", "B", NOW)
        last = max(r.t0_unix for r in at if r.hotkey == "B")
        assert until == pytest.approx(last + config.REFERRAL_PAIR_TTL_S)

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
        rows = _sharp_rows("B", "A", config.REFERRAL_PAIR_SHARP_EPISODES)
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is not None


# ── the gate names the FOLLOWER (2026-08-24) ───────────────────────────────────
class TestPairNoCopyDirection:
    """Before this, a trip dropped the whole pair: the account that was copied
    FROM lost its bonus alongside the copier. The 2026-08-18 widening to 4/8 was
    prompted by exactly that landing on the wrong party."""

    def test_only_the_follower_is_named(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        f = scoring.referral_pair_followers(_sharp_rows("A", "B", n), "A", "B", NOW)
        assert set(f) == {"B"}

    def test_direction_reverses_with_the_evidence(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        f = scoring.referral_pair_followers(_sharp_rows("B", "A", n), "A", "B", NOW)
        assert set(f) == {"A"}

    def test_both_sides_can_be_suspended_independently(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        rows = (_sharp_rows("A", "B", n, start=NOW - 20 * DAY)
                + _sharp_rows("B", "A", n, start=NOW - 5 * DAY))
        f = scoring.referral_pair_followers(rows, "A", "B", NOW)
        assert set(f) == {"A", "B"}
        # each clock runs off that side's OWN last event, so they differ
        assert f["A"] > f["B"]

    def test_one_side_expiring_does_not_free_the_other(self, monkeypatch):
        monkeypatch.setattr(config, "REFERRAL_PAIR_WINDOW_S", int(90 * DAY))
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        stale = NOW - config.REFERRAL_PAIR_TTL_S - 5 * DAY
        rows = (_sharp_rows("A", "B", n, start=stale)      # B's trip has expired
                + _sharp_rows("B", "A", n, start=NOW - 3 * DAY))
        f = scoring.referral_pair_followers(rows, "A", "B", NOW)
        assert set(f) == {"A"}

    def test_below_threshold_names_nobody(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        assert scoring.referral_pair_followers(
            _sharp_rows("A", "B", n - 1), "A", "B", NOW) == {}

    def test_symmetric_wrapper_still_answers_is_the_pair_flagged(self):
        n = config.REFERRAL_PAIR_SHARP_EPISODES
        rows = _sharp_rows("A", "B", n)
        f = scoring.referral_pair_followers(rows, "A", "B", NOW)
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) == max(f.values())

    def test_overlap_episodes_trip_without_sharp_lag(self):
        # follows ~1h behind (outside COPY_SHARP_LAG_S) but inside the leader's
        # live 8h horizon, on REFERRAL_PAIR_OVERLAP_EPISODES distinct occasions ⇒ overlap trigger.
        rows = []
        for i in range(config.REFERRAL_PAIR_OVERLAP_EPISODES):
            t = NOW - 2 * DAY + i * 10 * HOUR
            rows += [_row("A", t), _row("B", t + HOUR)]
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is not None

    def test_overlap_ends_at_journaled_exit_not_horizon(self):
        # HELD-TO-EXIT (2026-09-05). Same shape as the test above, but every
        # leader call RESOLVED (exit_unix) before the follower entered. A call
        # that has already hit its band is not held by anyone, so none of these
        # is an overlap and the pair is clean. Harold/5HNCsrgx: 4 of the 8
        # events that tripped the recruiter were entries after the recruit's
        # call had closed, one of them 79 min after a TAOUSD win.
        rows = []
        for i in range(config.REFERRAL_PAIR_OVERLAP_EPISODES):
            t = NOW - 2 * DAY + i * 10 * HOUR
            lead = _row("A", t)
            lead.exit_unix = t + 30 * 60          # resolved 30 min in
            rows += [lead, _row("B", t + HOUR)]   # B enters 30 min AFTER the close
        assert scoring.referral_pair_followers(rows, "A", "B", NOW) == {}

    def test_overlap_still_counts_while_leader_is_open(self):
        # exit_unix AFTER the follower's entry: the call was live, still a copy.
        rows = []
        for i in range(config.REFERRAL_PAIR_OVERLAP_EPISODES):
            t = NOW - 2 * DAY + i * 10 * HOUR
            lead = _row("A", t)
            lead.exit_unix = t + 3 * HOUR
            rows += [lead, _row("B", t + HOUR)]
        assert set(scoring.referral_pair_followers(rows, "A", "B", NOW)) == {"B"}

    def test_pending_leader_keeps_horizon_end(self):
        # no exit_unix (still open / caller did not supply): horizon rule as before
        rows = []
        for i in range(config.REFERRAL_PAIR_OVERLAP_EPISODES):
            t = NOW - 2 * DAY + i * 10 * HOUR
            rows += [_row("A", t, status="pending"), _row("B", t + HOUR)]
        assert set(scoring.referral_pair_followers(rows, "A", "B", NOW)) == {"B"}

    def test_ttl_self_clears(self, monkeypatch):
        # widen the window so the events still COUNT, but their TTL has expired:
        # the trip is computed, yet the suspension has already self-cleared.
        monkeypatch.setattr(config, "REFERRAL_PAIR_WINDOW_S", int(60 * DAY))
        t = NOW - config.REFERRAL_PAIR_TTL_S - 2 * DAY
        rows = _sharp_rows("A", "B", config.REFERRAL_PAIR_SHARP_EPISODES, start=t)
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None

    def test_outside_window_ignored(self):
        # events older than REFERRAL_PAIR_WINDOW_S never count at all
        t = NOW - config.REFERRAL_PAIR_WINDOW_S - 2 * DAY
        rows = _sharp_rows("A", "B", config.REFERRAL_PAIR_SHARP_EPISODES, start=t)
        assert scoring.referral_pair_suspended_until(rows, "A", "B", NOW) is None

    def test_third_party_rows_ignored(self):
        rows = _sharp_rows("A", "C", config.REFERRAL_PAIR_SHARP_EPISODES)
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
        # eff: A = 1 + 0.2·1 = 1.2, B = 1.1, D = 1.0
        assert w[1] == pytest.approx(1.2 / 3.3)
        assert w[2] == pytest.approx(1.1 / 3.3)
        assert w[3] == pytest.approx(1.0 / 3.3)

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
        # eff: A = 1 + 0.2 + 0.2 = 1.4, recruits 1.1 each, D 1.0
        assert w[1] == pytest.approx(1.4 / 4.6)
        assert w[2] == pytest.approx(1.1 / 4.6)
        assert w[4] == pytest.approx(1.0 / 4.6)

    def test_recruiter_cap_binds(self, lit):
        # tiny recruiter, three big recruits: raw bonus 0.2·3.0 = 0.6 but the cap
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
        # eff: A = 1.2, B = 1 + 0.1(recruit of A) + 0.2(recruiter of C) = 1.3, C = 1.1
        total = 1.2 + 1.3 + 1.1
        assert w[1] == pytest.approx(1.2 / total)
        assert w[2] == pytest.approx(1.3 / total)
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
        monkeypatch.setattr(config, "MINER_EMISSION_CAP_HISTORY", ((0, 0.30),))
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
        eff = {"A": base["A"] + 0.2 * base["B"],
               "B": 1.1 * base["B"], "D": base["D"]}
        total = sum(eff.values())
        for hk, uid in self.UIDS.items():
            assert w[uid] == pytest.approx(eff[hk] / total)

    def _shadow(self, sigs, leader, follower):
        """`follower` shadows `leader` enough to trip the sharp gate. Washed
        rows so the shadowing itself moves neither side's tally — what changes
        must be the BONUS and only the bonus."""
        for i in range(config.REFERRAL_PAIR_SHARP_EPISODES):
            t = NOW - 2 * DAY + i * 5 * HOUR
            pair = SHADOW_PAIRS[i % len(SHADOW_PAIRS)]
            sigs += [
                {"commit_hex": f"{leader}-s{i}", "hotkey": leader, "t0_unix": t,
                 "status": "washed", "plaintext": _pt(leader, pair)},
                {"commit_hex": f"{follower}-s{i}", "hotkey": follower,
                 "t0_unix": t + 60, "status": "washed",
                 "plaintext": _pt(follower, pair)},
            ]
        return sigs

    def test_copying_recruit_loses_its_own_bonus_only(self, lit):
        # B (the recruit) shadows A. B forfeits its +10%; A keeps the recruiter
        # bonus it earns off B, and both BASE tallies are untouched. This is the
        # case that reached us as "my bonus has been paused" from the recruiter.
        sigs = self._shadow(self._journal(), "A", "B")
        refs = [_ref("A", "B", reg_block="valid")]
        meta = _meta("A", "B", "D")
        w = replay.weights_from_journal(sigs, meta, self.UIDS, NOW, referrals=refs)
        base = {}
        for hk in ("A", "B", "D"):
            dec = [(s["t0_unix"], s["status"] == "won", False)
                   for s in sigs if s["hotkey"] == hk and s["status"] != "washed"]
            graded = [(s["t0_unix"], s["status"] == "washed")
                      for s in sigs if s["hotkey"] == hk]
            base[hk] = scoring.decayed_qwin_tally(
                scoring.qualified_wins(dec, OLD, False, graded=graded), NOW)
        eff = {"A": base["A"] + config.REFERRAL_RECRUITER_BONUS * base["B"],
               "B": base["B"],                       # +10% withheld: B copied
               "D": base["D"]}
        total = sum(eff.values())
        for hk, uid in self.UIDS.items():
            assert w[uid] == pytest.approx(eff[hk] / total)

    def test_copying_recruiter_does_not_cost_the_recruit(self, lit):
        # Mirror image: A (the recruiter) shadows B. A forfeits its recruiter
        # bonus; B keeps its +10%. Before 2026-08-24 B lost the bonus for A's
        # behaviour, which is what forced the 4/8 threshold widening.
        sigs = self._shadow(self._journal(), "B", "A")
        refs = [_ref("A", "B", reg_block="valid")]
        meta = _meta("A", "B", "D")
        w = replay.weights_from_journal(sigs, meta, self.UIDS, NOW, referrals=refs)
        base = {}
        for hk in ("A", "B", "D"):
            dec = [(s["t0_unix"], s["status"] == "won", False)
                   for s in sigs if s["hotkey"] == hk and s["status"] != "washed"]
            graded = [(s["t0_unix"], s["status"] == "washed")
                      for s in sigs if s["hotkey"] == hk]
            base[hk] = scoring.decayed_qwin_tally(
                scoring.qualified_wins(dec, OLD, False, graded=graded), NOW)
        eff = {"A": base["A"],                        # recruiter bonus withheld
               "B": (1 + config.REFERRAL_RECRUIT_BONUS) * base["B"],
               "D": base["D"]}
        total = sum(eff.values())
        for hk, uid in self.UIDS.items():
            assert w[uid] == pytest.approx(eff[hk] / total)

    def test_suspension_moves_only_the_bonus(self, lit):
        # The penalty is a withheld bonus and nothing else. With the referral
        # dark, a shadowed journal and a clean one produce the same vector —
        # the shadow rows are washes, so they carry no tally of their own.
        meta = _meta("A", "B", "D")
        clean = self._journal()
        shadowed = self._shadow(self._journal(), "A", "B")
        assert (replay.weights_from_journal(shadowed, meta, self.UIDS, NOW)
                == replay.weights_from_journal(clean, meta, self.UIDS, NOW))
        # Lit, the copier B sits BELOW where it would with its bonus intact,
        # while the copied-from A sits above its own base share.
        refs = [_ref("A", "B", reg_block="valid")]
        w_sus = replay.weights_from_journal(shadowed, meta, self.UIDS, NOW,
                                            referrals=refs)
        w_clean = replay.weights_from_journal(clean, meta, self.UIDS, NOW,
                                              referrals=refs)
        assert w_sus[2] < w_clean[2]        # B forfeited its +10%
        assert w_sus[1] > w_clean[1]        # A's recruiter bonus survived intact
        assert w_sus[3] > w_clean[3]        # the freed share re-splits the pool

    def test_purity_referrals_input_untouched(self, lit):
        sigs = self._journal()
        refs = [_ref("A", "B", reg_block="valid")]
        snap = [dict(r) for r in refs]
        replay.weights_from_journal(sigs, _meta("A", "B", "D"), self.UIDS,
                                    NOW, referrals=refs)
        assert refs == snap
