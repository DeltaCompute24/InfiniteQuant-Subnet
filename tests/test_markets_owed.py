"""Markets V2e: what a cycle could not pay is carried into later cycles."""
import sqlite3
from decimal import Decimal

from sn89_signals import config, markets

D = Decimal


def test_claims_over_the_emission_are_paid_pro_rata_and_the_rest_is_owed():
    out = markets.owed_step({"A": (D(30), D(0), False), "B": (D(10), D(0), False)}, D(20))
    assert out["A"] == (30, 15, 15) and out["B"] == (10, 5, 5)


def test_owed_is_paid_from_a_later_quiet_cycle():
    out = markets.owed_step({"A": (D(0), D(15), False), "B": (D(2), D(5), False)}, D(100))
    assert out["A"] == (15, 15, 0) and out["B"] == (7, 7, 0)       # under the emission: paid in full


def test_a_breach_claims_nothing_and_keeps_its_balance():
    out = markets.owed_step({"A": (D(0), D(15), True), "B": (D(10), D(0), False)}, D(5))
    assert out["A"] == (0, 0, 15) and out["B"] == (10, 5, 5)


def test_seed_replays_the_cycles_since_the_carry_arm(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settle_meta (cycle INTEGER PRIMARY KEY, emission TEXT)")
    db.execute("CREATE TABLE settle (cycle INTEGER, entity TEXT, payable TEXT)")
    monkeypatch.setattr(config, "MARKETS_CARRY_FROM_UNIX", 100)
    for c, rows in ((50, {"A": "99"}), (100, {"A": "30", "B": "10"}), (200, {"A": "0", "B": "0"}),
                    (300, {"A": "40", "B": "0"})):
        db.execute("INSERT INTO settle_meta VALUES (?, '20')", (c,))
        for e, p in rows.items():
            db.execute("INSERT INTO settle VALUES (?,?,?)", (c, e, p))
    # 50 is before the carry arm: ignored. 100: A 15/15 owed, B 5/5. 200: both paid off (20 = 15 + 5).
    # 300: A claims 40, paid 20, owes 20.
    assert markets.owed_seed(db, 300) == {"A": D(0), "B": D(0)}
    assert markets.owed_seed(db, 400) == {"A": D(20), "B": D(0)}
    assert markets.owed_seed(db, 200) == {"A": D(15), "B": D(5)}


def test_vector_uses_paid_from_the_arm(monkeypatch, tmp_path):
    db = markets._db(str(tmp_path))
    c = config.MARKETS_COLLATERAL_FROM_UNIX + 40 * config.MARKETS_SETTLE_PERIOD_S
    monkeypatch.setattr(config, "MARKETS_OWED_FROM_UNIX", c)
    db.execute("INSERT INTO settle_meta VALUES (?, '20')", (c,))
    db.execute("INSERT INTO settle (cycle, entity, won, lost, burned, deducted, debt, payable, lcum, bcum, dcum, "
               "claim, paid, owed) VALUES (?, 'A', '0','0','0','0','0','40','0','0','0','40','10','30')", (c,))
    v = markets.pnl_vector(db, {"A": 7}, c + config.MARKETS_SETTLE_PERIOD_S + config.MARKETS_SETTLE_GRACE_S + 10)
    assert abs(v[7] - 0.5) < 1e-9 and abs(v[config.BURN_UID] - 0.5) < 1e-9
