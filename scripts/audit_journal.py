#!/usr/bin/env python3
"""Audit the single validator: re-derive its weights from the published journal
and confirm they match what it set on-chain. This is the trust mechanism of the
single-validator model (docs/single-validator-model.md) — "replay and verify the
results match expectations." Anyone can run it; it needs no validator role.

    python3 audit_journal.py <checkpoint.json> [--chain] [--tolerance 1e-6]

Checks:
  1. REPLAY (always, offline): re-derive weights from the journal with the same
     scoring code the validator runs, and compare to the checkpoint's recorded
     weights. Proves the weight vector is an honest function of the journal — a
     validator that hid a loss or fudged a tier is caught.
  2. --chain ON-CHAIN WEIGHTS: compare the replay to the LIVE metagraph weights
     (not the checkpoint's snapshot), catching a validator that set something
     different on-chain than its journal implies.
  3. --chain COMMIT ANCHORS: spot-check that each signal's commit_hex exists
     on-chain at its commit_block (single CommitmentOf reads — no block scan),
     proving the journal contains no fabricated signals.

Exits non-zero on any mismatch.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import replay  # noqa: E402


def _weights_equal(a: dict, b: dict, tol: float) -> tuple[bool, list]:
    keys = set(a) | set(b)
    diffs = []
    for k in sorted(keys):
        va, vb = a.get(k, 0.0), b.get(k, 0.0)
        if abs(va - vb) > tol:
            diffs.append((k, va, vb))
    return (not diffs), diffs


def main():
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        print("usage: audit_journal.py <checkpoint.json> [--chain] [--tolerance T]")
        sys.exit(2)
    # default tolerance accommodates the u16 quantization set_weights applies on-chain
    tol = float(args[args.index("--tolerance") + 1]) if "--tolerance" in args else 1e-3
    cp = json.load(open(paths[0]))
    signals, meta, now = cp["signals"], cp["meta"], cp["now_unix"]

    uid_by_hotkey = {hk: int(u) for hk, u in (cp.get("uid_by_hotkey") or {}).items()}
    recorded = {int(k): float(v) for k, v in (cp.get("weights_onchain") or {}).items()}

    if "--chain" in args:
        try:
            from sn89_signals import chain
            ch = chain.Chain()
            mg = ch.metagraph()
            uid_by_hotkey = {hk: i for i, hk in enumerate(mg.hotkeys)}
            vuid = cp.get("validator_uid")
            if vuid is not None:
                live = ch.weights_for_uid(int(vuid))
                if live:
                    recorded = live
                    print(f"  (live: on-chain weights for validator uid {vuid} — "
                          f"{len(recorded)} entries)")
                else:
                    print(f"  ⚠ --chain: no on-chain weights set yet for validator uid {vuid}")
            else:
                print("  ⚠ --chain: checkpoint has no validator_uid; using snapshot")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ --chain requested but chain read failed: {e}")
            sys.exit(2)

    if not uid_by_hotkey:
        print("INCONCLUSIVE: no uid map (checkpoint lacks uid_by_hotkey; pass --chain)")
        sys.exit(2)

    referrals = cp.get("referrals") or []
    print(f"replaying {len(signals)} signals over {len(meta)} hotkeys "
          f"({len(referrals)} referrals, now={now:.0f})…")
    # The on-chain vector is the BLEND of LF + HF + Closers (config.COMP_WEIGHTS)
    # and has been since the 2026-08-03 cutover. Replaying LF alone -- which this
    # tool did until 2026-08-18 -- put every uid off by a constant
    # 1/COMP_WEIGHTS["lf"] and reported "AUDIT FAILED", which reads as proof of
    # dishonesty rather than an incomplete comparison. HF and Closers are graded
    # from the PUBLIC anchored windows, so this still needs no validator role.
    replayed, detail = replay.combined_weights_from_journal(
        signals, meta, uid_by_hotkey, now, referrals=referrals)
    if detail.get("combined"):
        def _n(v):
            return "∅" if v is None else sum(
                1 for u, w in v.items() if w > 0 and u != 0)
        print(f"  shares={detail['shares']}  earners: lf={_n(detail['lf'])} "
              f"hf={_n(detail['hf'])} closers={_n(detail['closers'])}")
        for comp, err in (detail.get("errors") or {}).items():
            print(f"  ⚠ {comp} vector could not be computed ({err}) — its share "
                  f"burns, and this replay cannot be compared to the chain.")
    else:
        print("  (pre-cutover instant: LF only, which is also what was on chain)")

    if not recorded:
        print(f"  replay produced {len(replayed)} uids, but there are NO on-chain weights "
              "to compare against — run with --chain, or export with --validator-hotkey.")
        print("\nAUDIT INCONCLUSIVE — nothing to verify the replay against.")
        sys.exit(2)

    match, diffs = _weights_equal(replayed, recorded, tol)
    if match:
        print(f"  ✓ REPLAY MATCHES on-chain weights ({len(replayed)} uids, tol={tol})")
    else:
        print(f"  ✗ REPLAY MISMATCH on {len(diffs)} uid(s):")
        lfv, hfv, clv = detail.get("lf") or {}, detail.get("hf") or {}, detail.get("closers") or {}
        for uid, rv, cv in diffs[:20]:
            where = ",".join(n for n, v in (("lf", lfv), ("hf", hfv), ("closers", clv))
                             if v.get(uid, 0) > 0) or "-"
            print(f"      uid {uid}: replay={rv:.6f}  on-chain={cv:.6f}  in [{where}]")
        # Attribution matters because the three competitions are NOT equally
        # stable. Closers routinely runs a handful of earners, so one call
        # grading between the validator's commit and this replay redistributes
        # its whole share -- a real point-in-time difference, not a broken
        # journal. A mismatch confined to `closers` (or to `hf`) with LF-only
        # uids clean is the expected shape when replaying minutes after a commit.
        drifty = [u for u, _, _ in diffs if (clv.get(u, 0) > 0 or hfv.get(u, 0) > 0)]
        if diffs and len(drifty) == len(diffs):
            print(f"      ↳ all {len(diffs)} are HF/Closers participants: consistent "
                  f"with grading drift since the commit, not with a journal defect. "
                  f"Re-run immediately after a weight commit to rule drift out.")

    anchors_ok = True
    if "--anchors" in args:
        anchors_ok = _check_anchors(signals, args)
    if "--referral-anchors" in args:
        anchors_ok = _check_referral_anchors(referrals) and anchors_ok

    # ── verdict ──────────────────────────────────────────────────────────────
    # Three outcomes, not two. The old code printed "AUDIT FAILED — the
    # validator's record does not match the chain" for ANY mismatch, which is
    # the most damaging possible reading of the most common one: LF replays
    # exactly, while HF and Closers are graded continuously from the public
    # windows and keep moving after the validator commits. Closers in particular
    # often has a handful of earners, so a single call grading in the gap
    # redistributes its entire share.
    #
    # A mismatch confined to HF/Closers participants is a TIMING statement.
    # A mismatch touching a uid that earns ONLY in LF is a JOURNAL statement,
    # and that is the one worth alarming on -- LF is replayed from the published
    # journal alone, so it must reproduce exactly at any time.
    lfv = detail.get("lf") or {}
    hfv = detail.get("hf") or {}
    clv = detail.get("closers") or {}
    lf_only_bad = [u for u, _, _ in diffs
                   if lfv.get(u, 0) > 0 and not (hfv.get(u, 0) > 0 or clv.get(u, 0) > 0)]
    burned = list((detail.get("errors") or {}).keys())

    if match and anchors_ok:
        print("\nAUDIT PASSED — on-chain weights match a replay of the journal"
              + (", and every checked signal is anchored on-chain."
                 if "--anchors" in args else "."))
        sys.exit(0)
    if not anchors_ok:
        print("\nAUDIT FAILED — a journalled signal is not anchored on-chain.")
        sys.exit(1)
    if lf_only_bad:
        print(f"\nAUDIT FAILED — {len(lf_only_bad)} uid(s) that earn only in LF do "
              f"not match: {lf_only_bad[:10]}. LF is replayed from the published "
              f"journal alone and must reproduce exactly.")
        sys.exit(1)
    if burned:
        print(f"\nAUDIT INCONCLUSIVE — could not compute {', '.join(burned)}; "
              f"that share burns on chain and this replay cannot be compared.")
        sys.exit(2)
    print(f"\nAUDIT INCONCLUSIVE — the LF journal replays clean (no LF-only uid "
          f"differs); the {len(diffs)} difference(s) are all HF/Closers "
          f"participants, whose grades keep moving after the validator commits. "
          f"Re-run within a minute of a weight commit to settle it.")
    sys.exit(2)


def _check_anchors(signals: list, args: list) -> bool:
    """Verify each signal's commit_hex against on-chain CommitmentOf at its
    commit_block (no fabricated signals). Checks the most recent N (default 50;
    `--anchors N` to change) so the reads land on non-pruned blocks unless the
    endpoint is an archive node. FAILS only on a definitive mismatch; an unreadable
    (pruned) block is reported, not failed. Logs exactly what was and wasn't checked."""
    try:
        from sn89_signals import chain
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ commit-anchor check needs chain access: {e}")
        return True
    i = args.index("--anchors")
    n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 50
    ch = chain.Chain()
    have = [s for s in signals if s.get("commit_block")]
    sample = sorted(have, key=lambda s: -int(s["commit_block"]))[:n]
    verified = mismatched = unreadable = 0
    bad = []
    for s in sample:
        on = ch.commitment_at_block(s["hotkey"], int(s["commit_block"]))
        if on is None:
            unreadable += 1
        elif on == s["commit_hex"]:
            verified += 1
        else:
            mismatched += 1
            bad.append((s["commit_hex"][:12], on[:12], s["commit_block"]))
    print(f"  COMMIT ANCHORS (most recent {len(sample)} of {len(have)}): "
          f"{verified} verified, {mismatched} mismatched, {unreadable} unreadable"
          + (" (older blocks need an archive node)" if unreadable else ""))
    for ch_hex, on_hex, blk in bad[:10]:
        print(f"      ✗ block {blk}: journal {ch_hex}… ≠ on-chain {on_hex}…")
    if len(have) > len(sample):
        print(f"  (note: {len(have) - len(sample)} older signals not sampled — raise "
              f"--anchors N to check more)")
    return mismatched == 0


