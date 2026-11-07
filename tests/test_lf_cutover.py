"""LF cutover: account migration and the own-UID bond (replay.apply_account_migrations,
weights_from_journal lf_bonded)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import chain, config, replay  # noqa: E402

ENT = sorted(config.SIGNALS_ENTITY_HOTKEYS)[0]
ACCT = f"{ENT}_7"
OLD = "5F7hr7531nvHnqw3Rfhihofe9QVJGrWCrEMwYe4fZ5BKT2og"
FROM = config.LF_MIGRATION_FROM_UNIX
NOW = FROM + 3600


def mig(frm=OLD, to=ACCT, block=10, unix=FROM - 100):
    return {"from_hk": frm, "to_hk": to, "commit_block": block, "commit_unix": unix}


def att(frm=OLD, to=ACCT):
    return {"from_hk": frm, "to_hk": to}


ROWS = [{"commit_hex": "a", "hotkey": OLD, "t0_unix": FROM - 500, "status": "won"},
        {"commit_hex": "b", "hotkey": OLD, "t0_unix": FROM + 10, "status": "won"},
        {"commit_hex": "c", "hotkey": "other", "t0_unix": FROM - 5, "status": "lost"}]
META = {OLD: {"first_seen_unix": 100.0, "strikes": 1}, "other": {"first_seen_unix": 5.0, "strikes": 0}}


def test_round_trip():
    assert chain.decode_account_migration(chain.encode_account_migration(ACCT)) == {"to": ACCT}
    assert chain.decode_account_migration(f"sn89mig:2:{ACCT}") is None
    assert chain.decode_account_migration(f"sn89mig:1:{ENT}") is None
    assert chain._decode_any(chain.encode_account_migration(ACCT))["kind"] == "account_migration"


def test_moves_rows_before_cutover_only():
    s, m, moved = replay.apply_account_migrations(ROWS, META, [mig()], [att()], NOW)
    assert moved == {OLD}
    assert [r["hotkey"] for r in s] == [ACCT, OLD, "other"]
    assert OLD not in m and m[ACCT]["strikes"] == 1 and m[ACCT]["first_seen_unix"] == 100.0
    assert ROWS[0]["hotkey"] == OLD and META[OLD]["strikes"] == 1   # inputs untouched


def test_late_commit_moves_up_to_commit():
    s, _, _ = replay.apply_account_migrations(ROWS, META, [mig(unix=FROM + 50)], [att()], NOW)
    assert s[1]["hotkey"] == ACCT


def test_merges_with_existing_account():
    meta = {**META, ACCT: {"first_seen_unix": 200.0, "strikes": 2}}
    _, m, _ = replay.apply_account_migrations(ROWS, meta, [mig()], [att()], NOW)
    assert m[ACCT] == {"first_seen_unix": 100.0, "strikes": 3}


@pytest.mark.parametrize("migs,atts,now", [
    ([mig()], [], NOW),                                     # no attestation
    ([mig()], [att(to=f"{ENT}_8")], NOW),                   # attested elsewhere
    ([mig()], [att()], FROM - 1),                           # before the cutover
    ([mig(to=f"5Dunknown{'x' * 39}_7")], [att(to=f"5Dunknown{'x' * 39}_7")], NOW),  # not allow-listed
    ([mig(to=f"{ENT}_{config.AGENT_ACCOUNT_MIN}")],
     [att(to=f"{ENT}_{config.AGENT_ACCOUNT_MIN}")], NOW),   # an agent account
])
def test_refused(migs, atts, now):
    s, m, moved = replay.apply_account_migrations(ROWS, META, migs, atts, now)
    assert moved == set() and s is ROWS and m is META


def test_one_hotkey_per_account():
    a = mig(block=1)
    b = mig(frm="other", block=2)
    _, _, moved = replay.apply_account_migrations(ROWS, META, [b, a], [att(), att(frm="other")], NOW)
    assert moved == {OLD}


def test_config_dates():
    assert config.LF_MIGRATION_FROM_UNIX == config.LF_BOND_FROM_UNIX == 1794182400
    assert config.LF_BOND_MIN_ALPHA == 1000.0
    assert config.lf_bond_active(1794182400) and not config.lf_bond_active(1794182399)


# ── the bond gate and a migrated record, through the real replay ─────────────
from sn89_signals.schema import Signal  # noqa: E402

T = 10_000_000.0
H = 3600.0


def _pt(hk, pair):
    return Signal(trade_pair=pair, direction="LONG", tp_bps=150, sl_bps=150, ts_miner=0, hotkey=hk,
                  asset_class="crypto").canonical_bytes().decode()


def _sigs(hk, pair, wins, losses):
    return [{"commit_hex": f"{hk}-{i}", "hotkey": hk, "t0_unix": T - 5 * 24 * H + i * H,
             "status": "won" if i < wins else "lost", "plaintext": _pt(hk, pair)}
            for i in range(wins + losses)]


def _meta(*hks):
    return {hk: {"first_seen_unix": T - 40 * 24 * H, "strikes": 0} for hk in hks}


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)


def test_unbonded_uid_earns_nothing_and_its_share_goes_to_the_rest():
    sigs = _sigs("A", "BTCUSD", 30, 10) + _sigs("B", "ETHUSD", 30, 10)
    uids = {"A": 1, "B": 2}
    open_ = replay.weights_from_journal(sigs, _meta("A", "B"), uids, T)
    assert open_.get(1, 0) > 0 and open_.get(2, 0) > 0
    w = replay.weights_from_journal(sigs, _meta("A", "B"), uids, T, lf_bonded={"A"})
    assert w.get(2, 0) == 0 and w.get(1, 0) > 0.99


def test_none_means_no_gate():
    sigs = _sigs("A", "BTCUSD", 30, 10)
    a = replay.weights_from_journal(sigs, _meta("A"), {"A": 1}, T)
    assert a == replay.weights_from_journal(sigs, _meta("A"), {"A": 1}, T, lf_bonded=None)


def test_entity_account_is_exempt():
    acct = f"{ENT}_9"
    sigs = _sigs(acct, "BTCUSD", 30, 10)
    w = replay.weights_from_journal(sigs, _meta(acct), {ENT: 5}, T, lf_bonded=set())
    assert w.get(5, 0) > 0.99


def test_keepalive_skips_unbonded_uid():
    sigs = _sigs("A", "BTCUSD", 30, 10)
    uids = {"A": 1, "B": 2}
    w = replay.weights_from_journal(sigs, _meta("A", "B"), uids, T, keepalive={2}, lf_bonded={"A"})
    assert w.get(2, 0) == 0


def test_agent_scope_ignores_the_gate():
    assert replay.weights_from_journal([], {}, {}, T, scope="agent", lf_bonded=set()) \
        == replay.weights_from_journal([], {}, {}, T, scope="agent")


def test_migrated_record_pays_the_entity_uid(monkeypatch):
    monkeypatch.setattr(config, "LF_MIGRATION_FROM_UNIX", int(T - H))
    acct = f"{ENT}_9"
    sigs = _sigs("A", "BTCUSD", 30, 10)
    m = [{"from_hk": "A", "to_hk": acct, "commit_block": 1, "commit_unix": T - 2 * H}]
    s, meta, moved = replay.apply_account_migrations(sigs, _meta("A"), m,
                                                     [{"from_hk": "A", "to_hk": acct}], T)
    uids = {h: u for h, u in {"A": 1, ENT: 5}.items() if h not in moved}
    w = replay.weights_from_journal(s, meta, uids, T, lf_bonded=set())
    assert w.get(5, 0) > 0.99 and w.get(1, 0) == 0
