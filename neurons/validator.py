#!/usr/bin/env python3
"""SN89 Signals validator.

Loop (every POLL_INTERVAL_S):
  1. INGEST    — read all sn89 commitments (raw CommitmentOf storage, which
                 carries the exact inclusion block = canonical T0); journal NEW
                 (hotkey, commit) pairs; fetch + store ciphertext blobs whose
                 url_tag matches.
  2. REVEAL    — for journaled rows whose drand round has matured: fetch the
                 round signature, decrypt W_time, verify SHA256(pt) == commit
                 and the round window (§5); parse + structurally validate.
  3. GRADE     — run validity filters (§6.4) over the revealed set, then
                 walk-forward touch-grade decisive outcomes from Polygon.
  4. WEIGHTS   — every tempo: gate → tier-weighted pro-rata trailing-30d wins → set_weights.

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
  first_seen_block INTEGER NOT NULL,   -- observational: block our poll saw it
  commit_block INTEGER,                -- consensus-exact: inclusion block of set_commitment
  t0_unix      REAL NOT NULL,
  t0_ms        INTEGER,                -- ms-precise T0 from commit_block's Timestamp pallet
  blob_json    TEXT,
  plaintext    TEXT,
  status       TEXT NOT NULL DEFAULT 'sealed',
    -- sealed | revealed | void | pending | won | lost | washed
    -- (lost + exit_reason='no_reveal' = forfeit: committed, blob never served)
  void_reason  TEXT,
  entry_price  REAL,
  outcome_bps  REAL,
  exit_reason  TEXT,
  exit_at_ms   INTEGER,
  is_copy      INTEGER NOT NULL DEFAULT 0   -- §7.5: opened a live identical trade after another hotkey
);
CREATE INDEX IF NOT EXISTS idx_signals_hotkey ON signals(hotkey);
CREATE TABLE IF NOT EXISTS hotkey_meta (
  hotkey TEXT PRIMARY KEY,
  first_seen_unix REAL NOT NULL,
  strikes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drand_cache (round INTEGER PRIMARY KEY, signature BLOB);
CREATE TABLE IF NOT EXISTS copier_flags (
  follower      TEXT NOT NULL,
  leader        TEXT NOT NULL,
  sharp_events  INTEGER NOT NULL,
  soft_events   INTEGER NOT NULL,
  flagged       INTEGER NOT NULL,   -- 1 = sharp copier (zero weight)
  low_diversity INTEGER NOT NULL,   -- 1 = soft shadowing (report-only)
  updated_unix  REAL NOT NULL,
  PRIMARY KEY (follower, leader)
);
"""


