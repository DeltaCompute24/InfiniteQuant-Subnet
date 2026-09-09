"""Carrying a miner's MAINNET record into the custom-sizing beta.

Whit, 2026-09-09: everyone qualified on the standard (LF) or high-frequency
(HF) board is qualified here and earns from their first call. The honest way to
apply that is to hand the validator each beta key's mainnet decisive record and
let scoring.qualified_calls run the points test over it -- the same test that
puts the QUALIFIED badge on the board -- rather than a whitelist.

Everything here is OFF unless SN89_BETA_ROSTER is set, which only .env.test
does. On mainnet there is no roster, so load_prior_by_hk returns {} and the
weight path is byte-identical to before this module existed.

Not replayable by a third party as-is: the LF record is read from the platform
DB and the roster from a local file. Acceptable on a single-validator testnet
beta; it must not be armed on 89 in this form.
"""
from __future__ import annotations

import json
import os
import sqlite3

from . import config, hf, scoring

ROSTER = os.getenv("SN89_BETA_ROSTER", "")
LF_DB = os.getenv("SN89_BETA_LF_DB",
                  "/opt/iq-platform/data/live/iq_admin_dash.db")
MAIN_HF_CACHE = os.path.expanduser(
    os.getenv("SN89_BETA_MAIN_HF_CACHE", "~/.sn89/hf-grade"))


def armed() -> bool:
    return bool(ROSTER)


def roster() -> list[dict]:
    if not ROSTER:
        return []
    try:
        with open(ROSTER) as fh:
            return json.load(fh).get("issued", [])
    except (FileNotFoundError, ValueError):
        return []


def carry_sigma_for(pair, t0_unix):
    """Sigma for a pair that may be on the LF board but not the HF one."""
    s = hf._board_sigma_for(pair, t0_unix)
    if s:
        return s
    try:
        b = (config.bands_as_of(t0_unix) or {}).get(str(pair).upper())
        if not b:
            return 0.0
        tp = float(b.get("tp_bps") if isinstance(b, dict) else b[0])
        hz = int(config.horizon_h_for(pair, t0_unix) * 3600)
        return scoring.sigma_from_board(tp, hz) if tp and hz else 0.0
    except Exception:                                              # noqa: BLE001
        return 0.0


def mainnet_record(mainnet_hk, user_id=None):
    """Decisive mainnet calls for one trader, priced, HF then LF. Same row
    shape as the beta: (t0, won, is_copy, resolved_unix, tp, hz, pair)."""
    out = []
    if not mainnet_hk:
        return out
    try:
        db = sqlite3.connect("file:%s/hf_grades.db?mode=ro" % MAIN_HF_CACHE, uri=True)
        for t0, pair, st, tp, sl, hz in db.execute(
                "SELECT t0_ms, pair, status, tp_bps, sl_bps, horizon_s FROM grades "
                "WHERE hk=? AND status IN ('won','lost')", (mainnet_hk,)):
            if tp is None or hz is None:
                b = (hf.hf_bands_as_of(t0 / 1000.0) or {}).get(pair)
                if not b:
                    continue
                tp, sl, hz = float(b[0]), float(b[1]), int(b[2])
            out.append((t0 / 1000.0, st == "won", False, None,
                        float(tp), int(hz), pair))
        db.close()
    except Exception as e:                                         # noqa: BLE001
        print("  ! HF record unreadable for %s: %s" % (mainnet_hk[:10], e))
    if user_id is None:
        return out
    try:
        c = sqlite3.connect("file:%s?mode=ro" % LF_DB, uri=True)
        for a, tpb, slb, hh, stt, t0a, t0b, created in c.execute(
                "SELECT asset, tp_bps, sl_bps, horizon_hours, "
                "COALESCE(onchain_status,status), onchain_t0_ms, anchor_t0_ms, created_at "
                "FROM signals_submissions WHERE signals_user_id=? "
                "AND COALESCE(onchain_status,status) IN ('won','lost')", (user_id,)):
            t0 = (t0a or t0b)
            if not t0 or not tpb or not hh:
                continue
            out.append((t0 / 1000.0, stt == "won", False, None,
                        float(tpb), int(hh) * 3600, str(a).upper()))
        c.close()
    except Exception as e:                                         # noqa: BLE001
        print("  ! LF record unreadable for user %s: %s" % (user_id, e))
    return out


def user_ids_by_mainnet() -> dict:
    """mainnet hotkey -> signals user id, for the LF half of the record."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % LF_DB, uri=True)
        out = {m: i for i, m in c.execute(
            "SELECT id, sn89_hotkey FROM signals_users "
            "WHERE sn89_hotkey IS NOT NULL AND sn89_hotkey<>''")}
        c.close()
        return out
    except Exception as e:                                         # noqa: BLE001
        print("  ! could not map hotkeys to signals users: %s" % e)
        return {}


def load_prior_by_hk() -> dict:
    """{testnet hotkey: prior decisive rows} for every issued beta key. {} when
    not armed. Keys with an empty record are omitted."""
    if not armed():
        return {}
    uid_of = user_ids_by_mainnet()
    out = {}
    for r in roster():
        thk, mhk = r.get("testnet_hotkey"), r.get("mainnet_hotkey")
        if not thk or not mhk:
            continue
        rec = mainnet_record(mhk, uid_of.get(mhk))
        if rec:
            out[thk] = rec
    return out
