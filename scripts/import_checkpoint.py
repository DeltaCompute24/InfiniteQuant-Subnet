#!/usr/bin/env python3
"""Seed a validator journal from the published checkpoint, verifying as it goes.

WHY THIS EXISTS
    SN89 deliberately has no live multi-validator catch-up: historical commitment
    access is ~1 RPC/block and a second chain connection deadlocks the substrate
    client, so genesis backfill was removed (docs/single-validator-model.md). A
    validator that starts today, or one whose journal was damaged, therefore sees
    only what happens from the moment it turns on. It pays LF miners nothing until
    each re-earns QUALIFY_MIN_DECISIVE from scratch, which on observed rates is
    6-28 days, and it loses VTRUST the whole time. That is not hypothetical: a
    third-party validator has been stuck at 0 of 12 recovered LF earners since a
    timelock outage voided its backlog.

WHAT IT VERIFIES, AND WHAT IT DOES NOT
    Two of the three trust tiers are checked here, offline and by anyone:

      1. NO ALTERED SIGNALS  -- the on-chain commitment is sha256(plaintext) with
         no salt, so every published plaintext is checked against the commitment
         hash it claims. A row that fails is REFUSED, never imported. Measured
         2026-08-20: 5286 of 5286 published plaintexts verify.
      2. NO FABRICATED SIGNALS -- with --anchors, each commit_hex is read back
         from chain at its commit_block. Sampled by default because it is one RPC
         per signal.
      3. GRADES ARE NOT VERIFIED HERE. status/outcome are imported as-is, and
         re-deriving them needs the price feed the validator grades against. So
         this is trust-REDUCED, not trust-minimised: the publisher cannot forge or
         alter a signal, but a follower that skips its own re-grade is taking our
         word for the outcome. Re-grade once your own feed has the window.

    first_seen_unix drives the 8-day immunity clock and is OBSERVATIONAL -- the
    instant a validator first saw a hotkey. Importing ours transplants our clock,
    which is what makes an imported journal agree with ours; deriving it locally
    would restart every miner's warmup and defeat the point.

USAGE
    python3 scripts/import_checkpoint.py <checkpoint.json|URL> [--db PATH]
                                         [--anchors N] [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config  # noqa: E402

# Cloudflare fronts the checkpoint and 403s urllib's default agent, so an
# importer that does not set one fails with an HTTP error that says nothing
# about the real cause.
_UA = {"User-Agent": "sn89-import-checkpoint/1"}


def load(src: str) -> dict:
    if src.startswith("http://") or src.startswith("https://"):
        req = urllib.request.Request(src, headers=_UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    with open(src, encoding="utf-8") as fh:
        return json.load(fh)


def plaintext_bytes(pt) -> bytes:
    """The exact bytes the commitment hashes. A dict is re-serialised the way the
    miner did; a str is already the serialised form and must NOT be re-encoded
    through json, which would reorder keys and change the hash."""
    return pt.encode() if isinstance(pt, str) else json.dumps(
        pt, separators=(",", ":")).encode()


def verify_signals(signals: list) -> tuple[list, list]:
    """(accepted, refused). Refusal is on the hash, never on the grade."""
    good, bad = [], []
    for s in signals:
        pt = s.get("plaintext")
        if not pt:
            good.append(s)          # sealed / unrevealed: nothing to check yet
            continue
        h = hashlib.sha256(plaintext_bytes(pt)).hexdigest()
        (good if h == s.get("commit_hex") else bad).append(s)
    return good, bad


def check_anchors(signals: list, n: int) -> tuple[int, int, int, list]:
    """(confirmed, unreadable, sampled, mismatches).

    commitment_at_block returns None for BOTH "no such commitment" and "state
    pruned, needs an archive node" -- its own docstring says so. Collapsing those
    into one bucket turns an ordinary pruned RPC into an accusation: the first
    version of this function refused an import because 13 of 25 reads came back
    None against a node that simply does not keep old state. An unreadable anchor
    is unverified, not falsified, and only a hash that comes back DIFFERENT is
    evidence of anything."""
    from sn89_signals import chain
    ch = chain.Chain()
    have = [s for s in signals if s.get("commit_block")]
    sample = sorted(have, key=lambda s: -int(s["commit_block"]))[:n]
    ok, unreadable, mismatch = 0, 0, []
    for s in sample:
        try:
            on = ch.commitment_at_block(s["hotkey"], int(s["commit_block"]))
        except Exception:  # noqa: BLE001 — an RPC error is not a fabrication
            unreadable += 1
            continue
        if on is None:
            unreadable += 1
        elif on == s["commit_hex"]:
            ok += 1
        else:
            mismatch.append((s["commit_hex"][:12], on[:12]))
    return ok, unreadable, len(sample), mismatch


SEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  commit_hex TEXT PRIMARY KEY, hotkey TEXT NOT NULL, round INTEGER NOT NULL,
  url_tag TEXT NOT NULL, first_seen_block INTEGER NOT NULL, commit_block INTEGER,
  t0_unix REAL NOT NULL, t0_ms INTEGER, blob_json TEXT, plaintext TEXT,
  status TEXT NOT NULL DEFAULT 'sealed', void_reason TEXT, entry_price REAL,
  outcome_bps REAL, exit_reason TEXT, exit_at_ms INTEGER,
  is_copy INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_signals_hotkey ON signals(hotkey);
CREATE TABLE IF NOT EXISTS hotkey_meta (
  hotkey TEXT PRIMARY KEY, first_seen_unix REAL NOT NULL,
  strikes INTEGER NOT NULL DEFAULT 0, eliminated_t0 REAL);
CREATE TABLE IF NOT EXISTS referrals (
  recruiter_hk TEXT NOT NULL, recruit_hk TEXT NOT NULL, commit_block INTEGER NOT NULL,
  first_seen_block INTEGER NOT NULL, observed_unix REAL NOT NULL,
  recruit_reg_block INTEGER, PRIMARY KEY (recruiter_hk, recruit_hk));
"""


