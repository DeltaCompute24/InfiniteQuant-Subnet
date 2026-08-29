"""The decisive-row tuple layout is load-bearing and positional.

`scoring._resolved_by` reads `row[3] if len(row) > 3` as the journaled resolution
time. Anything else placed at index 3 is silently read as a unix timestamp: a
19.0 bps band becomes "resolved 19 seconds after the epoch", every prior row
looks already-resolved, and the causal qualify window reverts to exactly the
behaviour CAUSAL_QWIN_FROM was introduced to fix -- with no error anywhere.

Caught during the signed-points work, having written the band into index 3 and
watched all 637 tests still pass.
"""
from sn89_signals import scoring


class TestResolvedUnixKeepsItsSlot:
    def test_index_3_is_read_as_resolution_time(self):
        assert scoring._resolved_by((100.0, True, False, 50.0), 60.0) is True
        assert scoring._resolved_by((100.0, True, False, 90.0), 60.0) is False

    def test_absent_index_3_is_legacy_treatment(self):
        assert scoring._resolved_by((100.0, True, False), 60.0) is True

    def test_none_at_index_3_is_legacy_treatment(self):
        # HF has no journaled resolution time and passes None here on purpose.
        assert scoring._resolved_by((100.0, True, False, None), 60.0) is True

    def test_a_band_at_index_3_would_be_misread(self):
        # The bug this file exists to prevent, asserted as a fact about the API
        # rather than a hope: a plausible band value at index 3 reads as resolved
        # for every conceivable t0.
        assert scoring._resolved_by((1.7e9, True, False, 19.0), 1.7e9) is True

    def test_extra_elements_past_3_are_ignored_here(self):
        # The band lives at 4 and 5. _resolved_by must not care.
        assert scoring._resolved_by((100.0, True, False, None, 19.0, 1800), 60.0) is True
        assert scoring._resolved_by((100.0, True, False, 50.0, 19.0, 1800), 60.0) is True


class TestQualifiedWinsToleratesTheWiderRow:
    def test_six_element_rows_still_score(self):
        rows = [(1000.0 + i, i % 2 == 0, False, None, 19.0, 1800) for i in range(10)]
        out = scoring.qualified_wins(rows, first_seen_unix=0.0)
        assert isinstance(out, list)
        for t0, w in out:
            assert w >= 1.0
