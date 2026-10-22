"""V3.3 depth by collateral tier and V3.4 sigma calibration (Whit, 2026-10-13)."""
from decimal import Decimal as D

from sn89_signals import config, markets

E = "5EntityTierTestHotkeyxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _db(tmp_path, alpha=None, hwm=None, rcum=None, t_us=0):
    db = markets._db(str(tmp_path))
    if alpha is not None:
        db.execute("INSERT INTO vault_ledger VALUES (?,?,?,?,?,?,?)", ("k1", t_us, E, str(alpha), str(alpha), "deposit", "1:1"))
    if hwm is not None:
        db.execute("INSERT INTO settle (cycle, entity, won, lost, burned, deducted, debt, payable, lcum, bcum, dcum, "
                   "won_ret, lost_ret, rcum, hwm, lock_breach) VALUES (?,?,0,0,0,0,0,0,0,0,0,0,0,?,?,0)",
                   (0, E, str(rcum), str(hwm)))
    return db


def test_tiers_by_vault_collateral(tmp_path, monkeypatch):
    t = 1_900_000_000 * 1_000_000
    for alpha, want in ((12_000, None), (7_000, (D(625), D(2500))), (4_000, (D(250), D(1000))),
                        (500, (D(100), D(400)))):
        db = _db(tmp_path / str(alpha), alpha)
        assert markets.depth_limits(db, E, t) == want


def test_the_lock_comes_off_the_tier_and_only_deposits_before_the_hour_count(tmp_path, monkeypatch):
    t = 1_900_000_000 * 1_000_000
    db = _db(tmp_path / "a", 11_000, hwm=2_000, rcum=0)          # 11,000 - 2,000 locked = 9,000: second tier
    assert markets.depth_limits(db, E, t) == (D(625), D(2500))
    hour_us = t // 3_600_000_000 * 3_600_000_000
    db = _db(tmp_path / "b", 12_000, t_us=hour_us + 1)          # deposited inside the bet's hour: not yet counted
    assert markets.depth_limits(db, E, t + 2) == (D(100), D(400))


def test_sigma_is_scaled_from_the_arm_by_window(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_FROM_UNIX", 1_800_000_000)
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V2_FROM_UNIX", 0)
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V3_FROM_UNIX", 0)
    s = D("0.0001")
    after, before = 1_800_000_000 // 3600 * 3600 + 3600, 1_800_000_000 // 3600 * 3600 - 3600
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "15m", after), s) == D("0.000130")
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1h", after), s) == D("0.000120")
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "15m", before), s) == s
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_FROM_UNIX", 0)
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "15m", after), s) == s


def test_sigma_v2_arm_rescales_1h_only_from_its_start(monkeypatch):
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_FROM_UNIX", 1_700_000_000)
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V2_FROM_UNIX", 1_800_000_000)
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V3_FROM_UNIX", 0)
    s = D("0.0001")
    after, before = 1_800_000_000 // 3600 * 3600 + 3600, 1_800_000_000 // 3600 * 3600 - 3600
    assert markets.scaled_sigma(markets.market_id("XAUUSD", "1h", after), s) == D("0.000170")
    assert markets.scaled_sigma(markets.market_id("XAUUSD", "15m", after), s) == D("0.000130")
    assert markets.scaled_sigma(markets.market_id("XAUUSD", "1d", markets.window_start("1d", 1_800_000_000 + 2 * 86400)), s) == s
    assert markets.scaled_sigma(markets.market_id("XAUUSD", "1h", before), s) == D("0.000120")   # replays as priced


def test_sigma_scale_v3_day_week(monkeypatch):
    """V3.6: 1d/1w get a scale from the arm; gold has its own; earlier markets replay as priced."""
    from decimal import Decimal as D
    from sn89_signals import config, markets
    arm = 1_800_000_000
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V3_FROM_UNIX", arm)
    s = D("0.0001")
    after = markets.window_start("1d", arm + 2 * 86400)
    before = markets.window_start("1d", arm - 2 * 86400)
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1d", after), s) == s * D("2.0")
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1w", markets.window_start("1w", arm + 9 * 86400)), s) == s * D("1.5")
    assert markets.scaled_sigma(markets.market_id("XAUUSD", "1d", after), s) == s * D("1.0")
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1h", after), s) == D("0.000170")
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1d", before), s) == s
    monkeypatch.setattr(config, "MARKETS_SIGMA_SCALE_V3_FROM_UNIX", 0)
    assert markets.scaled_sigma(markets.market_id("BTCUSD", "1d", after), s) == s
