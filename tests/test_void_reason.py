"""A void must say WHY, at the row a miner can be shown.

Before this, every HF void was indistinguishable from every other HF void at
every surface a miner can reach: the website charted the call as though it had
been scored, so a call that voided on the open-position gate while its own price
path ran to TP read as a grading bug. Hiig hit that three times in one hour on
2026-08-25 and it took a validator-database session to answer.
"""
from sn89_signals import hf_grade


def _db(tmp_path):
    return hf_grade._db(str(tmp_path))


def _put(db, hk, pair, t0, status, open_until, direction="LONG", reason=None):
    db.execute("INSERT OR REPLACE INTO grades "
               "(key, hk, t0_ms, pair, status, open_until_ms, direction, reason) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (f"{hk}:{t0}", hk, t0, pair, status, open_until, direction, reason))


class TestVoidReasonColumn:

    def test_column_exists(self, tmp_path):
        db = _db(tmp_path)
        assert "reason" in {r[1] for r in db.execute("PRAGMA table_info(grades)")}
        db.close()

    def test_stored_reason_is_returned_with_the_holder(self, tmp_path):
        db = _db(tmp_path)
        _put(db, "A", "ETHUSD", 1_000_000, "lost", 1_600_000)
        _put(db, "A", "ETHUSD", 1_300_000, "void", 1_300_000, "SHORT",
             "pair_open_same_mechanism:1:1600000")
        r = hf_grade.void_reason_for(db, "A", "ETHUSD", 1_300_000,
                                     "pair_open_same_mechanism:1:1600000")
        assert r["code"] == "pair_open_same_mechanism"
        assert r["free_at_ms"] == 1_600_000
        # The time alone is unactionable: name the call that held the pair.
        assert r["blocked_by"]["t0_ms"] == 1_000_000
        db.close()

    def test_legacy_null_reason_is_derived_not_dropped(self, tmp_path):
        """Rows graded before the column existed, and every row after a cache
        rebuild, carry NULL. They must still explain."""
        db = _db(tmp_path)
        _put(db, "A", "SOLUSD", 1_000_000, "won", 1_500_000)
        _put(db, "A", "SOLUSD", 1_200_000, "void", 1_200_000, "SHORT", None)
        r = hf_grade.void_reason_for(db, "A", "SOLUSD", 1_200_000, None)
        assert r["code"] == "pair_open_same_mechanism"
        assert r["blocked_by"]["t0_ms"] == 1_000_000
        db.close()

    def test_a_void_never_holds_the_pair_so_refusals_do_not_chain(self, tmp_path):
        """`open_until_ms` is t0 for a void. A voided predecessor must not appear
        as a holder, or the first refusal locks the pair for everything behind
        it."""
        db = _db(tmp_path)
        _put(db, "A", "BTCUSD", 1_000_000, "void", 1_000_000, "LONG",
             "pair_open_same_mechanism:1:1000000")
        r = hf_grade.void_reason_for(db, "A", "BTCUSD", 1_200_000, None)
        assert r["holders"] == []
        assert r["code"] == "no_entry_price"
        db.close()

    def test_other_hotkeys_and_pairs_never_hold_your_pair(self, tmp_path):
        db = _db(tmp_path)
        _put(db, "B", "ETHUSD", 1_000_000, "won", 1_900_000)      # someone else
        _put(db, "A", "BTCUSD", 1_000_000, "won", 1_900_000)      # another pair
        r = hf_grade.void_reason_for(db, "A", "ETHUSD", 1_200_000, None)
        assert r["holders"] == []
        db.close()
