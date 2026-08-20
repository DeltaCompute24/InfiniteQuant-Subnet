"""FX/metals calls whose grade window is a closed market must VOID, not wash.

A wash is supposed to say "the move was too small to matter". Three GBPUSD calls
committed at 20:59 UTC on Friday 2026-08-07 -- inside the last minute of the FX
week -- ran a 12h window across a frozen tape and graded WASHED with an MFE of
0.0 bps. Nothing about that describes the trader.

These lock: the session calendar (which must not depend on the host's tzdata),
the void itself, that it consumes no quota, and that it is forward-only.
"""
from datetime import datetime, timezone

import pytest

from sn89_signals import config, scoring, sessions


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# 2026-08-07 20:59:07Z — the real commit, 53 seconds inside the FX week.
T_FRI_2059 = _ts("2026-08-07T20:59:07Z")

# Post-cutover fixtures. Friday 2026-08-14 20:59Z is the same weekday and NY hour
# as the defect; Sunday 2026-08-16 sits either side of a 21:00Z reopen.
T_ARMED_FRI_2059 = _ts("2026-08-14T20:59:07Z")
T_ARMED_SUN_DEAD = _ts("2026-08-16T00:00:00Z")
T_ARMED_SUN_OPEN = _ts("2026-08-16T22:00:00Z")
T_ARMED_MIDWEEK = _ts("2026-08-12T09:00:00Z")

# The forex pair the post-cutover fixtures fire on. It is a NAME and not a literal
# because the board narrows over time and these tests are about the session calendar,
# not about any one pair: the fxmacro3-20260813 narrowing dropped GBPUSD and both armed
# tests below started failing on a void reason that had nothing to do with the calendar.
# The historical rows keep GBPUSD — it was on the board on 2026-08-07 and rewriting a
# fixture that reproduces a real incident would defeat the point of it.
#
# PAIR_LIVE has now stranded TWICE: GBPUSD left on the fxmacro3-20260813 narrowing, and
# USDCHF left on fxpoll3-20260821 when the board was rebuilt from the miner poll. Pick a
# pair the board is least likely to drop — AUDUSD survived both, and it is the only FX
# pair that also holds an HF listing, so dropping it would be a much larger decision
# than a re-band. The guard below is what turns the next stranding into a named failure
# instead of a mysterious void reason.
PAIR_LIVE = "AUDUSD"
PAIR_HISTORICAL = "GBPUSD"


def test_fixtures_are_the_right_side_of_the_cutover():
    assert T_FRI_2059 < config.FX_DEAD_HORIZON_FROM
    for t in (T_ARMED_FRI_2059, T_ARMED_SUN_DEAD, T_ARMED_SUN_OPEN, T_ARMED_MIDWEEK):
        assert t >= config.FX_DEAD_HORIZON_FROM


def test_fixture_pairs_are_on_the_board_when_they_fire():
    """Guards the stranding above: a fixture pair that has left the board voids for
    the wrong reason, and the failure names a calendar bug that isn't there."""
    for t in (T_ARMED_FRI_2059, T_ARMED_SUN_DEAD, T_ARMED_SUN_OPEN, T_ARMED_MIDWEEK):
        board = config.bands_as_of(t)
        assert PAIR_LIVE in board, f"{PAIR_LIVE} left the board before {t}"
        assert board[PAIR_LIVE]["asset_class"] == "forex"
    assert PAIR_HISTORICAL in config.bands_as_of(T_FRI_2059)


