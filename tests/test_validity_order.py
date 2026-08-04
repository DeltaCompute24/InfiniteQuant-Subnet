"""apply_validity_filters SORTS its input — callers must never pair the result
back to their own rows by POSITION.

`grade_revealed` did exactly that (`zip(rows, filtered)`) and so wrote each void
verdict onto a different commit. That is how Israel's #5147 (EURUSD,
2026-08-04 03:57) was voided `pair_locked_other_mechanism` when his hotkey had
no lock row of any kind — a verdict computed for another call landed on his.

These lock the contract: the function reorders, and it stamps the verdict onto
the caller's own row objects, so callers match by identity.
"""
import pytest

from sn89_signals import config, scoring


def _row(hk, pair, t0):
    return scoring.GradedRow(hotkey=hk, trade_pair=pair, direction="LONG",
                             t0_unix=t0, status="ok", horizon_h=12)


def test_result_is_reordered_so_position_pairing_is_unsafe():
    late = _row("hkA", "EURUSD", 2_000_000)
    early = _row("hkB", "GBPUSD", 1_000_000)
    out = scoring.apply_validity_filters([late, early])
    assert [r.t0_unix for r in out] == [1_000_000, 2_000_000]


def test_verdict_lands_on_the_callers_own_object(monkeypatch):
    """Two commits from one hotkey inside the min-gap: the later one voids.

    Passed deliberately unsorted, so a position-paired caller would blame the
    wrong row — which is the defect this guards.
    """
    T0 = 1_785_800_000        # 2026-08-04, the era this defect was found in
    cap, gap = config.submission_rules_as_of(T0)
    if not gap:
        pytest.skip("no min-gap rule in force at this t0")
    first = _row("hkA", "EURUSD", T0)
    second = _row("hkA", "GBPUSD", T0 + max(1, gap // 2))
    scoring.apply_validity_filters([second, first])   # unsorted on purpose
    assert first.status != "void"
    assert second.status == "void"
    assert second.void_reason == "min_spacing"
