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

import concurrent.futures
import json
import os
import sqlite3
import sys
import time

import bittensor as bt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import (bucket, chain, closers, competitions, config, crypto,
                          hf, hf_grade, replay, scoring)
from sn89_signals.grader import PENDING, grade
from sn89_signals.schema import Signal, ValidationError, validate
from timelock import Timelock

# §9.1 LF-side pair-lock feed cache. Far below the reveal delay, so a verdict is
# never decided on a feed that could have moved since the call being judged.
HF_LOCK_REFRESH_S = int(os.getenv("SN89_HF_LOCK_REFRESH_S", "60"))

# HF grading was coupled to the weight cycle (mecid1_weights → sync_and_grade),
# which only runs once a TEMPO (~72 min), so a resolved HF call sat pending for
# over an hour. mecid-0 already grades every poll and only SETS weights per tempo;
# HF now does the same. Throttled because sync_and_grade fetches published windows
# over the network — a few min of staleness in a warmup board is fine.
HF_GRADE_EVERY_S = int(os.getenv("SN89_HF_GRADE_EVERY_S", "120"))

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
  strikes INTEGER NOT NULL DEFAULT 0,
  eliminated_t0 REAL                   -- decisive t0 that crossed the floor
);
CREATE TABLE IF NOT EXISTS drand_cache (round INTEGER PRIMARY KEY, signature BLOB);
CREATE TABLE IF NOT EXISTS referrals (
  recruiter_hk      TEXT NOT NULL,
  recruit_hk        TEXT NOT NULL,
  commit_block      INTEGER NOT NULL,  -- consensus-exact inclusion block of sn89ref
  first_seen_block  INTEGER NOT NULL,  -- observational: block our poll saw it
  observed_unix     REAL NOT NULL,
  recruit_reg_block INTEGER,           -- filled ONCE at first metagraph sighting; never updated
  PRIMARY KEY (recruiter_hk, recruit_hk)
);
CREATE TABLE IF NOT EXISTS referral_transfers (
  from_hk          TEXT NOT NULL,     -- ORIGINAL recruiter (the signer)
  to_hk            TEXT NOT NULL,     -- referrer's own hotkey
  commit_block     INTEGER NOT NULL,  -- consensus-exact inclusion block
  first_seen_block INTEGER NOT NULL,
  observed_unix    REAL NOT NULL,
  PRIMARY KEY (from_hk, to_hk)
);
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
    def __init__(self, wallet: "bt.Wallet", cosign_wallet: "bt.Wallet | None" = None):
        self.wallet = wallet
        # Optional second hotkey that commits the IDENTICAL weight vector each
        # cycle (e.g. the subnet-owner hotkey UID0). Whichever of the two holds
        # a validator permit actually lands weights; the other is skipped.
        self.cosign_wallet = cosign_wallet
        self.ch = chain.Chain()
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.tlock = Timelock(config.DRAND_PUBLIC_KEY)
        self._last_weights_block = 0          # last SUCCESSFUL commit (TEMPO cadence)
        self._last_weights_attempt_block = 0  # last attempt (failed-retry backoff)
        self._last_scanned_block = 0   # recent-overwrite scan cursor (audit #5)
        self._hf_locks: dict = {}      # §9.1 LF-side pair lock (published HF logs)
        self._hf_locks_at = 0.0
        self._hf_graded_at = 0.0       # last HF sync_and_grade (decoupled from weights)
        self._closers_graded_at = 0.0  # last Closers sync_and_grade (same pattern)

    def _migrate(self):
        """Additive column migration for DBs created before commit_block/t0_ms."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(signals)")}
        for col, decl in (("commit_block", "INTEGER"), ("t0_ms", "INTEGER"),
                          ("is_copy", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")
        meta_cols = {r[1] for r in self.db.execute("PRAGMA table_info(hotkey_meta)")}
        for col, decl in (("eliminated_t0", "REAL"),):
            if col not in meta_cols:
                self.db.execute(f"ALTER TABLE hotkey_meta ADD COLUMN {col} {decl}")
        self.db.commit()

    # ── 1. ingest ────────────────────────────────────────────────────────────
    def _journal_commit(self, c: dict, block: int, now: float):
        """Journal one observed commitment (idempotent on commit_hex)."""
        if self.db.execute(
                "SELECT 1 FROM signals WHERE commit_hex=?", (c["commit"],)).fetchone():
            return
        hk = c["hotkey"]
        # T0 = the commitment's true inclusion block (docs/entry-timing.md §2.1)
        # — consensus-exact, identical on every validator regardless of poll
        # phase. first_seen_block stays as an observation log.
        commit_block = c.get("commit_block") or block
        t0_ms = self.ch.block_time_ms(commit_block)
        t0_unix = t0_ms / 1000.0
        # Judge the miner-supplied drand round against the commitment's own
        # inclusion-block time (NOT round_time(rnd)): a round outside the
        # commit+REVEAL_DELAY ±tolerance window is journaled VOID right here,
        # never left sealed. A far-future round would otherwise never mature —
        # sitting sealed forever and re-pulling its blob every loop — and the
        # reveal-time window check never reaches it. commit_block is consensus-
        # exact, so every validator voids the identical rows.
        status, void_reason = "sealed", None
        if not crypto.expected_round_ok(c["round"], t0_unix):
            status, void_reason = "void", "round_out_of_window"
        self.db.execute(
            "INSERT OR IGNORE INTO signals (commit_hex,hotkey,round,url_tag,"
            "first_seen_block,commit_block,t0_unix,t0_ms,status,void_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (c["commit"], hk, c["round"], c["url_tag"], block, commit_block,
             t0_unix, t0_ms, status, void_reason))
        self.db.execute(
            "INSERT OR IGNORE INTO hotkey_meta (hotkey, first_seen_unix) VALUES (?,?)",
            (hk, now))
        print(f"  + {status} {c['commit'][:12]}… {hk[:8]}… round={c['round']}"
              + (f" ({void_reason})" if void_reason else ""))

    def _journal_referral(self, c: dict, block: int, now: float):
        """Journal one observed referral claim (§ referral). Idempotent on the
        (recruiter, recruit) pair — INSERT OR IGNORE keeps the FIRST observed
        commit_block, so the earliest claim is canonical and a re-commit can't
        move it. Raw facts only; validity (lead blocks, one-recruiter-per-
        recruit, breadth cap, pair no-copy) is re-derived at replay time."""
        recruiter, recruit = c["hotkey"], c["recruit"]
        if recruiter == recruit:
            print(f"  ✗ referral rejected {recruiter[:8]}…: self-referral")
            return
        cur = self.db.execute(
            "INSERT OR IGNORE INTO referrals (recruiter_hk, recruit_hk, "
            "commit_block, first_seen_block, observed_unix) VALUES (?,?,?,?,?)",
            (recruiter, recruit, int(c.get("commit_block") or block), block, now))
        if cur.rowcount:
            print(f"  ⊕ referral {recruiter[:8]}… → {recruit[:8]}… "
                  f"commit_block={c.get('commit_block') or block}")

    def _journal_referral_transfer(self, c: dict, block: int, now: float):
        """Journal one observed sn89refx referral-base transfer. Raw facts only
        — INSERT OR IGNORE on (from, to) keeps the first-observed commit_block;
        which single transfer per from_hk actually COUNTS (earliest block, once
        ever, non-chaining) is re-derived at replay by
        scoring.apply_referral_transfers, so validator state cannot drift it."""
        frm, to = c["hotkey"], c["to"]
        if frm == to:
            print(f"  ✗ referral transfer rejected {frm[:8]}…: self-transfer")
            return
        cur = self.db.execute(
            "INSERT OR IGNORE INTO referral_transfers (from_hk, to_hk, "
            "commit_block, first_seen_block, observed_unix) VALUES (?,?,?,?,?)",
            (frm, to, int(c.get("commit_block") or block), block, now))
        if cur.rowcount:
            print(f"  ⇄ referral base transfer {frm[:8]}… → {to[:8]}… "
                  f"commit_block={c.get('commit_block') or block}")

    def refresh_referrals(self, mg):
        """Fill recruit_reg_block ONCE at the recruit's first metagraph sighting.

        BlockAtRegistration is per-UID-slot and latest-wins: re-reading it live
        would let an already-registered recruit deregister and re-register to
        launder a late referral into "registered after the commit". The first
        sighting is journaled and never updated — if the recruit was already
        registered when the referral appeared, the recorded block is < the
        referral's commit_block and the claim is permanently invalid at replay.
        """
        pend = self.db.execute(
            "SELECT recruiter_hk, recruit_hk FROM referrals "
            "WHERE recruit_reg_block IS NULL").fetchall()
        if not pend:
            return
        uid_by_hk = {hk: i for i, hk in enumerate(mg.hotkeys)}
        for recruiter, recruit in pend:
            uid = uid_by_hk.get(recruit)
            if uid is None:
                continue                       # not registered yet — keep waiting
            try:
                reg_block = int(mg.block_at_registration[uid])
            except (AttributeError, IndexError, TypeError, ValueError):
                continue                       # metagraph without the field — retry next cycle
            self.db.execute(
                "UPDATE referrals SET recruit_reg_block=? WHERE recruiter_hk=? "
                "AND recruit_hk=? AND recruit_reg_block IS NULL",
                (reg_block, recruiter, recruit))
            print(f"  ⊕ referral recruit sighted {recruit[:8]}… reg_block={reg_block}")
        self.db.commit()

    def ingest(self):
        block = self.ch.current_block()
        # Latest on-chain snapshot (one commitment per hotkey — latest wins).
        sources = list(self.ch.read_all_commitments_with_block().values())
        # OPTIONAL (audit #5): also scan the blocks since the last poll so a
        # commitment a miner OVERWROTE before we observed it isn't lost — the
        # root of the cross-validator split-brain. Off by default; the on-chain
        # event/extrinsic shape must be soaked on testnet before mainnet (see
        # chain.read_commitments_in_block_range). Dedup is by commit_hex, so the
        # union with the snapshot is safe.
        if config.SCAN_COMMITMENT_HISTORY:
            frm = self._last_scanned_block or (block - 1)
            try:
                sources += self.ch.read_commitments_in_block_range(frm, block)
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ commitment-history scan skipped: {e}")
            self._last_scanned_block = block
        now = time.time()
        for c in sources:
            if c.get("kind") == "referral":
                self._journal_referral(c, block, now)
            elif c.get("kind") == "referral_transfer":
                self._journal_referral_transfer(c, block, now)
            else:
                self._journal_commit(c, block, now)
        self.db.commit()

        # Fetch missing ciphertext blobs from R2_PUBLIC_BASE (the trust
        # boundary; the blob is encrypted and hash-checked regardless). Bounded
        # so a slow/malicious host can't stall the loop (audit #7): each fetch
        # has a hard deadline (bucket.fetch) and the batch runs in a small thread
        # pool under BLOB_FETCH_BUDGET_S. Whatever doesn't land this pass is
        # retried next loop — the on-chain commitment is durable. Only the main
        # thread touches the DB; workers just do network I/O.
        pending = self.db.execute(
            "SELECT commit_hex, hotkey, url_tag FROM signals "
            "WHERE blob_json IS NULL AND status='sealed'").fetchall()
        if pending:
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=config.BLOB_FETCH_WORKERS)
            futs = {ex.submit(self._find_blob, hk, tag): commit_hex
                    for commit_hex, hk, tag in pending}
            done, _ = concurrent.futures.wait(
                futs, timeout=config.BLOB_FETCH_BUDGET_S)
            for fut in done:
                try:
                    blob = fut.result()
                except Exception:  # noqa: BLE001
                    blob = None
                if blob is not None:
                    self.db.execute(
                        "UPDATE signals SET blob_json=? WHERE commit_hex=?",
                        (json.dumps(blob, separators=(",", ":")), futs[fut]))
            # Don't block on stragglers — abandon them (bucket.fetch's own
            # deadline bounds each worker, so they retire on their own).
            ex.shutdown(wait=False, cancel_futures=True)
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
            # Window check BEFORE the maturity gate. A round outside commit+24h
            # is void regardless of whether it has "matured", so a far-future
            # round can't slip past the `round_time(rnd) > now` continue and sit
            # sealed forever. (Ingest already voids these; this also catches any
            # row journaled before that guard shipped.)
            if not crypto.expected_round_ok(rnd, t0):
                self._void(commit_hex, "round_out_of_window", strike=False)
                continue
            if crypto.round_time(rnd) > now:
                continue
            # §: W_owner must be wrapped to the canonical owner key so the owner
            # retains real-time visibility. A blob wrapped to any other key is
            # opting out of that visibility — void it (deterministic, drand-
            # independent: every validator reads the same blob owner_pk). The
            # broken all-zero placeholder can't even encrypt (X25519 raises), so
            # any blob that arrives with a non-canonical owner_pk is deliberate.
            try:
                blob = json.loads(blob_json)
            except (ValueError, TypeError):
                self._void(commit_hex, "blob_unparseable", strike=False)
                continue
            if str(blob.get("owner_pk", "")).lower() != config.OWNER_PK_HEX.lower():
                self._void(commit_hex, "wrong_owner_pk", strike=True)
                continue
            sig_bytes = self._drand_sig(rnd)
            if sig_bytes is None:
                continue
            pt = crypto.decrypt_timelock(blob, sig_bytes, self.tlock)
            if pt is None or __import__("hashlib").sha256(pt).hexdigest() != commit_hex:
                # NO STRIKE: decrypt_timelock already tries every supported
                # timelock version (0.0.2 + legacy 0.0.1 fallback). A blob that
                # still will not open is AMBIGUOUS -- it may be an honest miner on
                # a timelock version we do not yet support, not an abuser. Void it
                # (it cannot count) but do not accrue a 3-strikes/30-day-zeroing
                # strike on that ambiguity. Withholding losers is already deterred
                # by the forfeit-LOSS path (forfeit_unrevealed); wrong_owner_pk (a
                # deliberate visibility opt-out) still strikes.
                self._void(commit_hex, "decrypt_or_hash_mismatch", strike=False)
                continue
            try:
                # Validate against the board AS OF the commit block (t0), so a
                # band update between commit and reveal can't void a signal that
                # was valid when it was committed (the README's in-flight promise).
                parsed = validate(Signal.from_bytes(pt), t0_unix=t0)
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
        for commit_hex, hk, rnd, t0 in self.db.execute(
                "SELECT commit_hex, hotkey, round, t0_unix FROM signals "
                "WHERE status='sealed' AND blob_json IS NULL").fetchall():
            # A round outside the commit+24h window never matures into a real
            # forfeit deadline — void it instead of re-pulling its blob forever
            # (covers legacy rows journaled before the ingest-time guard).
            if not crypto.expected_round_ok(rnd, t0):
                self._void(commit_hex, "round_out_of_window", strike=False)
                continue
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
    def _hf_lock_index(self, now: float) -> dict:
        """§9.1 pair lock, LF side — HF submissions inside the rolling horizon.

        Built from the PUBLISHED windows, never from our local ingest dir, so an
        LF void is reproducible by anyone replaying the journal.

        Cached for HF_LOCK_REFRESH_S. Staleness cannot change a verdict: an LF
        call is judged at its own t0, at least a reveal delay in the past, while
        the freshest HF submit this could miss is under a minute old.

        Propagates HFLockFeedError — the caller must not turn a dead feed into an
        empty index. That equivalence is what left this rule unenforced.
        """
        if time.time() - self._hf_locks_at <= HF_LOCK_REFRESH_S:
            return self._hf_locks
        rows = hf_grade.load_hf_lock_rows(hf.HF_PUBLIC_BASE,
                                          int(now * 1000) - hf.PAIR_LOCK_MS)
        self._hf_locks = hf.build_lock_index(rows)
        self._hf_locks_at = time.time()
        return self._hf_locks

    def grade_hf(self):
        """Resolve HF (mecid-1) calls whose horizon has elapsed, decoupled from
        the weight cycle. Fills the same grade cache mecid1_weights reads, so the
        next weight commit is computed off an already-current board rather than
        having to grade a TEMPO's worth of backlog in one shot. Best-effort:
        never let an HF grading hiccup disturb the mecid-0 loop."""
        if time.time() - self._hf_graded_at < HF_GRADE_EVERY_S:
            return
        self._hf_graded_at = time.time()
        try:
            cache_dir = os.path.expanduser(
                os.getenv("SN89_HF_GRADE_CACHE", "~/.sn89/hf-grade"))
            hf_grade.sync_and_grade(hf.HF_PUBLIC_BASE, cache_dir, time.time())
        except Exception as e:  # noqa: BLE001 — HF must never break mecid-0
            print(f"  ! HF grade skipped (mecid-0 unaffected): {e}")

    def grade_closers(self):
        """Resolve Closers calls whose horizon has elapsed — same decoupled
        pattern as grade_hf, same isolation guarantee: a Closers hiccup never
        disturbs LF/HF. Reads the SAME published windows the HF grader syncs
        (closers receipts live in the same anchored logs)."""
        if time.time() - self._closers_graded_at < HF_GRADE_EVERY_S:
            return
        self._closers_graded_at = time.time()
        try:
            cache_dir = os.path.expanduser(
                os.getenv("SN89_CLOSERS_GRADE_CACHE", "~/.sn89/closers-grade"))
            closers.sync_and_grade(hf.HF_PUBLIC_BASE, cache_dir, time.time())
        except Exception as e:  # noqa: BLE001
            print(f"  ! Closers grade skipped (LF/HF unaffected): {e}")

    def grade_revealed(self):
        # ── §9.1 cross-mechanism pair lock, LF side ──────────────────────────
        # The pair is only knowable once revealed, so this cannot run at ingest
        # the way the HF side does; the spec puts it here and the only available
        # action is a void. apply_validity_filters judges each row at its OWN
        # t0 against config.pair_lock_lf_enforced_as_of, so arming today cannot
        # reach back and void calls committed while the rule was unenforced.
        hf_locks = None
        if config.PAIR_LOCK_LF_FROM:
            try:
                hf_locks = self._hf_lock_index(time.time())
            except hf.HFLockFeedError as e:
                # Do NOT fall through with hf_locks=None: that is indistinguishable
                # from "no HF calls exist" and would grade the whole journal with
                # the lock silently off. Skip the cycle instead — grading is
                # idempotent and re-runs from scratch every cycle, so nothing is
                # lost by deferring, and a persistent failure stays loud.
                print(f"  ! HF lock feed unreadable — grade cycle SKIPPED so the "
                      f"pair lock is never silently off: {e}")
                return

        # deterministic validity pass over everything revealed/graded
        rows = []
        for commit_hex, hk, t0, pt, status in self.db.execute(
                "SELECT commit_hex, hotkey, t0_unix, plaintext, status FROM signals "
                "WHERE status IN ('revealed','pending','won','lost','washed') "
                "AND plaintext IS NOT NULL").fetchall():  # forfeit losses have no plaintext
            s = Signal.from_bytes(pt.encode())
            rows.append((commit_hex, s, scoring.GradedRow(
                hotkey=hk, trade_pair=s.trade_pair, direction=s.direction,
                t0_unix=t0, status="ok" if status in ("revealed", "pending") else status,
                horizon_h=config.horizon_h_for(s.trade_pair, t0))))
        filtered = scoring.apply_validity_filters([r for _, _, r in rows],
                                                  hf_locks=hf_locks)
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
            # Re-apply the GOVERNED band before grading. schema.validate() already
            # normalises tp/sl to the board row as-of t0 when the blob is revealed,
            # but the reveal path stores the miner's RAW plaintext -- it is hash-bound
            # to the commitment and must stay verbatim -- so that normalisation was
            # discarded and never reached the grader. Without this the TP/SL brackets
            # are built from the miner's payload, so a hotkey running a stale vendored
            # board is scored against its OWN bands instead of the governed board's.
            # SKIP (never void) on failure: this payload already passed validate() at
            # reveal, so a raise here means a transient board-resolution problem, and
            # voiding on that would destroy good signals. It retries next cycle.
            try:
                validate(s, t0_unix=t0_ms / 1000.0)
            except Exception as e:  # noqa: BLE001
                print(f"  ! band-normalise skip {commit_hex[:12]}… {e}")
                continue
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
    def refresh_eliminations(self):
        """Mark hotkeys whose graded journal crossed the elimination floor
        (scoring.elimination_t0 — deterministic, so every validator agrees).
        Marking zeroes the hotkey permanently.
        """
        for (hk,) in self.db.execute(
                "SELECT hotkey FROM hotkey_meta WHERE eliminated_t0 IS NULL").fetchall():
            decisive = [(t0, status == "won") for t0, status in self.db.execute(
                "SELECT t0_unix, status FROM signals WHERE hotkey=? "
                "AND status IN ('won','lost')", (hk,)).fetchall()]
            t0 = scoring.elimination_t0(decisive)
            if t0 is not None:
                self.db.execute(
                    "UPDATE hotkey_meta SET eliminated_t0=? WHERE hotkey=?", (t0, hk))
                print(f"  ☠ eliminated {hk[:8]}… floor crossed at t0={t0:.0f}")
        self.db.commit()

    def _weights_due(self, block: int) -> bool:
        """Whether to (re)attempt set_weights now. A fresh weight cycle runs
        every TEMPO after the last SUCCESS; but after a FAILED attempt we wait
        WEIGHTS_RETRY_BLOCKS before retrying rather than hammering every poll —
        commit-reveal weights are rate-limited, so a faster retry only piles up
        unrevealed commits (TooManyUnrevealedCommits) without landing sooner.
        (The faster fixed-cadence poll loop makes the every-poll retry especially
        wasteful.)"""
        if block - self._last_weights_block < config.TEMPO:
            return False
        if block - self._last_weights_attempt_block < config.WEIGHTS_RETRY_BLOCKS:
            return False
        return True

    def maybe_set_weights(self):
        block = self.ch.current_block()
        if not self._weights_due(block):
            return
        self._last_weights_attempt_block = block
        self.refresh_eliminations()
        mg = self.ch.metagraph()
        uid_by_hotkey = {hk: i for i, hk in enumerate(mg.hotkeys)}
        self.refresh_referrals(mg)

        # A hotkey with no validator permit can commit weights, but the chain
        # never reveals them — they pile up until `TooManyUnrevealedCommits` and
        # the validator earns nothing. Commit only from permitted hotkeys; if
        # neither the primary nor the cosign hotkey has a permit, skip and say why.
        signers = [w for w in (self.wallet, self.cosign_wallet) if w is not None
                   and self.ch.has_validator_permit(w.hotkey.ss58_address, mg=mg)]
        if not signers:
            hks = [w.hotkey.ss58_address for w in (self.wallet, self.cosign_wallet) if w]
            print(f"  ⚠️  skipping weights: no validator permit on netuid "
                  f"{config.NETUID} for any signer {hks} — commits "
                  f"would never reveal. Stake a hotkey to earn a permit.")
            self._last_weights_block = block  # throttle this warning to once per tempo
            return

        now = time.time()

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
                t0_unix=t0, status=status, horizon_h=config.horizon_h_for(s.trade_pair, t0))
            copy_rows.append(gr)
            by_commit.append((commit_hex, gr))

        # Eligible copy-leaders (anti-grief, §7.5): only a hotkey with a real
        # track record (≥ COPY_LEADER_MIN_DECISIVE decisive outcomes), not
        # eliminated, and not holding both directions of a pair at once, can make
        # a LATER entrant a copier. This stops a zero-history griefer from
        # manufacturing copy-flags against honest miners by occupying (both sides
        # of) a pair — the 24h timelock makes live copying impossible, so mere
        # overlap is not evidence that anyone copied the griefer.
        decisive_counts = dict(self.db.execute(
            "SELECT hotkey, COUNT(*) FROM signals WHERE status IN ('won','lost') "
            "GROUP BY hotkey").fetchall())
        eliminated_hk = {h for (h,) in self.db.execute(
            "SELECT hotkey FROM hotkey_meta WHERE eliminated_t0 IS NOT NULL").fetchall()}
        both_dir = (scoring.both_direction_spammers(copy_rows)
                    if config.COPY_EXCLUDE_BOTH_DIR else set())
        eligible_leaders = {
            hk for hk, n in decisive_counts.items()
            if n >= config.COPY_LEADER_MIN_DECISIVE
            and hk not in eliminated_hk and hk not in both_dir}
        for hk in sorted(both_dir):
            print(f"  ⚠ both-direction spammer {hk[:8]}… barred as copy-leader")

        # PRIMARY: mark the later entrant on each live identical trade, persist
        # is_copy. The scoring SQL below then declines to credit a copied win.
        scoring.mark_copies(copy_rows, eligible_leaders=eligible_leaders)
        for commit_hex, gr in by_commit:
            self.db.execute("UPDATE signals SET is_copy=? WHERE commit_hex=?",
                            (int(gr.is_copy), commit_hex))

        # SECONDARY: 30-day shadowing report (report-only unless COPY_ZERO_WEIGHT).
        reports = scoring.detect_copiers(copy_rows, now, eligible_leaders=eligible_leaders)
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

        # The weight vector is the SAME pure replay any auditor runs over the
        # published journal (sn89_signals/replay.py) — so the validator's on-chain
        # weights are verifiable by anyone (the single-validator trust model,
        # docs/single-validator-model.md). Hand it the journal and let it re-derive
        # eliminations, copy penalty, gate, tier and emission identically; the copy
        # forensics above persist `is_copy`/`copier_flags` only for the dashboard.
        sig_rows = [
            {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st,
             "is_copy": int(cp or 0), "plaintext": pt}
            for ch, hk, t0, st, cp, pt in self.db.execute(
                "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext "
                "FROM signals")]
        meta = {hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
                for hk, fs, sk in self.db.execute(
                    "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")}
        referral_rows = [
            {"recruiter_hk": r, "recruit_hk": c, "commit_block": cb,
             "recruit_reg_block": rb}
            for r, c, cb, rb in self.db.execute(
                "SELECT recruiter_hk, recruit_hk, commit_block, recruit_reg_block "
                "FROM referrals")]
        w = replay.weights_from_journal(sig_rows, meta, uid_by_hotkey, now,
                                        referrals=referral_rows)

        # ── unified multi-competition path (competitions.py) ────────────────
        # LF/HF/Closers each produce their own normalized vector; the blend is
        # committed as the SINGLE mecid-0 vector. A competition that fails to
        # compute burns its own share (never redistributed). Dark by default —
        # config.COMBINED_WEIGHTS gates it, so mainnet is byte-identical until
        # the coordinated flip.
        if config.combined_weights_active(now):
            vectors: dict = {"lf": w}
            try:
                vectors["hf"] = hf_grade.mecid1_weights(uid_by_hotkey, now)
            except Exception as e:  # noqa: BLE001 — dead competition burns its share
                print(f"  ! HF vector failed (share burns): {e}")
                vectors["hf"] = None
            try:
                # "Qualified in HF or LF" = currently earning weight in either
                # vector (excluding burn) — the closers sybil gate. Testnet
                # disables the gate via SN89_CLOSERS_REQUIRE_QUALIFIED=0.
                hk_by_uid = {u: h for h, u in uid_by_hotkey.items()}
                qual = {hk_by_uid[u]
                        for vec in (w, vectors.get("hf") or {})
                        for u, wt in vec.items()
                        if wt > 0 and u != config.BURN_UID and u in hk_by_uid}
                vectors["closers"] = closers.closers_weights(
                    uid_by_hotkey, now, qualified_hks=qual)
            except Exception as e:  # noqa: BLE001
                print(f"  ! Closers vector failed (share burns): {e}")
                vectors["closers"] = None
            shares = config.comp_weights_as_of(now)
            w = competitions.combine(vectors, shares)
            parts = " ".join(
                f"{k}={'∅' if vectors.get(k) is None else len([u for u, x in (vectors[k] or {}).items() if x > 0 and u != config.BURN_UID])}"
                for k in shares)
            print(f"  ⊕ combined weights shares={shares} "
                  f"earners: {parts} burn={w.get(config.BURN_UID, 0):.3f}")

        uids, vals = list(w.keys()), list(w.values())
        # Every permitted signer commits the SAME vector this cycle. Advance the
        # per-tempo throttle only when at least one commit actually landed.
        # Treating a failed extrinsic as done would throttle the retry by a full
        # TEMPO (~72 min); instead retry on the next poll and surface the chain's
        # rejection reason so the failure is diagnosable rather than silent.
        any_ok = False
        for sw in signers:
            ok, msg = self.ch.set_weights(sw, uids, vals)
            any_ok = any_ok or ok
            print(f"  → set_weights[{sw.hotkey.ss58_address[:8]}…] ok={ok} "
                  f"({len(uids)} uids, burn={w.get(config.BURN_UID, 0):.3f}, "
                  f"copiers_zeroed={len(excluded_uids)})"
                  + (f" — {msg}" if msg else ""))
            if not ok:
                print(f"  ! set_weights rejected for {sw.hotkey.ss58_address[:8]}… "
                      f"— will retry next poll. reason: "
                      f"{msg or 'no message returned by SDK'}")
        if any_ok:
            self._last_weights_block = block

        # ── mechanism 1 (HF) — same signers, same tempo, graded from PUBLIC logs ──
        # Wrapped so ANY HF failure is isolated: mecid 0 already committed above and
        # is never affected. Graded off HF_PUBLIC_BASE, so every validator computes
        # the identical vector (replayable, same as mecid 0). All-burn until an HF
        # miner clears the 8-decisive gate.
        if config.HF_MECID1_WEIGHTS:
            try:
                from sn89_signals import hf as _hf, hf_grade as _hfg
                if config.combined_weights_active(now):
                    if config.REFERRER_MECID1:
                        # § referrer mechanism: the freed slot pays the
                        # referrer class — pure replay, same trust model.
                        transfer_rows = [
                            {"from_hk": f, "to_hk": t, "commit_block": cb}
                            for f, t, cb in self.db.execute(
                                "SELECT from_hk, to_hk, commit_block "
                                "FROM referral_transfers")]
                        hw = replay.referrer_weights_from_journal(
                            sig_rows, meta, uid_by_hotkey, now,
                            referrals=referral_rows,
                            referral_transfers=transfer_rows)
                    else:
                        # merge-era parking: all-burn while the split ramps.
                        # Committing the HF vector here would DOUBLE-pay HF.
                        hw = {config.BURN_UID: 1.0}
                else:
                    hw = _hfg.mecid1_weights(uid_by_hotkey, now)
                huids, hvals = list(hw.keys()), list(hw.values())
                for sw in signers:
                    ok, msg = self.ch.set_mechanism_weights(sw, _hf.MECID_1, huids, hvals)
                    print(f"  ⚡ set_mechanism_weights[mecid1][{sw.hotkey.ss58_address[:8]}…] "
                          f"ok={ok} ({len(huids)} uids, burn={hw.get(config.BURN_UID, 0):.3f})"
                          + (f" — {msg}" if msg else ""))
            except Exception as e:  # noqa: BLE001 — never let HF break mecid-0
                print(f"  ! mecid-1 weights skipped (mecid-0 unaffected): {e}")

    # ── loop ─────────────────────────────────────────────────────────────────
    def run(self):
        print(f"SN89 validator · netuid={config.NETUID} · network={config.NETWORK} "
              f"· db={config.DB_PATH}")
        for w_ in (self.wallet, self.cosign_wallet):
            if w_ is None:
                continue
            hk = w_.hotkey.ss58_address
            permit = self.ch.has_validator_permit(hk)
            if permit is None:
                print(f"  ⚠️  hotkey {hk} is not registered on netuid {config.NETUID}.")
            elif not permit:
                print(f"  ⚠️  hotkey {hk} holds NO validator permit — weight commits "
                      f"will never reveal until it is staked into the validator set.")
        while True:
            cycle_start = time.monotonic()
            try:
                self.ingest()
                self.reveal()
                self.forfeit_unrevealed()
                self.grade_revealed()
                self.grade_hf()
                self.grade_closers()
                self.maybe_set_weights()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"  loop error: {e}")
            # FIXED cadence: sleep only the REMAINDER of POLL_INTERVAL_S so the
            # poll period tracks wall-clock instead of ballooning with the loop's
            # own work time. A longer period widens the window in which a miner
            # can overwrite a commitment before any validator observes the prior
            # one (audit #5); pinning the cadence keeps that window tight and
            # uniform across validators. If work overran the interval, poll again
            # immediately (no negative sleep).
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, config.POLL_INTERVAL_S - elapsed))


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    p.add_argument("--cosign.name", dest="cosign_name", default=None,
                   help="optional second wallet that co-signs the same weights")
    p.add_argument("--cosign.hotkey", dest="cosign_hotkey", default="default")
    args = p.parse_args()
    cosign = (bt.Wallet(name=args.cosign_name, hotkey=args.cosign_hotkey)
              if args.cosign_name else None)
    Validator(bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey),
              cosign_wallet=cosign).run()


if __name__ == "__main__":
    main()
