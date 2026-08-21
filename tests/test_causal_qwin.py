# -*- coding: utf-8 -*-
"""A banked qualified win survives a loss that OPENED before it and GRADED after.

REGRESSION. qualified_wins re-derives every win's gate verdict each cycle from a
t0-sorted list. A loss whose t0 precedes a banked win but whose grade lands after
it used to be inserted BEHIND that win, flipping its verdict and erasing a win
that was already banked and paying — the emission cliff the 7-day linear decay
exists to prevent, and the opposite of compute_weights' stated invariant.

Reproduced from 5Ct3Nn on 2026-08-20: SOLUSD won (t0 06:55, graded 08:15) banked
at 9W/4L; XAGUSD lost (t0 02:09 — 4h46m EARLIER — graded 12:11) and the tally
went 0.81 -> 0.00 in one cycle.

Every test pins the CAUSAL era via SN89_CAUSAL_QWIN_FROM, so it does not silently
change meaning when the arming stamp moves.
"""
import importlib
import os

import pytest

H = 3600.0


@pytest.fixture
def sc(monkeypatch):
    """scoring with the causal window armed from t=0 (whole fixture is causal-era)."""
    monkeypatch.setenv("SN89_CAUSAL_QWIN_FROM", "1")
    from sn89_signals import config as cfg
    importlib.reload(cfg)
    from sn89_signals import scoring
    importlib.reload(scoring)
    yield scoring
    monkeypatch.delenv("SN89_CAUSAL_QWIN_FROM", raising=False)
    importlib.reload(cfg)
    importlib.reload(scoring)


def _hist(base, n_win, n_loss):
    """n_win wins then n_loss losses, all opened AND resolved before `base`."""
    out = []
    t = base - 500 * H
    for i in range(n_win + n_loss):
        out.append((t, i < n_win, False, t + H))     # resolves 1h after it opens
        t += 2 * H
    return out


def test_late_grading_earlier_loss_cannot_unbank_a_win(sc):
    base = 1_800_000_000.0
    first_seen = base - 1000 * H            # long past warmup
    # 8W/4L of settled history: lb passes the gate at 12 decisive.
    hist = _hist(base, 8, 4)
    win = (base + 10 * H, True, False, base + 11 * H)          # opens T+10h
    # the loss OPENS at T+5h (before the win) but RESOLVES at T+20h (after it)
    late_loss = (base + 5 * H, False, False, base + 20 * H)

    banked = sc.qualified_wins(hist + [win], first_seen)
    assert win[0] in dict(banked), "control: the win must bank without the loss"

    after = sc.qualified_wins(hist + [win, late_loss], first_seen)
    assert win[0] in dict(after), (
        "a loss that resolved AFTER this win must not retroactively un-bank it")
    assert dict(after)[win[0]] == dict(banked)[win[0]], "banked value must not move"


def test_a_loss_that_had_already_resolved_does_count(sc):
    """The gate must still be a gate: a KNOWN loss can keep a win from banking."""
    base = 1_800_000_000.0
    first_seen = base - 1000 * H
    hist = _hist(base, 5, 5)                                   # 5W/5L, gate fails
    win = (base + 10 * H, True, False, base + 11 * H)
    assert win[0] not in dict(sc.qualified_wins(hist + [win], first_seen)), (
        "settled losses must still be able to hold a win out of the tally")


def test_missing_resolution_time_keeps_legacy_treatment(sc):
    """No exit_at_ms -> counted as known, matching scripts/audit_journal.py."""
    base = 1_800_000_000.0
    first_seen = base - 1000 * H
    hist = _hist(base, 8, 4)
    win = (base + 10 * H, True, False, base + 11 * H)
    no_ts_loss = (base + 5 * H, False, False)                  # 3-tuple, no time
    assert sc.qualified_wins(hist + [win], first_seen), "control: banks at 9W/4L"
    assert win[0] not in dict(sc.qualified_wins(hist + [win, no_ts_loss], first_seen)), (
        "a row without a resolution time must fall back to legacy inclusion")


def test_legacy_era_win_is_unchanged(monkeypatch):
    """A win placed BEFORE the arming stamp keeps the verdict it was given."""
    monkeypatch.setenv("SN89_CAUSAL_QWIN_FROM", "9999999999")
    from sn89_signals import config as cfg
    importlib.reload(cfg)
    from sn89_signals import scoring
    importlib.reload(scoring)
    try:
        base = 1_800_000_000.0
        first_seen = base - 1000 * H
        hist = _hist(base, 8, 4)
        win = (base + 10 * H, True, False, base + 11 * H)
        late_loss = (base + 5 * H, False, False, base + 20 * H)
        # legacy behaviour: the late-grading earlier loss DOES un-bank it
        assert win[0] not in dict(sc_qw(scoring, hist + [win, late_loss], first_seen))
    finally:
        monkeypatch.delenv("SN89_CAUSAL_QWIN_FROM", raising=False)
        importlib.reload(cfg)
        importlib.reload(scoring)


def sc_qw(scoring, rows, first_seen):
    return scoring.qualified_wins(rows, first_seen)
