#!/usr/bin/env python3
"""SN89 Signals validator.

Loop (every POLL_INTERVAL_S):
  1. INGEST    — read all sn89 commitments; journal NEW (hotkey, commit) pairs
                 with first_seen_block (canonical T0); fetch + store ciphertext
                 blobs whose url_tag matches.
  2. REVEAL    — for journaled rows whose drand round has matured: fetch the
                 round signature, decrypt W_time, verify SHA256(pt) == commit
                 and the round window (§5); parse + structurally validate.
  3. GRADE     — run validity filters (§6.4) over the revealed set, then
                 walk-forward touch-grade decisive outcomes from Polygon.
  4. WEIGHTS   — every tempo: gate → pro-rata trailing-8d wins → set_weights.

All state lives in one SQLite journal so a restarted validator replays to the
same conclusions (grading is deterministic given the same chain + Polygon).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import bittensor as bt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import bucket, chain, config, crypto, scoring
from sn89_signals.grader import PENDING, grade
from sn89_signals.schema import Signal, ValidationError, validate
from timelock import Timelock

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  commit_hex   TEXT PRIMARY KEY,
  hotkey       TEXT NOT NULL,
  round        INTEGER NOT NULL,
  url_tag      TEXT NOT NULL,
  first_seen_block INTEGER NOT NULL,
  t0_unix      REAL NOT NULL,
  blob_json    TEXT,
  plaintext    TEXT,
  status       TEXT NOT NULL DEFAULT 'sealed',
    -- sealed | revealed | void | pending | won | lost | washed
  void_reason  TEXT,
  entry_price  REAL,
  outcome_bps  REAL,
  exit_reason  TEXT,
  exit_at_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_hotkey ON signals(hotkey);
CREATE TABLE IF NOT EXISTS hotkey_meta (
  hotkey TEXT PRIMARY KEY,
  first_seen_unix REAL NOT NULL,
  strikes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drand_cache (round INTEGER PRIMARY KEY, signature BLOB);
"""