class Validator:
    def __init__(self, wallet: "bt.Wallet"):
        self.wallet = wallet
        self.ch = chain.Chain()
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.tlock = Timelock(config.DRAND_PUBLIC_KEY)
        self._last_weights_block = 0

    def _migrate(self):
        """Additive column migration for DBs created before commit_block/t0_ms."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(signals)")}
        for col, decl in (("commit_block", "INTEGER"), ("t0_ms", "INTEGER"),
                          ("is_copy", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")
        self.db.commit()

    # ── 1. ingest ────────────────────────────────────────────────────────────
    def ingest(self):
        block = self.ch.current_block()
        commits = self.ch.read_all_commitments_with_block()
        now = time.time()
        for hk, c in commits.items():
            row = self.db.execute(
                "SELECT 1 FROM signals WHERE commit_hex=?", (c["commit"],)).fetchone()
            if row:
                continue
            # T0 = the commitment's true inclusion block (docs/entry-timing.md
            # §2.1) — consensus-exact, identical on every validator regardless
            # of poll phase. first_seen_block stays as an observation log.
            commit_block = c["commit_block"] or block
            t0_ms = self.ch.block_time_ms(commit_block)
            self.db.execute(
                "INSERT OR IGNORE INTO signals (commit_hex,hotkey,round,url_tag,"
                "first_seen_block,commit_block,t0_unix,t0_ms) VALUES (?,?,?,?,?,?,?,?)",
                (c["commit"], hk, c["round"], c["url_tag"], block, commit_block,
                 t0_ms / 1000.0, t0_ms))
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

    def forfeit_unrevealed(self):
        """§6.4 forfeit loss. A committed signal whose ciphertext blob never
        becomes gradeable is recorded as a LOST decisive outcome — not a void,
        not a free pass.

        Without this the 24h timelock is a costless option: the miner already
        knows their own signal, so they can commit at T0, watch the market, and
        publish the blob only for trades that won — withholding losers earns no
        strike (a strike fires only on a *fetched* blob that fails decrypt/hash,
        never on one that was never served). Forcing a loss makes withholding
        cost exactly what revealing would, so there is nothing to game.

        Fires only once the drand round has matured AND REVEAL_GRACE_S has since
        elapsed AND we never captured the blob (blob_json IS NULL). A blob the
        validator already fetched is pinned in the journal and grades normally
        even if the miner later deletes it from their bucket; a validator-side
        drand/decrypt hiccup leaves blob_json non-NULL and is never mistaken for
        a forfeit. Deterministic in (round, t0, capture state) so every
        validator reaches the identical verdict.
        """
        now = time.time()
        for commit_hex, hk, rnd in self.db.execute(
                "SELECT commit_hex, hotkey, round FROM signals "
                "WHERE status='sealed' AND blob_json IS NULL").fetchall():
            if now < crypto.round_time(rnd) + config.REVEAL_GRACE_S:
                continue
            self.db.execute(
                "UPDATE signals SET status='lost', outcome_bps=NULL, "
                "exit_reason='no_reveal' WHERE commit_hex=?", (commit_hex,))
            print(f"  ⊗ forfeit {commit_hex[:12]}… {hk[:8]}… "
                  f"blob never served by reveal+grace ⇒ LOST")
        self.db.commit()

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
                "WHERE status IN ('revealed','pending','won','lost','washed') "
                "AND plaintext IS NOT NULL").fetchall():  # forfeit losses have no plaintext
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
        for commit_hex, hk, t0_ms, pt in self.db.execute(
                "SELECT commit_hex, hotkey, "
                "COALESCE(t0_ms, CAST(t0_unix * 1000 AS INTEGER)), plaintext "
                "FROM signals WHERE status IN ('revealed','pending')").fetchall():
            s = Signal.from_bytes(pt.encode())
            g = grade(s, t0_ms, now_ms)
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

        # ── copy penalty + forensics (§7.5) ────────────────────────────────────
        # Build every non-void commit in the copy window once; carry the real
        # status + horizon so mark_copies can compute live-trade overlap.
        copy_rows, by_commit = [], []
        for commit_hex, hk, t0, status, pt in self.db.execute(
                "SELECT commit_hex, hotkey, t0_unix, status, plaintext FROM signals "
                "WHERE status != 'void' AND plaintext IS NOT NULL "
                "AND t0_unix >= ?", (now - config.COPY_WINDOW_S,)).fetchall():
            s = Signal.from_bytes(pt.encode())
            gr = scoring.GradedRow(
                hotkey=hk, trade_pair=s.trade_pair, direction=s.direction,
                t0_unix=t0, status=status, horizon_h=s.horizon_h)
            copy_rows.append(gr)
            by_commit.append((commit_hex, gr))

        # PRIMARY: mark the later entrant on each live identical trade, persist
        # is_copy. The scoring SQL below then declines to credit a copied win.
        scoring.mark_copies(copy_rows)
        for commit_hex, gr in by_commit:
            self.db.execute("UPDATE signals SET is_copy=? WHERE commit_hex=?",
                            (int(gr.is_copy), commit_hex))

        # SECONDARY: 30-day shadowing report (report-only unless COPY_ZERO_WEIGHT).
        reports = scoring.detect_copiers(copy_rows, now)
        self.db.execute("DELETE FROM copier_flags")
        for follower, rs in reports.items():
            for r in rs:
                self.db.execute(
                    "INSERT OR REPLACE INTO copier_flags (follower,leader,"
                    "sharp_events,soft_events,flagged,low_diversity,updated_unix) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r.follower, r.leader, r.sharp_events, r.soft_events,
                     int(r.flagged), int(r.low_diversity), now))
        self.db.commit()
        flagged_hk = scoring.flagged_copier_hotkeys(reports) if config.COPY_ZERO_WEIGHT else set()
        excluded_uids = {uid_by_hotkey[h] for h in flagged_hk if h in uid_by_hotkey}
        for follower, rs in reports.items():
            for r in rs:
                tag = "⛔ COPIER(zeroed)" if (r.flagged and config.COPY_ZERO_WEIGHT) else (
                    "⚠ repeat-copier" if r.flagged else "· low-diversity")
                print(f"  {tag}: {follower[:8]}… shadows {r.leader[:8]}… "
                      f"(sharp={r.sharp_events} soft={r.soft_events})")

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
            won_all, won_orig, copies, td = self.db.execute(
                "SELECT SUM(status='won'), "
                "SUM(status='won' AND COALESCE(is_copy,0)=0), "
                "SUM(COALESCE(is_copy,0)), COUNT(*) "
                "FROM signals WHERE hotkey=? AND status IN ('won','lost') "
                "AND t0_unix >= ?", (hk, cutoff)).fetchone()
            td = td or 0
            # only a habitual copier loses credit for its copied wins; an honest
            # occasional second-lander keeps them (COPY_PENALTY="loss").
            habitual = scoring.is_habitual_copier(copies or 0, td)
            tw = (won_orig if habitual else won_all) or 0
            if habitual:
                print(f"  ⛔ habitual copier {hk[:8]}…: {copies}/{td} trades "
                      f"copied → {won_all - tw} wins stripped")
            states.append(scoring.MinerState(
                hotkey=hk, uid=uid, first_seen_unix=first_seen,
                lifetime_decisive=lifetime, trailing_wins=tw,
                trailing_decisive=td))

        w = scoring.compute_weights(states, now, excluded_uids=excluded_uids)
        uids, vals = list(w.keys()), list(w.values())
        ok = self.ch.set_weights(self.wallet, uids, vals)
        self._last_weights_block = block
        print(f"  → set_weights ok={ok} ({len(uids)} uids, "
              f"burn={w.get(config.BURN_UID, 0):.3f}, "
              f"copiers_zeroed={len(excluded_uids)})")

    # ── loop ─────────────────────────────────────────────────────────────────
    def run(self):
        print(f"SN89 validator · netuid={config.NETUID} · network={config.NETWORK} "
              f"· db={config.DB_PATH}")
        while True:
            try:
                self.ingest()
                self.reveal()
                self.forfeit_unrevealed()
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
