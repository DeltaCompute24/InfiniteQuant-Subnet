#!/usr/bin/env python3
"""SN89 limit-order watcher — fires dashboard submissions when their trigger
price is hit (or immediately when they carry no trigger).

ONE signing path for every dashboard submission, all three competitions:

    dashboard → partner-webhook (auth, writes a row, NEVER holds keys)
        → sn89_limit_orders (iq_admin_dash.db)
        → THIS service (root: reads wallets, polls the tick bus, fires)
            kind=closers|hf → signed frame → ingest WSS → countersigned receipt
            kind=lf         → neurons.miner build_signal + submit (on-chain)

A row with trigger_price NULL fires on the next poll (~1 s) — that is the
"submit now" button. A row with (trigger_price, trigger_side) fires when the
bus price crosses it: side='above' → price >= trigger, 'below' → price <=.
The receipt / rejection / error is written back to the row, so the dashboard
shows exactly what the ingest signed, not what the webhook hoped.

Signer map (SN89_LIMIT_SIGNERS, JSON): {hotkey_ss58: {"hotkey_file": path}} or
{"wallet": name, "wallet_hotkey": hk} — which hotkeys this host may sign for
(hosted miners / testnet handoff keys). A row for an unmapped hotkey is marked
rejected:no_signer rather than silently parked — self-hosters submit straight
to the ingest with their own keys instead.

Also exports the closers grade cache to an iq-readable JSON each cycle
(SN89_CLOSERS_GRADES_EXPORT) so the dashboard/testnet page can render the
closers board — same pattern as iq-hf-grades-export.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bittensor_wallet import Keypair

from sn89_signals import config, hf

ADMIN_DB = os.getenv("IQ_ADMIN_DB", "/opt/iq-platform/data/live/iq_admin_dash.db")
TICKBUS = os.getenv("IQ_TICKBUS_URL", "http://127.0.0.1:18774")
WS_URL = os.getenv("SN89_LIMIT_WS", "ws://127.0.0.1:8792")
SIGNERS_PATH = os.getenv("SN89_LIMIT_SIGNERS", "/etc/iq/sn89-limit-signers.json")
POLL_S = float(os.getenv("SN89_LIMIT_POLL_S", "1.0"))
# Which network's orders THIS watcher instance fires. The table is shared
# (one dashboard DB) but a testnet and a mainnet watcher must never race for
# the same row — each instance claims only its own network's orders.
NETWORK_SCOPE = os.getenv("SN89_LIMIT_NETWORK", "test")
GRADES_DB = os.getenv("SN89_CLOSERS_GRADES_DB",
                      os.path.expanduser("~/.sn89/closers-grade/closers_grades.db"))
GRADES_EXPORT = os.getenv("SN89_CLOSERS_GRADES_EXPORT",
                          "/opt/iq-platform/data/live/sn89-closers-grades.json")
EXPORT_EVERY_S = float(os.getenv("SN89_CLOSERS_EXPORT_EVERY_S", "30"))
# Per-tenant feed tokens. An LF order commits on chain, and the commitment points
# at a blob URL the validator must be able to FETCH — so signing is only half the
# job. The signer map deliberately holds no token (it is a key-path map, mode 600
# for that reason), so the transport is looked up here, per tenant, exactly as
# multiplexer.py does before its own submit().
TENANTS_PATH = os.getenv(
    "SN89_TENANTS", "/opt/iq-platform/data/live/sn89-managed-main/tenants.json")

DDL = """
CREATE TABLE IF NOT EXISTS sn89_limit_orders (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  hotkey        TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('closers','hf','lf')),
  payload       TEXT NOT NULL,
  trigger_price REAL,
  trigger_side  TEXT CHECK(trigger_side IN ('above','below')),
  status        TEXT NOT NULL DEFAULT 'open',
  result        TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  fired_at      TEXT,
  expires_at    TEXT,
  network       TEXT NOT NULL DEFAULT 'test'
);
CREATE INDEX IF NOT EXISTS idx_sn89_limit_open ON sn89_limit_orders(status);
"""


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [limit-watcher] {m}", flush=True)


def fix_wal_ownership() -> None:
    """This service runs as root against an iq-owned WAL database. If sqlite
    ever (re)creates the -wal/-shm sidecars under root, every iq service that
    writes this DB starts EACCES-failing — the exact class of breakage in the
    rsync-perms post-mortem. Re-align the sidecars to the DB file's owner after
    every commit; a no-op when they already match."""
    try:
        st = os.stat(ADMIN_DB)
        for suffix in ("-wal", "-shm"):
            p = ADMIN_DB + suffix
            if os.path.exists(p):
                s = os.stat(p)
                if (s.st_uid, s.st_gid) != (st.st_uid, st.st_gid):
                    os.chown(p, st.st_uid, st.st_gid)
    except OSError:
        pass


def db() -> sqlite3.Connection:
    c = sqlite3.connect(ADMIN_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    # additive migration for rows/tables created before the network column
    if not any(r[1] == "network"
               for r in c.execute("PRAGMA table_info(sn89_limit_orders)")):
        c.execute("ALTER TABLE sn89_limit_orders ADD COLUMN network TEXT "
                  "NOT NULL DEFAULT 'test'")
    return c


def load_signers() -> dict:
    try:
        return json.load(open(SIGNERS_PATH))
    except Exception:  # noqa: BLE001
        return {}


def load_feed_tokens() -> dict:
    """tenant -> feed_token, for the LF blob upload. Reloaded on the same cycle
    as the signer map so a newly enrolled tenant can submit without a restart."""
    try:
        tenants = json.load(open(TENANTS_PATH))
    except Exception:  # noqa: BLE001
        return {}
    return {name: t["feed_token"] for name, t in tenants.items()
            if isinstance(t, dict) and t.get("feed_token")}


# ── LF submission rows ───────────────────────────────────────────────────────
# A workspace LF order used to commit on chain and exist NOWHERE else: no
# signals_submissions row, and comment="iq-limit", which reconcile.sh's
# `plaintext LIKE '%iq-follow%'` filter cannot match. So the call came back from
# the chain unattributable — it never appeared in the trader's history, never
# counted in their W/L, never got a result message, and never got an entry
# receipt. Zero LF workspace calls had EVER been graded when this was written.
#
# The row is created HERE, at fire time, not when the order is placed: a resting
# limit order can wait hours, and t0 is the commit, not the click.
DAY_CAP = int(os.getenv("SN89_LF_DAY_CAP", "3"))
MIN_GAP_MINUTES = float(os.getenv("SN89_LF_MIN_GAP_MIN", "60"))
COMMIT_MAP_PATH = os.getenv("SN89_COMMIT_MAP",
                            "/opt/iq-platform/data/live/sn89-commit-map.jsonl")


def signals_user_for_hotkey(con: sqlite3.Connection, hk: str):
    r = con.execute("SELECT id FROM signals_users WHERE sn89_hotkey = ?",
                    (hk,)).fetchone()
    return r["id"] if r else None


def quota_block(con: sqlite3.Connection, uid: int):
    """The bot's DAY_CAP and MIN_GAP, applied to this lane too.

    Mirrors submissionsToday() / minGapBlock() in iq-signals-bot. Both lanes now
    write signals_submissions, so they share one quota per miner — which is what
    the chain already enforced: over-quota calls come back void (`daily_quota`,
    `min_spacing`) having spent a commit. Refusing here costs the trader nothing
    and tells them why."""
    n = con.execute(
        "SELECT COUNT(*) FROM signals_submissions WHERE signals_user_id = ? "
        "AND submitted_at >= strftime('%Y-%m-%dT00:00:00.000Z','now') "
        "AND status NOT IN ('rejected','failed')", (uid,)).fetchone()[0]
    if n >= DAY_CAP:
        return f"lf_daily_cap:{n}/{DAY_CAP}"
    r = con.execute(
        "SELECT submitted_at FROM signals_submissions WHERE signals_user_id = ? "
        "AND status NOT IN ('rejected','failed') "
        "ORDER BY submitted_at DESC LIMIT 1", (uid,)).fetchone()
    if r and r["submitted_at"]:
        try:
            last = time.mktime(time.strptime(r["submitted_at"][:19],
                                             "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            return None
        mins = (time.time() - last) / 60.0
        if mins < MIN_GAP_MINUTES:
            return f"lf_min_gap:{MIN_GAP_MINUTES - mins:.0f}min"
    return None


def create_submission(con: sqlite3.Connection, uid: int, sig) -> int:
    """Insert the row this call will be graded and reported against.

    Band values come off the SIGNAL, never off a second board read: the signal is
    what gets committed, so anything else could disagree with what the validator
    grades. entry_price stays NULL on purpose — the anchored entry is a function
    of the commit block and is supplied by iq-sn89-lf-receipt once the tick window
    seals. A fire-time guess here is the number that manufactured the Jeremiah
    dispute on 2026-08-03."""
    cur = con.execute(
        "INSERT INTO signals_submissions "
        "(signals_user_id, asset, asset_class, direction, entry_method, "
        " tp_bps, sl_bps, horizon_hours, status, venue_mode, mechanism, "
        " raw_input, submitted_at) "
        "VALUES (?,?,?,?,'MARKET',?,?,?,'fired','paper',0,?, "
        "        strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (uid, sig.trade_pair, sig.asset_class, sig.direction,
         int(sig.tp_bps), int(sig.sl_bps), int(sig.horizon_h),
         "workspace-lf"))
    con.commit()
    return int(cur.lastrowid)


def record_commit(sub_id: int, hk: str, sig, res: dict) -> None:
    """Append the submission -> commitment mapping iq-sn89-lf-receipt reads.

    Same file and shape the multiplexer writes. Without this line the receipt
    service cannot tell which t0_ms (and so which anchored entry) belongs to this
    call, and the trader gets no entry and no replay chart. Never raises: a
    bookkeeping failure must not turn a landed commit into a reported error."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "submission_id": sub_id, "tenant": None, "hotkey": hk,
               "asset": sig.trade_pair, "direction": sig.direction,
               "mechanism": 0, "commitment": res.get("commitment"),
               "ok": bool(res.get("ok")), "source": "limit_watcher"}
        with open(COMMIT_MAP_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠ commit-map write failed for #{sub_id}: {e}")


def keypair_for(hk: str, signers: dict) -> Keypair | None:
    ent = signers.get(hk)
    if not ent:
        return None
    if ent.get("hotkey_file"):
        d = json.load(open(os.path.expanduser(ent["hotkey_file"])))
        kp = (Keypair.create_from_seed(d["secretSeed"])
              if str(d.get("secretSeed", "")).startswith("0x")
              else Keypair.create_from_mnemonic(d["secretPhrase"]))
        return kp if kp.ss58_address == hk else None
    return None


def ticks() -> dict:
    with urllib.request.urlopen(f"{TICKBUS}/ticks", timeout=2) as r:
        return json.loads(r.read()).get("ticks", {}) or {}


def _seq_path(hk: str) -> str:
    return os.path.expanduser(f"~/.sn89/limit_watcher_seq_{hk}.json")


def next_seq(hk: str) -> int:
    """Shares hf.next_submit_seq with the self-hosted miner — see that docstring.

    This used to seed from int(time.time()) and then increment by 1 per call, so a
    hotkey's counter drifted BEHIND wall clock by however long it had been in use
    (~9.5 days when this was found). Only the seed was time-based; every step after
    it was +1. That is why fixing the miner alone would have flipped the lockout onto
    this path instead of clearing it.
    """
    return hf.next_submit_seq(_seq_path(hk))


async def fire_ws(kp: Keypair, payload: dict) -> dict:
    import websockets
    seq = next_seq(kp.ss58_address)
    nonce = os.urandom(8).hex()
    ts = int(time.time() * 1000)
    sb = hf.submit_signing_bytes(kp.ss58_address, seq, nonce, payload, ts)
    frame = {"kind": "hf.submit", "v": 1, "hk": kp.ss58_address, "seq": seq,
             "nonce": nonce, "payload": payload, "ts_miner": ts,
             "sig": kp.sign(sb).hex()}
    async with websockets.connect(WS_URL, open_timeout=10) as ws:
        await ws.send(json.dumps(frame))
        return json.loads(await ws.recv())


def fire_lf(hk: str, signers: dict, payload: dict, feed_tokens: dict,
            con: sqlite3.Connection) -> dict:
    """On-chain LF commit via the normal miner path (build_signal + submit).
    Requires a wallet-name signer entry (bt.Wallet signs the extrinsic).

    Also creates the signals_submissions row and stamps `iq-follow:<id>` so the
    call is attributable everywhere the Telegram lane is — history, W/L, grading,
    entry receipt, replay chart. See the section comment above create_submission."""
    ent = signers.get(hk) or {}
    if not ent.get("wallet"):
        return {"kind": "error", "reason": "lf_needs_wallet_signer"}
    import bittensor as bt
    from neurons.miner import build_signal, submit
    # Fall back to the NAME implied by hotkey_file, never to "default". No managed
    # tenant is named "default" (156 "miner", 3 "miner_e2", 1 "hosted"), so that
    # guess could only ever produce a FileNotFound — and it did, on every LF order,
    # while the map entry looked complete.
    hk_name = ent.get("wallet_hotkey")
    if not hk_name and ent.get("hotkey_file"):
        hk_name = os.path.basename(ent["hotkey_file"])
    if not hk_name:
        return {"kind": "error", "reason": "lf_signer_has_no_hotkey_name"}
    w = bt.Wallet(name=ent["wallet"], hotkey=hk_name)
    # submit() encrypts the blob, uploads it, and commits the URL on chain. With
    # no transport configured bucket.upload() raises "no blob transport" AFTER the
    # wallet loads — which is what every LF order did from 2026-08-08 (when the
    # hotkey-name fix unmasked it) to 2026-08-12. This service has no R2 creds and
    # must NOT use SN89_BLOB_DIR: /opt/sn89-blobs is not what the relay serves, so
    # that path publishes blobs no validator can fetch — silently worse than the
    # rejection. Publish through the owner relay as the tenant, same as the
    # multiplexer. Single-threaded fire loop, so mutating the global is race-free.
    token = feed_tokens.get(ent.get("tenant") or "")
    if not token:
        return {"kind": "error",
                "reason": f"lf_no_feed_token_for_tenant:{ent.get('tenant')}"}
    from sn89_signals import config as _sn89_config
    _sn89_config.RELAY_TOKEN = token

    uid = signals_user_for_hotkey(con, hk)
    if uid is None:
        return {"kind": "error", "reason": "lf_hotkey_not_linked_to_signals_account"}
    blocked = quota_block(con, uid)
    if blocked:
        return {"kind": "error", "reason": blocked}

    # build_signal validates the pair against the board and refuses a call that
    # would void (pair off board, FX weekend, dead horizon) — so it runs BEFORE
    # the row is created. A rejected call must not consume a daily slot.
    sig = build_signal(w.hotkey.ss58_address, payload["trade_pair"],
                       payload["direction"])
    sub_id = create_submission(con, uid, sig)
    # Signal is a plain dataclass and commitment() hashes current field values,
    # so stamping the comment here (after the id exists) is what gets committed.
    sig.comment = f"iq-follow:{sub_id}"
    try:
        res = submit(w, sig)
    except Exception as e:  # noqa: BLE001
        con.execute("UPDATE signals_submissions SET status='failed', "
                    "fire_error=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id=?", (f"{type(e).__name__}: {e}", sub_id))
        con.commit()
        raise
    if not res.get("ok"):
        con.execute("UPDATE signals_submissions SET status='failed', "
                    "fire_error=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id=?", (json.dumps(res)[:500], sub_id))
        con.commit()
        return {"kind": "error", "reason": "lf_submit_failed", **res}
    record_commit(sub_id, hk, sig, res)
    return {"kind": "lf.commit", "submission_id": sub_id, **res}


def fire(row: sqlite3.Row, signers: dict, feed_tokens: dict,
         con: sqlite3.Connection) -> dict:
    payload = json.loads(row["payload"])
    if row["kind"] == "lf":
        return fire_lf(row["hotkey"], signers, payload, feed_tokens, con)
    kp = keypair_for(row["hotkey"], signers)
    if kp is None:
        return {"kind": "error", "reason": "no_signer_for_hotkey"}
    if row["kind"] == "hf":
        # Stamp the band/horizon from the HF board AT FIRE TIME — a limit
        # order can rest across a board change, and hf.validate_submission
        # checks the payload against the board as of the receive instant.
        pair = str(payload["trade_pair"]).upper()
        board = hf.hf_bands_as_of(time.time()) or {}
        if pair not in board:
            return {"kind": "error", "reason": f"pair_not_on_hf_board:{pair}"}
        tp, sl, hz, cls = board[pair]
        # A band the miner DREW survives the rest. Stamping the board over it
        # here would submit a different trade from the one on their screen --
        # same shape as the ratchet reading the previous position's P&L: the
        # value is right for the mechanism and wrong for this call.
        #
        # Only honoured when the chain has armed custom sizing. On an unarmed
        # chain the board is the rule, so passing a drawn band through would
        # earn a band_mismatch rejection instead of quietly being ignored --
        # and a rejection the miner can see beats a silent override.
        if config.custom_bands_enforced_as_of(time.time()):
            tp = float(payload.get("tp_bps") or tp)
            sl = float(payload.get("sl_bps") or sl)
            hz = int(payload.get("horizon_s") or hz)
        payload = {"trade_pair": pair, "direction": payload["direction"],
                   "asset_class": cls, "tp_bps": tp, "sl_bps": sl,
                   "horizon_s": hz}
    return asyncio.run(fire_ws(kp, payload))


def export_grades() -> None:
    if not os.path.exists(GRADES_DB):
        return
    src = sqlite3.connect(f"file:{GRADES_DB}?mode=ro", uri=True)
    rows = [{"key": k, "hk": h, "t0_ms": t, "pair": p, "action": a,
             "score": s, "status": st}
            for k, h, t, p, a, s, st in src.execute(
                "SELECT key, hk, t0_ms, pair, action, score, status FROM grades")]
    # pending too — "submission arrived, horizon still running" is the state a
    # trader looks for right after clicking, and grades alone can't show it
    try:
        pend = [{"key": k, "hk": h, "t0_ms": t, "pair": p, "action": a,
                 "position_id": pid or ""}
                for k, h, t, p, a, pid in src.execute(
                    "SELECT key, hk, t0_ms, pair, action, pid FROM pending")]
    except sqlite3.OperationalError:
        # validator hasn't run the pid migration on this cache yet (it owns
        # the schema; this reader is read-only) — export without position ids
        pend = [{"key": k, "hk": h, "t0_ms": t, "pair": p, "action": a}
                for k, h, t, p, a in src.execute(
                    "SELECT key, hk, t0_ms, pair, action FROM pending")]
    src.close()
    tmp = GRADES_EXPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"exported_at_ms": int(time.time() * 1000), "grades": rows,
                   "pending": pend}, fh)
    os.replace(tmp, GRADES_EXPORT)
    try:
        os.chmod(GRADES_EXPORT, 0o644)
    except OSError:
        pass


