"""Custom sizing: the band is a property of the CALL, not of the board.

Two invariants, and the first matters more than the second.

1. OFF BY DEFAULT ON EVERY NETWORK. The arming stamp is a timestamp, and a
   timestamp does not know which chain it is on. If this ever defaults to a real
   date, the day the code reaches finney it reclassifies every mainnet call after
   that instant as a custom-band call -- on a board that never had custom bands --
   and the grader re-derives different verdicts for calls already graded and paid.

2. A call with no declared band grades off the board, byte-identically to how it
   graded before the column existed. That is what makes the whole corpus replay.
"""
import sqlite3

import pytest

from sn89_signals import config, hf, hf_grade

PAIR = "EURUSD"
T0_UNIX = hf.HF_LAUNCH_FROM + 3600


def _board_row(pair=PAIR, t0=T0_UNIX):
    return hf.hf_bands_as_of(t0)[pair]


def _payload(**over):
    tp, sl, hz, cls = _board_row()
    p = {"trade_pair": PAIR, "direction": "LONG", "tp_bps": tp,
         "sl_bps": sl, "horizon_s": hz, "asset_class": cls}
    p.update(over)
    return p


class TestArmingIsOffByDefault:
    def test_stamp_is_zero_in_source(self):
        assert config.HF_CUSTOM_BANDS_FROM == 0, (
            "custom bands must ship disarmed; arm it per-network from the env")

    def test_never_enforced_at_any_time_when_disarmed(self):
        for t in (0, hf.HF_LAUNCH_FROM, 2_000_000_000, 4_000_000_000):
            assert config.custom_bands_enforced_as_of(t) is False


class TestFixedBoardUnchangedWhenDisarmed:
    def test_board_band_still_accepted(self):
        hf.validate_submission(_payload(), T0_UNIX)

    def test_off_board_band_still_refused(self):
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(_payload(tp_bps=999.0, sl_bps=999.0), T0_UNIX)
        assert "band_mismatch" in str(e.value)

    def test_off_board_horizon_still_refused(self):
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(_payload(horizon_s=600), T0_UNIX)
        assert "horizon_mismatch" in str(e.value)


class TestEnvelopeWhenArmed:
    """Behaviour once a network arms it. Patched, never armed in source."""

    @pytest.fixture(autouse=True)
    def _armed(self, monkeypatch):
        monkeypatch.setattr(config, "HF_CUSTOM_BANDS_FROM", int(hf.HF_LAUNCH_FROM))

    def test_a_declared_band_is_accepted(self):
        hf.validate_submission(_payload(tp_bps=40.0, sl_bps=40.0, horizon_s=1800), T0_UNIX)

    def test_asymmetric_band_refused(self):
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(_payload(tp_bps=40.0, sl_bps=20.0), T0_UNIX)
        assert "band_not_symmetric" in str(e.value)

    def test_no_spread_floor_on_a_drawn_band(self):
        # Dropped 2026-09-09: the band is the miner's claim. A band under the
        # old 8x-spread floor is accepted and priced by the symmetric payout.
        hf.validate_submission(_payload(tp_bps=1.0, sl_bps=1.0), T0_UNIX)

    def test_horizon_outside_the_envelope_refused(self):
        for hz in (60, 60 * 60 * 96):
            with pytest.raises(hf.HFRejected) as e:
                hf.validate_submission(_payload(horizon_s=hz), T0_UNIX)
            assert "horizon_out_of_range" in str(e.value)

    def test_universe_pair_off_the_board_is_accepted(self):
        # The bench has no board: any pair the network records is callable.
        pair = next(p for p in hf.HF_CUSTOM_UNIVERSE_V1
                    if p not in hf.hf_bands_as_of(T0_UNIX))
        cls, sigma = hf.HF_CUSTOM_UNIVERSE_V1[pair]
        hf.validate_submission(
            _payload(trade_pair=pair, asset_class=cls,
                     tp_bps=10.0, sl_bps=10.0, horizon_s=1800), T0_UNIX)
        assert hf.hf_asset_class_as_of(pair, T0_UNIX) == cls
        assert hf._board_sigma_for(pair, T0_UNIX) == pytest.approx(sigma)

    def test_universe_pair_needs_its_class(self):
        pair = next(p for p in hf.HF_CUSTOM_UNIVERSE_V1
                    if p not in hf.hf_bands_as_of(T0_UNIX))
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(
                _payload(trade_pair=pair, asset_class="equities",
                         tp_bps=10.0, sl_bps=10.0, horizon_s=1800), T0_UNIX)
        assert "asset_class_mismatch" in str(e.value)

    def test_unknown_pair_still_refused(self):
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(
                _payload(trade_pair="ZZZUSD", asset_class="crypto",
                         tp_bps=10.0, sl_bps=10.0, horizon_s=1800), T0_UNIX)
        assert "pair_not_on_hf_board" in str(e.value)

    def test_board_pair_keeps_its_board_sigma(self):
        from sn89_signals import scoring
        tp, _sl, hz, _ = _board_row()
        assert hf._board_sigma_for(PAIR, T0_UNIX) == pytest.approx(
            scoring.sigma_from_board(tp, hz))


