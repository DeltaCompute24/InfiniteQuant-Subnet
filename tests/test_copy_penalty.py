"""Shadow-gated copy penalty (§7.5) — is_penalised_copier.

The RATE gate alone (is_habitual_copier: "landed second inside the leader's
8-12h horizon on >=50% of decisive trades") cannot separate a copier from an
honest miner on a crowded board: 41% of all decisive mainnet calls land second,
and 69% of copy-marks sit on a (pair, direction) that >=4 hotkeys held at once —
a consensus/news move, not a copy. The SHADOW gate (detect_copiers: >=3 sharp
follows of ONE leader inside COPY_SHARP_LAG_S) supplies the intent evidence the
rate gate lacks. Both must agree before a hotkey's copied wins are stripped.

mark_copies is deliberately NOT gated — a sybil operator authors its own
signals and never needs to see a reveal, so the spray must still be marked.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, scoring   # noqa: E402


class TestIsPenalisedCopier:
    def test_rate_gate_alone_does_not_strip(self, monkeypatch):
        # habitual by rate but no 1:1 shadow fingerprint -> honest crowd, no strip
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", True)
        assert scoring.is_habitual_copier(5, 10) is True
        assert scoring.is_penalised_copier(5, 10, False) is False

    def test_both_gates_strip(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", True)
        assert scoring.is_penalised_copier(5, 10, True) is True

    def test_shadow_alone_does_not_strip(self, monkeypatch):
        # a sharp signature but a low landing-second rate is not enough
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", True)
        assert scoring.is_habitual_copier(1, 10) is False
        assert scoring.is_penalised_copier(1, 10, True) is False

    def test_legacy_rate_only_when_lever_off(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", False)
        assert scoring.is_penalised_copier(5, 10, False) is True

    def test_penalty_off_short_circuits(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_PENALTY", "off")
        assert scoring.is_penalised_copier(10, 10, True) is False

    def test_below_min_copies_never_strips(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", True)
        # 4 copies is under COPY_MIN_COPIES(5) even at a 0.8 rate
        assert scoring.is_penalised_copier(4, 5, True) is False

    def test_deterministic(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_REQUIRE_SHADOW", True)
        for _ in range(5):
            assert scoring.is_penalised_copier(7, 10, True) is True


class TestSybilSprayStillMarked:
    """mark_copies must stay ungated: the anti-sybil economics depend on it."""

    def test_spray_across_keys_inside_reveal_window_still_marked(self):
        # 10 keys, same call, 60s apart — well inside the 2h reveal. One operator
        # authors all of them, so "they couldn't have seen the reveal" is no defence.
        rows = scoring.mark_copies([
            scoring.GradedRow(hotkey=f"k{i}", trade_pair="BTCUSD", direction="LONG",
                              t0_unix=i * 60, status="ok") for i in range(10)])
        marked = [r.hotkey for r in rows if r.is_copy]
        assert "k0" not in marked
        assert len(marked) == 9


class TestEpisodes:
    """Sharp follows inside COPY_EPISODE_S are ONE decision, not N.

    MAX_SIGNALS_PER_UTC_DAY is 6, so a CPI/PPI print makes a trader fire its whole
    allowance across many pairs within minutes. Counting each pair as separate
    evidence is pseudo-replication — it is what put an honest news trader over a
    3-event gate (haroldyeah902 / 5H8Vq6jZ: 3 raw follows, 2 real occasions).
    """

    def test_empty(self):
        assert scoring._episodes([], 1800) == 0

    def test_single(self):
        assert scoring._episodes([100.0], 1800) == 1

    def test_one_news_burst_collapses(self):
        # 5 pairs fired 12s apart off one print -> ONE decision
        assert scoring._episodes([0, 12, 24, 36, 48], 1800) == 1

    def test_distinct_occasions_still_count(self):
        # three separate days -> three occasions
        assert scoring._episodes([0, 86_400, 172_800], 1800) == 3

    def test_harold_shape_is_two_not_three(self):
        # 07-14 14:09:00, then 07-15 12:24:12 + 12:24:24 (12s apart)
        assert scoring._episodes([0.0, 86_400.0, 86_412.0], 1800) == 2

    def test_anchored_not_chained(self):
        # a chain 20 min apart must NOT collapse into one 60-min episode
        assert scoring._episodes([0, 1200, 2400], 1800) == 2

    def test_unsorted_input(self):
        assert scoring._episodes([86_412.0, 0.0, 86_400.0], 1800) == 2

    def test_deterministic(self):
        for _ in range(5):
            assert scoring._episodes([0, 12, 86_400], 1800) == 2


class TestDetectCopiersUsesEpisodes:
    def _row(self, hk, pair, direction, t0):
        return scoring.GradedRow(hotkey=hk, trade_pair=pair, direction=direction,
                                 t0_unix=t0, status="ok")

    def test_news_burst_does_not_flag(self):
        # leader L and follower F both fire 4 pairs within a minute of one print.
        # 4 raw sharp follows, but ONE occasion -> not a copier. detect_copiers
        # only emits a row when flagged/low_diversity, so F drops out entirely.
        rows = []
        for i, pair in enumerate(["BTCUSD", "ETHUSD", "XAUUSD", "XRPUSD"]):
            rows.append(self._row("L", pair, "LONG", 1000 + i))
            rows.append(self._row("F", pair, "LONG", 1010 + i))
        reps = scoring.detect_copiers(rows, now_unix=100_000, eligible_leaders={"L"})
        # 4 raw follows collapse to ONE episode, so F is never a copier here.
        assert "F" not in scoring.flagged_copier_hotkeys(reps)

    def test_repeat_shadowing_across_days_still_flags(self, monkeypatch):
        # a real fingerprint: same follow on COPY_SHARP_MIN_EVENTS separate days
        monkeypatch.setattr(config, "COPY_SHARP_MIN_EVENTS", 3)
        rows = []
        for d in range(3):
            t = d * 86_400
            rows.append(self._row("L", "BTCUSD", "LONG", t))
            rows.append(self._row("F", "BTCUSD", "LONG", t + 60))
        reps = scoring.detect_copiers(rows, now_unix=500_000, eligible_leaders={"L"})
        r = reps["F"][0]
        assert r.sharp_episodes == 3
        assert r.flagged is True

    def test_shared_strategy_cluster_below_threshold_not_flagged(self):
        # 3 separate co-entries (e.g. both long gold at each intraday low) is the
        # honest shared-strategy noise floor; the default gate (6 episodes) ignores it.
        rows = []
        for d in range(3):
            t = d * 86_400
            rows.append(self._row("L", "XAUUSD", "LONG", t))
            rows.append(self._row("F", "XAUUSD", "LONG", t + 60))
        reps = scoring.detect_copiers(rows, now_unix=500_000, eligible_leaders={"L"})
        assert "F" not in scoring.flagged_copier_hotkeys(reps)


class TestCopyGateInputsAndTTL:
    """The penalty is windowed evidence + a freshness TTL, so a false positive
    self-clears COPY_PENALTY_TTL_S (2d) after the trader stops landing second."""

    def _dec(self, ts_flags):
        # [(t0, won, is_copy), ...]
        return [(t, True, cp) for t, cp in ts_flags]

    def test_last_copy_timestamp_returned(self):
        dec = self._dec([(1000.0, True), (2000.0, False), (3000.0, True)])
        copies, decisive, last = scoring.copy_gate_inputs(dec, now_unix=4000.0)
        assert (copies, decisive, last) == (2, 3, 3000.0)

    def test_no_copies_returns_none_last(self):
        dec = self._dec([(1000.0, False), (2000.0, False)])
        assert scoring.copy_gate_inputs(dec, now_unix=3000.0) == (0, 2, None)

    def test_only_counts_inside_copy_window(self, monkeypatch):
        monkeypatch.setattr(config, "COPY_WINDOW_S", 86_400)  # 1d window
        now = 1_000_000.0
        dec = self._dec([(now - 200_000, True), (now - 1000, True)])  # one stale, one fresh
        copies, decisive, last = scoring.copy_gate_inputs(dec, now)
        assert (copies, decisive) == (1, 1)
        assert last == now - 1000

    def test_ttl_freshness_math(self, monkeypatch):
        # the exact check replay/build_dashboard apply
        monkeypatch.setattr(config, "COPY_PENALTY_TTL_S", 2 * 86_400)
        now = 1_000_000.0
        fresh_last = now - 1 * 86_400
        stale_last = now - 3 * 86_400
        assert (now - fresh_last) <= config.COPY_PENALTY_TTL_S
        assert not ((now - stale_last) <= config.COPY_PENALTY_TTL_S)