class Validator:
    def __init__(self, wallet: "bt.Wallet"):
        self.wallet = wallet
        self.ch = chain.Chain()
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH)
        self.db.executescript(SCHEMA)
        self.tlock = Timelock(config.DRAND_PUBLIC_KEY)
        self._last_weights_block = 0

    # ── 1. ingest ────────────────────────────────────────────────────────────
    def ingest(self):
        block = self.ch.current_block()
        commits = self.ch.read_all_commitments()
        now = time.time()
        for hk, c in commits.items():
            row = self.db.execute(
                "SELECT 1 FROM signals WHERE commit_hex=?", (c["commit"],)).fetchone()
            if row:
                continue
            t0 = self.ch.block_time_unix(block)  # first-seen block ≈ T0 (poll ≤30s lag)
            self.db.execute(
                "INSERT OR IGNORE INTO signals (commit_hex,hotkey,round,url_tag,"
                "first_seen_block,t0_unix) VALUES (?,?,?,?,?,?)",
                (c["commit"], hk, c["round"], c["url_tag"], block, t0))
            self.db.execute(
                "INSERT OR IGNORE INTO hotkey_meta (hotkey, first_seen_unix) VALUES (?,?)",
                (hk, now))
            print(f"  + sealed {c['commit'][:12]}… {hk[:8]}… round={c['round']}")
        self.db.commit()

        # fetch missing ciphertext blobs (urls reconstructed from the bucket
        # convention; miners using custom hosting are matched purely by tag —
        # they must serve {base}/{hotkey}/<nonce>.json and we discover nonce
        # at reveal; until then try the bucket listing endpoint if configured)
        for commit_hex, hk, tag in self.db.execute(
                "SELECT commit_hex, hotkey, url_tag FROM signals "
                "WHERE blob_json IS NULL AND status='sealed'").fetchall():
            blob = self._find_blob(hk, tag)
            if blob is not None:
                self.db.execute("UPDATE signals SET blob_json=? WHERE commit_hex=?",
                                (json.dumps(blob, separators=(",", ":")), commit_hex))
        self.db.commit()

    def _find_blob(self, hotkey: str, tag: str) -> dict | None:
        if not config.R2_PUBLIC_BASE:
            return None
        # bucket listing: {base}/{hotkey}/index.json maintained by the miner
        idx = bucket.fetch(f"{config.R2_PUBLIC_BASE.rstrip('/')}/{hotkey}/index.json") or {}
        for nonce in (idx.get("nonces") or [])[-50:]:
            url = bucket.blob_url(hotkey, nonce)
            if chain.url_tag(url) == tag:
                return bucket.fetch(url)
        return None

    # ── 2. reveal ────────────────────────────────────────────────────────────
    def reveal(self):
        now = time.time()
        rows = self.db.execute(
            "SELECT commit_hex, hotkey, round, t0_unix, blob_json FROM signals "
            "WHERE status='sealed' AND blob_json IS NOT NULL").fetchall()
        for commit_hex, hk, rnd, t0, blob_json in rows:
            if crypto.round_time(rnd) > now:
                continue
            if not crypto.expected_round_ok(rnd, t0):
                self._void(commit_hex, "round_out_of_window", strike=False)
                continue
            sig_bytes = self._drand_sig(rnd)
            if sig_bytes is None:
                continue
            pt = crypto.decrypt_timelock(json.loads(blob_json), sig_bytes, self.tlock)
            if pt is None or __import__("hashlib").sha256(pt).hexdigest() != commit_hex:
                self._void(commit_hex, "decrypt_or_hash_mismatch", strike=True)
                continue
            try:
                parsed = validate(Signal.from_bytes(pt))
                if parsed.hotkey != hk:
                    raise ValidationError("payload hotkey != committing hotkey")
            except (ValidationError, Exception) as e:  # noqa: BLE001
                self._void(commit_hex, f"invalid_payload:{e}", strike=False)
                continue
            self.db.execute(
                "UPDATE signals SET status='revealed', plaintext=? WHERE commit_hex=?",
                (pt.decode(), commit_hex))
            print(f"  ✓ revealed {commit_hex[:12]}… {parsed.direction} {parsed.trade_pair}")
        self.db.commit()

    def _void(self, commit_hex: str, reason: str, strike: bool):
        self.db.execute(
            "UPDATE signals SET status='void', void_reason=? WHERE commit_hex=?",
            (reason, commit_hex))
        if strike:
            self.db.execute(
                "UPDATE hotkey_meta SET strikes = strikes + 1 WHERE hotkey = "
                "(SELECT hotkey FROM signals WHERE commit_hex=?)", (commit_hex,))
        print(f"  ✗ void {commit_hex[:12]}… {reason}")

    def _drand_sig(self, rnd: int) -> bytes | None:
        row = self.db.execute("SELECT signature FROM drand_cache WHERE round=?", (rnd,)).fetchone()
        if row:
            return row[0]
        sig = crypto.fetch_drand_signature(rnd)
        if sig:
            self.db.execute("INSERT OR REPLACE INTO drand_cache VALUES (?,?)", (rnd, sig))
            self.db.commit()
        return sig

    # ── 3. grade ─────────────────────────────────────────────────────────────
    def grade_revealed(self):
        # deterministic validity pass over everything revealed/graded
        rows = []
        for commit_hex, hk, t0, pt, status in self.db.execute(
                "SELECT commit_hex, hotkey, t0_unix, plaintext, status FROM signals "
                "WHERE status IN ('revealed','pending','won','lost','washed')").fetchall():
            s = Signal.from_bytes(pt.encode())
            rows.append((commit_hex, s, scoring.GradedRow(
                hotkey=hk, trade_pair=s.trade_pair, direction=s.direction,
                t0_unix=t0, status="ok" if status in ("revealed", "pending") else status)))
        filtered = scoring.apply_validity_filters([r for _, _, r in rows])
        for (commit_hex, s, _), fr in zip(rows, filtered):
            if fr.status == "void":
                self._void(commit_hex, fr.void_reason or "validity", strike=False)

        # touch-grade whatever survives and isn't decisive yet
        now_ms = int(time.time() * 1000)
        for commit_hex, hk, t0, pt in self.db.execute(
                "SELECT commit_hex, hotkey, t0_unix, plaintext FROM signals "
                "WHERE status IN ('revealed','pending')").fetchall():
            s = Signal.from_bytes(pt.encode())
            g = grade(s, int(t0 * 1000), now_ms)
            if g.status == PENDING:
                self.db.execute("UPDATE signals SET status='pending', entry_price=? "
                                "WHERE commit_hex=?", (g.entry_price, commit_hex))
                continue
            self.db.execute(
                "UPDATE signals SET status=?, entry_price=?, outcome_bps=?, "
                "exit_reason=?, exit_at_ms=? WHERE commit_hex=?",
                (g.status, g.entry_price, g.outcome_bps, g.exit_reason,
                 g.exit_at_ms, commit_hex))
            print(f"  ⚖ {g.status.upper()} {s.direction} {s.trade_pair} "
                  f"{g.outcome_bps and round(g.outcome_bps, 1)}bps · {hk[:8]}…")
        self.db.commit()

    # ── 4. weights ───────────────────────────────────────────────────────────
    def maybe_set_weights(self):
        block = self.ch.current_block()
        if block - self._last_weights_block < config.TEMPO:
            return
        mg = self.ch.metagraph()
        uid_by_hotkey = {hk: i for i, hk in enumerate(mg.hotkeys)}
        now = time.time()
        cutoff = now - config.SCORE_WINDOW_S

        states = []
        for hk, first_seen, strikes in self.db.execute(
                "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta").fetchall():
            uid = uid_by_hotkey.get(hk)
            if uid is None:
                continue
            if strikes >= config.STRIKE_LIMIT:
                continue  # zeroed (§7.4)
            lifetime = self.db.execute(
                "SELECT COUNT(*) FROM signals WHERE hotkey=? AND status IN ('won','lost')",
                (hk,)).fetchone()[0]
            tw, td = self.db.execute(
                "SELECT SUM(status='won'), COUNT(*) FROM signals WHERE hotkey=? "
                "AND status IN ('won','lost') AND t0_unix >= ?", (hk, cutoff)).fetchone()
            states.append(scoring.MinerState(
                hotkey=hk, uid=uid, first_seen_unix=first_seen,
                lifetime_decisive=lifetime, trailing_wins=tw or 0,
                trailing_decisive=td or 0))

        w = scoring.compute_weights(states, now)
        uids, vals = list(w.keys()), list(w.values())
        ok = self.ch.set_weights(self.wallet, uids, vals)
        self._last_weights_block = block
        print(f"  → set_weights ok={ok} ({len(uids)} uids, "
              f"burn={w.get(config.BURN_UID, 0):.3f})")

    # ── loop ─────────────────────────────────────────────────────────────────
    def run(self):
        print(f"SN89 validator · netuid={config.NETUID} · network={config.NETWORK} "
              f"· db={config.DB_PATH}")
        while True:
            try:
                self.ingest()
                self.reveal()
                self.grade_revealed()
                self.maybe_set_weights()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"  loop error: {e}")
            time.sleep(config.POLL_INTERVAL_S)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    args = p.parse_args()
    Validator(bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)).run()


if __name__ == "__main__":
    main()