# ── the calendar ─────────────────────────────────────────────────────────────
def test_calendar_matches_tzdata_across_a_full_year():
    """The arithmetic DST rule must agree with IANA, or two validators on
    different tzdata patch levels disagree about a grade."""
    ZoneInfo = pytest.importorskip("zoneinfo").ZoneInfo
    ny = ZoneInfo("America/New_York")
    t, end = _ts("2026-01-01T00:00:00Z"), _ts("2027-01-01T00:00:00Z")
    while t < end:
        ref = int(datetime.fromtimestamp(t, tz=ny).utcoffset().total_seconds() // 3600)
        assert sessions.ny_utc_offset_h(t) == ref, t
        t += 3600


def test_week_boundaries_are_ny_anchored_not_utc_anchored():
    # Summer: Friday close is 21:00 UTC (17:00 EDT).
    assert not sessions.fx_market_closed(_ts("2026-08-07T20:59:00Z"))
    assert sessions.fx_market_closed(_ts("2026-08-07T21:00:00Z"))
    # Winter: the same close is 22:00 UTC (17:00 EST). A UTC-anchored rule would
    # get one of these two wrong.
    assert not sessions.fx_market_closed(_ts("2026-12-04T21:59:00Z"))
    assert sessions.fx_market_closed(_ts("2026-12-04T22:00:00Z"))
    # Saturday is shut end to end; Sunday reopens at 17:00 NY.
    assert sessions.fx_market_closed(_ts("2026-08-08T12:00:00Z"))
    assert sessions.fx_market_closed(_ts("2026-08-09T20:59:00Z"))
    assert not sessions.fx_market_closed(_ts("2026-08-09T21:00:00Z"))


def test_open_fraction_bounds():
    assert sessions.open_fraction(T_FRI_2059, 12) < 0.01      # the real defect
    assert sessions.open_fraction(T_ARMED_MIDWEEK, 12) == 1.0
    assert sessions.open_fraction(T_ARMED_SUN_DEAD, 12) == 0.0


# ── the void ─────────────────────────────────────────────────────────────────
def _row(pair, t0, hk="hkA"):
    return scoring.GradedRow(hotkey=hk, trade_pair=pair, direction="LONG",
                             t0_unix=t0, status="ok")


def test_dead_horizon_voids_after_the_cutover():
    (out,) = scoring.apply_validity_filters([_row(PAIR_LIVE, T_ARMED_FRI_2059)])
    assert out.status == "void"
    assert out.void_reason == "fx_dead_horizon"


def test_a_live_session_call_is_untouched():
    (out,) = scoring.apply_validity_filters([_row(PAIR_LIVE, T_ARMED_MIDWEEK)])
    assert out.status == "ok" and out.void_reason is None


def test_crypto_never_consults_the_calendar():
    """BTC trades through the weekend, so no horizon of its can be dead."""
    (out,) = scoring.apply_validity_filters([_row("BTCUSD", T_ARMED_FRI_2059)])
    assert out.status == "ok"


def test_forward_only_never_regrades_history():
    """The three real 2026-08-07 calls predate the cutover and must be left
    exactly as they graded — arming a rule cannot rewrite a settled journal."""
    # Distinct hotkeys, as the real three were — one hotkey firing three commits
    # a second apart would void on min_spacing for unrelated reasons.
    rows = [_row(PAIR_HISTORICAL, T_FRI_2059 + i, hk=f"hk{i}") for i in range(3)]
    out = scoring.apply_validity_filters(rows)
    assert all(r.status == "ok" for r in out)


def test_dead_horizon_consumes_no_daily_quota():
    """The trader did nothing wrong, so a dead call must not spend their
    allowance. Fill the whole cap with dead Sunday-morning calls, then submit a
    live one after the 21:00Z reopen — same UTC day, so the quota is shared."""
    cap, gap = config.submission_rules_as_of(T_ARMED_SUN_DEAD)
    step = max(gap, 3600)
    dead = [_row(PAIR_LIVE, T_ARMED_SUN_DEAD + i * step) for i in range(cap)]
    live = _row(PAIR_LIVE, T_ARMED_SUN_OPEN)
    assert int(live.t0_unix // 86400) == int(dead[0].t0_unix // 86400)

    out = scoring.apply_validity_filters(dead + [live])
    assert all(r.void_reason == "fx_dead_horizon" for r in out if r.status == "void")
    survivor = [r for r in out if r.t0_unix == live.t0_unix][0]
    assert survivor.status == "ok", "dead calls ate the daily quota"