class TestUniverseInvisibleWhenDisarmed:
    def test_universe_pair_refused_on_an_unarmed_network(self):
        pair = next(p for p in hf.HF_CUSTOM_UNIVERSE_V1
                    if p not in hf.hf_bands_as_of(T0_UNIX))
        assert hf.hf_custom_universe_as_of(T0_UNIX) == {}
        assert hf.hf_callable_as_of(pair, T0_UNIX) is False
        assert hf._board_sigma_for(pair, T0_UNIX) == 0.0
        with pytest.raises(hf.HFRejected) as e:
            hf.validate_submission(
                _payload(trade_pair=pair, asset_class="crypto",
                         tp_bps=10.0, sl_bps=10.0, horizon_s=1800), T0_UNIX)
        assert "pair_not_on_hf_board" in str(e.value)

    def test_every_universe_entry_is_well_formed(self):
        for p, (cls, sigma) in hf.HF_CUSTOM_UNIVERSE_V1.items():
            assert cls in hf.GRID_MS_BY_CLASS, p
            assert 0 < sigma < 10, p


class TestPendingCarriesTheBand:
    def test_null_band_falls_back_to_the_board(self, tmp_path):
        """A row written before the column existed must grade off the board."""
        db = sqlite3.connect(":memory:")
        hf_grade._ensure_schema(db) if hasattr(hf_grade, "_ensure_schema") else None
        # schema mirrors production, including the added columns
        db.execute("CREATE TABLE pending (key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, "
                   "pair TEXT, direction TEXT, end_ms INTEGER, tp_bps REAL, "
                   "sl_bps REAL, horizon_s INTEGER)")
        db.execute("INSERT INTO pending (key, hk, t0_ms, pair, direction, end_ms) "
                   "VALUES ('k','hk',1,?,'LONG',2)", (PAIR,))
        row = db.execute(
            "SELECT key, hk, t0_ms, pair, direction, end_ms, tp_bps, sl_bps, horizon_s "
            "FROM pending").fetchone()
        assert row[6] is None and row[7] is None and row[8] is None, (
            "a legacy row must read NULL so the board fallback engages")

    def test_declared_band_round_trips(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE pending (key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, "
                   "pair TEXT, direction TEXT, end_ms INTEGER, tp_bps REAL, "
                   "sl_bps REAL, horizon_s INTEGER)")
        db.execute("INSERT INTO pending (key, hk, t0_ms, pair, direction, end_ms, "
                   "tp_bps, sl_bps, horizon_s) VALUES ('k','hk',1,?,'LONG',2,?,?,?)",
                   (PAIR, 40.0, 40.0, 1800))
        row = db.execute(
            "SELECT key, hk, t0_ms, pair, direction, end_ms, tp_bps, sl_bps, horizon_s "
            "FROM pending").fetchone()
        assert (row[6], row[7], row[8]) == (40.0, 40.0, 1800)
