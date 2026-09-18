"""HF board v5 / LF cryptoalts21 -- the 2026-09-21 crypto-alt listing."""
from sn89_signals import config, hf

ALTS_1800 = {"BNBUSD", "DOGEUSD", "ADAUSD", "AVAXUSD", "LINKUSD", "TRXUSD", "LTCUSD",
             "SUIUSD", "NEARUSD", "UNIUSD", "AAVEUSD", "CRVUSD", "XMRUSD", "ZECUSD",
             "ENAUSD", "ZROUSD", "KPEPEUSD"}
ALTS_7200 = {"DOTUSD", "BCHUSD", "ARBUSD", "ALGOUSD"}
HELD = {"ASTERUSD", "WLDUSD", "PUMPUSD"}
ALTS = ALTS_1800 | ALTS_7200


class TestBoardV5:
    def test_not_callable_before_the_boundary(self):
        before = hf.hf_bands_as_of(hf.HF_V5_FROM - 1)
        assert before is hf.HF_BOARD_V4
        assert not (ALTS & set(before))

    def test_listed_at_the_boundary_with_the_right_clock(self):
        b = hf.hf_bands_as_of(hf.HF_V5_FROM)
        assert ALTS <= set(b)
        for p in ALTS_1800:
            assert b[p][2] == 1800 and b[p][3] == "crypto" and b[p][0] == b[p][1]
        for p in ALTS_7200:
            assert b[p][2] == 7200 and b[p][3] == "crypto" and b[p][0] == b[p][1]

    def test_existing_rows_are_untouched(self):
        for p, row in hf.HF_BOARD_V4.items():
            assert hf.HF_BOARD_V5[p] == row

    def test_held_pairs_are_not_listed(self):
        assert not (HELD & set(hf.HF_BOARD_V5))

    def test_every_alt_clears_the_spread_gate(self):
        for p in ALTS:
            r = hf.band_spread_ratio(p, hf.HF_V5_FROM)
            assert r is not None and r >= hf.MIN_BAND_SPREAD_RATIO, (p, r)

    def test_validate_accepts_after_and_refuses_before(self):
        import pytest
        pl = {"trade_pair": "DOGEUSD", "direction": "LONG", "asset_class": "crypto",
              "tp_bps": 20.0, "sl_bps": 20.0, "horizon_s": 1800}
        hf.validate_submission(dict(pl), hf.HF_V5_FROM + 60)
        with pytest.raises(hf.HFRejected):
            hf.validate_submission(dict(pl), hf.HF_V5_FROM - 60)


class TestLFEntry:
    def test_lf_matches_hf_listing_and_boundary(self):
        before = config.bands_as_of(hf.HF_V5_FROM - 1)
        after = config.bands_as_of(hf.HF_V5_FROM)
        assert not (ALTS & set(before))
        assert ALTS <= set(after)
        assert not (HELD & set(after))
        for p in ALTS:
            assert after[p]["asset_class"] == "crypto"
            assert after[p]["tp_bps"] == after[p]["sl_bps"] > 0
        for p, spec in before.items():
            assert after[p] == spec


class TestDiversityBreadth:
    PAIRS = ["BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD", "ADAUSD", "LINKUSD", "SUIUSD"]

    def test_pair_count_before_v5(self):
        assert hf.hf_diversity_breadth(self.PAIRS, hf.HF_V5_FROM - 1) == 7

    def test_crypto_counts_once_from_v5(self):
        assert hf.hf_diversity_breadth(self.PAIRS, hf.HF_V5_FROM) == 1
        assert hf.hf_diversity_breadth(self.PAIRS + ["XAUUSD", "AUDUSD"], hf.HF_V5_FROM) == 3
        assert hf.hf_diversity_breadth(["XAUUSD", "AUDUSD"], hf.HF_V5_FROM) == 2

    def test_delisted_pair_keeps_its_class(self):
        assert hf._hf_pair_class_any("EURUSD") == "forex"

    def test_seven_long_only_alts_no_longer_reach_the_broad_floor(self):
        now = hf.HF_V5_FROM + 86400
        subs = []
        for i in range(140):
            p = self.PAIRS[i % 7]
            d = "SHORT" if i % 25 == 0 else "LONG"      # 4% minority, inside pairs traded
            subs.append(((now - 3600 - i * 60) * 1000, p, d, 1800))
        v = hf.hf_diversity(subs, now)
        assert v["pairs"] == 7 and v["breadth"] == 1
        assert v["floor"] == hf.HF_DIVERSITY_FLOOR_NARROW
        assert v["applies"] and not v["ok"]
        legacy = hf.hf_diversity([(t - (now - hf.HF_V5_FROM + 86400) * 1000, p, d, h)
                                  for t, p, d, h in subs], hf.HF_V5_FROM - 1)
        assert legacy["breadth"] == 7 and legacy["ok"]