def due(row: sqlite3.Row, px: dict) -> bool:
    if row["trigger_price"] is None:
        return True
    pair = json.loads(row["payload"]).get("trade_pair", "").upper()
    tick = px.get(pair)
    if not tick or not tick.get("price"):
        return False
    p = float(tick["price"])
    return (p >= float(row["trigger_price"]) if row["trigger_side"] == "above"
            else p <= float(row["trigger_price"]))


def main() -> None:
    log(f"db={ADMIN_DB} ws={WS_URL} signers={SIGNERS_PATH}")
    last_export = 0.0
    while True:
        try:
            c = db()
            open_rows = c.execute(
                "SELECT * FROM sn89_limit_orders WHERE status='open' AND network=?",
                (NETWORK_SCOPE,)).fetchall()
            if open_rows:
                signers = load_signers()
                feed_tokens = load_feed_tokens()
                try:
                    px = ticks()
                except Exception:  # noqa: BLE001
                    px = {}
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for row in open_rows:
                    if row["expires_at"] and row["expires_at"] < now_iso:
                        c.execute("UPDATE sn89_limit_orders SET status='expired' "
                                  "WHERE id=?", (row["id"],))
                        continue
                    if not due(row, px):
                        continue
                    try:
                        res = fire(row, signers, feed_tokens, c)
                    except Exception as e:  # noqa: BLE001
                        res = {"kind": "error", "reason": f"{type(e).__name__}: {e}"}
                    ok = res.get("kind") in ("hf.receipt", "lf.commit")
                    status = "filled" if ok else "rejected"
                    c.execute("UPDATE sn89_limit_orders SET status=?, result=?, "
                              "fired_at=? WHERE id=?",
                              (status, json.dumps(res), now_iso, row["id"]))
                    log(f"#{row['id']} {row['kind']} {row['hotkey'][:8]}… → {status} "
                        f"{res.get('reason', '')}")
            c.commit()
            c.close()
            fix_wal_ownership()
            if time.time() - last_export > EXPORT_EVERY_S:
                export_grades()
                last_export = time.time()
        except Exception as e:  # noqa: BLE001
            log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
