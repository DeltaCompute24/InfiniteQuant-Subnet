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

from sn89_signals import hf

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


def fire_lf(hk: str, signers: dict, payload: dict, feed_tokens: dict) -> dict:
    """On-chain LF commit via the normal miner path (build_signal + submit).
    Requires a wallet-name signer entry (bt.Wallet signs the extrinsic)."""
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
    sig = build_signal(w.hotkey.ss58_address, payload["trade_pair"],
                       payload["direction"], comment="iq-limit")
    return {"kind": "lf.commit", **submit(w, sig)}


def fire(row: sqlite3.Row, signers: dict, feed_tokens: dict) -> dict:
    payload = json.loads(row["payload"])
    if row["kind"] == "lf":
        return fire_lf(row["hotkey"], signers, payload, feed_tokens)
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
                        res = fire(row, signers, feed_tokens)
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