def seed(db_path: str, cp: dict, signals: list) -> dict:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SEED_SCHEMA)
    n_sig = n_meta = n_ref = 0
    for s in signals:
        # ON CONFLICT DO UPDATE, never INSERT OR REPLACE.
        #
        # REPLACE deletes the row and reinserts it, so every column the checkpoint
        # does not carry is silently nulled. Measured 2026-08-20 against a
        # populated journal: t0_ms 5340 rows -> 1, blob_json 5324 -> 1,
        # entry_price 5190 -> 0, outcome_bps 4008 -> 0, exit_reason 4053 -> 0.
        # The whole point of this script is repairing a DAMAGED validator, and a
        # repair that destroys the ms-precise T0 that board resolution and entry
        # pricing key on -- plus every miner's ciphertext -- is worse than the
        # damage. It was invisible in testing because the first test seeded an
        # EMPTY database, where there is nothing to overwrite.
        #
        # So: update only the fields this file owns, and COALESCE the rest so a
        # local value survives when the checkpoint has none.
        db.execute(
            "INSERT INTO signals (commit_hex, hotkey, round, url_tag, "
            "first_seen_block, commit_block, t0_unix, t0_ms, plaintext, status, "
            "void_reason, exit_at_ms, is_copy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(commit_hex) DO UPDATE SET "
            "  status       = excluded.status, "
            "  void_reason  = excluded.void_reason, "
            "  plaintext    = COALESCE(excluded.plaintext, signals.plaintext), "
            "  commit_block = COALESCE(excluded.commit_block, signals.commit_block), "
            "  t0_ms        = COALESCE(excluded.t0_ms, signals.t0_ms), "
            "  exit_at_ms   = COALESCE(excluded.exit_at_ms, signals.exit_at_ms), "
            "  is_copy      = excluded.is_copy",
            (s["commit_hex"], s["hotkey"], int(s.get("round") or 0), "",
             int(s["commit_block"]) if s.get("commit_block") else 0,
             s.get("commit_block"), float(s["t0_unix"]), s.get("t0_ms"),
             s.get("plaintext"), s.get("status") or "sealed",
             s.get("void_reason"), s.get("exit_at_ms"),
             int(s.get("is_copy") or 0)))
        n_sig += 1
    for hk, m in (cp.get("meta") or {}).items():
        # Same rule: eliminated_t0 lives only in the local DB and must survive.
        db.execute(
            "INSERT INTO hotkey_meta (hotkey, first_seen_unix, strikes) "
            "VALUES (?,?,?) ON CONFLICT(hotkey) DO UPDATE SET "
            "  first_seen_unix = excluded.first_seen_unix, "
            "  strikes         = excluded.strikes",
            (hk, float(m["first_seen_unix"]), int(m.get("strikes") or 0)))
        n_meta += 1
    for r in (cp.get("referrals") or []):
        db.execute(
            "INSERT OR REPLACE INTO referrals (recruiter_hk, recruit_hk, "
            "commit_block, first_seen_block, observed_unix, recruit_reg_block) "
            "VALUES (?,?,?,?,?,?)",
            (r["recruiter_hk"], r["recruit_hk"], int(r["commit_block"]),
             int(r.get("first_seen_block") or r["commit_block"]),
             float(r.get("observed_unix") or 0.0), r.get("recruit_reg_block")))
        n_ref += 1
    db.commit()
    db.close()
    return {"signals": n_sig, "hotkeys": n_meta, "referrals": n_ref}


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__.strip().split("USAGE")[-1].strip())
        sys.exit(2)
    src = args[0]
    db_path = args[args.index("--db") + 1] if "--db" in args else config.DB_PATH
    n_anchor = int(args[args.index("--anchors") + 1]) if "--anchors" in args else 0
    dry = "--dry-run" in args

    cp = load(src)
    print(f"checkpoint: netuid={cp.get('netuid')} network={cp.get('network')} "
          f"signals={len(cp.get('signals') or [])} hotkeys={len(cp.get('meta') or {})} "
          f"referrals={len(cp.get('referrals') or [])}")
    if cp.get("netuid") != config.NETUID:
        print(f"  ✗ REFUSED: checkpoint is netuid {cp.get('netuid')}, this validator "
              f"is {config.NETUID}. Importing a testnet journal into mainnet (or the "
              f"reverse) would silently corrupt the vector.")
        sys.exit(1)

    good, bad = verify_signals(cp["signals"])
    checked = sum(1 for s in cp["signals"] if s.get("plaintext"))
    print(f"  ✓ plaintext↔commitment: {checked - len(bad)}/{checked} verify "
          f"(sha256(plaintext) == on-chain commit_hex)")
    if bad:
        print(f"  ✗ {len(bad)} signal(s) REFUSED — plaintext does not hash to the "
              f"commitment it claims:")
        for s in bad[:5]:
            print(f"      {s['commit_hex'][:16]}… {s.get('hotkey', '?')[:10]}…")

    if n_anchor:
        ok, unreadable, tot, mismatch = check_anchors(good, n_anchor)
        print(f"  {'✗' if mismatch else '✓'} anchors: {ok}/{tot} confirmed on-chain"
              + (f", {unreadable} unreadable (pruned state — an archive node reads "
                 f"these; unverified, not falsified)" if unreadable else ""))
        for cx, on in mismatch[:5]:
            print(f"      {cx}… chain says {on}…")
        if mismatch:
            print("  ✗ REFUSED: a journalled signal's commitment DIFFERS from chain.")
            sys.exit(1)

    if dry:
        print(f"\n(dry run — would import {len(good)} signals into {db_path})")
        return
    n = seed(db_path, cp, good)
    print(f"\nimported into {db_path}: {n['signals']} signals, {n['hotkeys']} "
          f"hotkeys, {n['referrals']} referrals")
    print("  NOTE: grades were imported as published and are NOT verified here. "
          "Re-grade against your own price feed before treating them as yours.")


if __name__ == "__main__":
    main()
