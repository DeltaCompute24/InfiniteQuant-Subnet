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