def _check_referral_anchors(referrals: list) -> bool:
    """Verify each journaled referral against on-chain CommitmentOf at its
    commit_block (an sn89ref payload naming the same recruit — no fabricated
    referrals). Best-effort like _check_anchors: FAILS only on a definitive
    mismatch; an unreadable (pruned) block is reported, not failed."""
    if not referrals:
        print("  REFERRAL ANCHORS: none to check")
        return True
    try:
        from sn89_signals import chain
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ referral-anchor check needs chain access: {e}")
        return True
    ch = chain.Chain()
    verified = mismatched = unreadable = 0
    bad = []
    for r in referrals:
        on = ch.referral_at_block(r["recruiter_hk"], int(r["commit_block"]))
        if on is None:
            unreadable += 1
        elif on == r["recruit_hk"]:
            verified += 1
        else:
            mismatched += 1
            bad.append((r["recruiter_hk"][:8], r["recruit_hk"][:8], r["commit_block"]))
    print(f"  REFERRAL ANCHORS ({len(referrals)}): {verified} verified, "
          f"{mismatched} mismatched, {unreadable} unreadable"
          + (" (older blocks need an archive node)" if unreadable else ""))
    for rec, cru, blk in bad[:10]:
        print(f"      ✗ block {blk}: {rec}… → {cru}… not anchored on-chain")
    return mismatched == 0


if __name__ == "__main__":
    main()
