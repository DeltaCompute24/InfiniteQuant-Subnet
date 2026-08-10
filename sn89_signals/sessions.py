"""FX / metals trading-session calendar (CONSENSUS).

The retail FX week runs Sun 17:00 -> Fri 17:00 America/New_York. Metals spot
keeps the same hours. Crypto trades continuously and never consults this module.

DETERMINISM: this is a scoring-path calendar, so the US daylight-saving boundary
is computed ARITHMETICALLY rather than read from tzdata. `zoneinfo` resolves
against whatever IANA database the host happens to carry, which makes a validator's
OS patch level able to move a grading boundary -- the same class of hazard the
scoring module avoids by refusing numpy/erf. The rule encoded here is the Energy
Policy Act of 2005 schedule, in force for every year this subnet has graded:

    DST starts  second Sunday in March,   02:00 local standard  = 07:00 UTC
    DST ends    first  Sunday in November, 02:00 local daylight = 06:00 UTC

so New York is UTC-4 inside that span and UTC-5 outside it. If Congress ever
changes the schedule this file changes with an as-of cutover, exactly like the
bands and the hit rule -- never retroactively.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# New York local hour at which the FX week opens (Sunday) and closes (Friday).
_FX_WEEK_BOUNDARY_NY_HOUR = 17

# Asset classes that observe the FX session calendar. Crypto is absent on
# purpose: it has no close, so no dead horizon can exist.
SESSION_BOUND_CLASSES = ("forex", "forex-commodities")


def _nth_sunday(year: int, month: int, n: int) -> int:
    """Day-of-month of the n-th Sunday (1-based) in the given month."""
    first = date(year, month, 1)
    # date.weekday(): Mon=0 .. Sun=6
    first_sunday = 1 + (6 - first.weekday()) % 7
    return first_sunday + 7 * (n - 1)


def _dst_span_utc(year: int) -> tuple[float, float]:
    """[start, end) of US daylight saving for `year`, as UTC epoch seconds."""
    start = datetime(year, 3, _nth_sunday(year, 3, 2), 7, 0,
                     tzinfo=timezone.utc).timestamp()
    end = datetime(year, 11, _nth_sunday(year, 11, 1), 6, 0,
                   tzinfo=timezone.utc).timestamp()
    return start, end


def ny_utc_offset_h(ts: float) -> int:
    """New York's UTC offset in hours at `ts` -- -4 (EDT) or -5 (EST)."""
    year = datetime.fromtimestamp(ts, tz=timezone.utc).year
    start, end = _dst_span_utc(year)
    return -4 if start <= ts < end else -5


def fx_market_closed(ts: float) -> bool:
    """True when FX/metals spot is shut for the week at `ts`."""
    ny = datetime.fromtimestamp(ts + ny_utc_offset_h(ts) * 3600, tz=timezone.utc)
    wd, hour = ny.weekday(), ny.hour     # Mon=0 .. Sun=6
    if wd == 5:                                             # Saturday
        return True
    if wd == 4 and hour >= _FX_WEEK_BOUNDARY_NY_HOUR:       # Friday close
        return True
    if wd == 6 and hour < _FX_WEEK_BOUNDARY_NY_HOUR:        # Sunday pre-open
        return True
    return False


def open_seconds(t0_unix: float, horizon_h: float) -> float:
    """Seconds of OPEN FX market inside [t0, t0 + horizon_h).

    Sampled on a 60-second grid, which is the resolution the grader itself reads
    (1-minute aggregates / anchored ticks) -- a finer grid could not change a
    grade, and a coarse fixed grid keeps this a pure function of its arguments
    on every validator. The final partial minute is counted at its true length so
    the total is exact for horizons that are not whole minutes.
    """
    if horizon_h <= 0:
        return 0.0
    end = t0_unix + horizon_h * 3600.0
    total = 0.0
    t = float(t0_unix)
    while t < end:
        step = min(60.0, end - t)
        if not fx_market_closed(t):
            total += step
        t += 60.0
    return total


def open_fraction(t0_unix: float, horizon_h: float) -> float:
    """Share of the horizon that falls in an open FX session, in [0, 1]."""
    if horizon_h <= 0:
        return 0.0
    return open_seconds(t0_unix, horizon_h) / (horizon_h * 3600.0)
