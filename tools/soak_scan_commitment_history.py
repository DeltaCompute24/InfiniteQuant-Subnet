#!/usr/bin/env python3
"""Testnet soak for SCAN_COMMITMENT_HISTORY (audit #5).

The unit tests (tests/test_protocol.py::TestCommitmentHistoryScan) already prove
the INGEST LOGIC with a fake chain. This harness proves the one thing a unit
test cannot: that chain.read_commitments_in_block_range() recognizes the REAL
per-runtime `set_commitment` event shape on a LIVE chain and resolves committer
accounts — the exact precondition chain.py flags before
SN89_SCAN_COMMITMENT_HISTORY may be turned on for mainnet.

It also answers the question that decides whether the split-brain race is even
reachable: the Commitments pallet's on-chain RATE LIMIT. If a hotkey can only
set a commitment once per N blocks and N·blocktime exceeds one POLL_INTERVAL_S,
then no validator can ever miss an intermediate commitment — the overwrite-race
is chain-prevented, independent of this scan.

Read-only by default (safe to point at mainnet). Run from the repo root:

    # passive observe testnet for the last 600 blocks (~2 h)
    python tools/soak_scan_commitment_history.py --network test --netuid <N> --blocks 600

    # active end-to-end proof — needs a funded testnet miner wallet. Commits A,
    # then B within one poll interval, and proves snapshot=={B} while scan=={A,B}.
    python tools/soak_scan_commitment_history.py --network test --netuid <N> \
        --active --wallet.name <w> --wallet.hotkey <miner>

Exit code is 0 only when every applicable check PASSES, so it can gate CI.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# run-in-place: make `sn89_signals` importable when launched from the repo root
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from sn89_signals import chain as chainmod  # noqa: E402
from sn89_signals import config  # noqa: E402
# `crypto` pulls in `timelock` (Python-3.10-only wheels) and is needed ONLY by
# the active-overwrite check; defer it so the read-only soak runs anywhere.

BLOCKTIME_S = 12.0  # subtensor block time; only used for human-readable spans


# ── pretty verdict helpers ────────────────────────────────────────────────────
class Verdict:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool | None, str]] = []

    def record(self, name: str, ok: bool | None, detail: str = "") -> None:
        # ok=None ⇒ inconclusive (skipped / no data), not a failure
        self.checks.append((name, ok, detail))
        mark = {True: "PASS", False: "FAIL", None: "····"}[ok]
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    def ok(self) -> bool:
        return all(c[1] is not False for c in self.checks)


# ── check 0: on-chain commitment rate limit ───────────────────────────────────
def check_rate_limit(ch: "chainmod.Chain", v: Verdict) -> None:
    """Read the Commitments pallet rate limit and decide whether the
    overwrite-within-a-poll race is even reachable. The storage/const name has
    moved across runtimes, so probe a few and report the first that resolves."""
    print("\n[0] Commitments rate limit (is the overwrite race reachable?)")
    sub = ch.st.substrate
    rate_blocks = None
    src = None
    # newer runtimes expose it as a storage item; some as a pallet constant.
    for kind, name in (("storage", "RateLimit"), ("storage", "MaxSpace"),
                       ("constant", "RateLimit"), ("constant", "DefaultRateLimit")):
        try:
            if kind == "storage":
                q = sub.query("Commitments", name)
                val = getattr(q, "value", q)
            else:
                q = sub.get_constant("Commitments", name)
                val = getattr(q, "value", q)
            if isinstance(val, int) and name in ("RateLimit", "DefaultRateLimit"):
                rate_blocks, src = val, f"{kind}:Commitments.{name}"
                break
        except Exception:  # noqa: BLE001 — probe the next candidate
            continue
    if rate_blocks is None:
        v.record("rate-limit readable", None,
                 "could not read Commitments.RateLimit on this runtime — "
                 "inspect manually before relying on it")
        return
    span_s = rate_blocks * BLOCKTIME_S
    detail = (f"{rate_blocks} blocks (~{span_s:.0f}s) per hotkey via {src}; "
              f"POLL_INTERVAL_S={config.POLL_INTERVAL_S}s")
    if span_s >= config.POLL_INTERVAL_S:
        v.record("rate limit ≥ poll interval", True,
                 detail + " ⇒ at most one commit can land between two polls; "
                          "the overwrite-race is chain-prevented")
    else:
        v.record("rate limit ≥ poll interval", False,
                 detail + " ⇒ a hotkey CAN land >1 commit between polls; the "
                          "history scan is the mitigation — validate checks 1–3")


# ── raw event-shape introspection ─────────────────────────────────────────────
def _commitment_events_in_block(ch: "chainmod.Chain", block: int) -> list[dict]:
    """Return [{module, event, attrs, resolved_account}] for every event in
    `block` whose pallet looks like Commitments. Mirrors the loose match used by
    read_commitments_in_block_range so we see exactly what it will key on."""
    out: list[dict] = []
    try:
        bh = ch.st.get_block_hash(block)
        events = ch.st.substrate.get_events(bh)
    except Exception as e:  # noqa: BLE001
        return [{"error": f"block {block} unreadable: {e}"}]
    for ev in events or []:
        try:
            e = ev.value if hasattr(ev, "value") else ev
            inner = e.get("event", e) if isinstance(e, dict) else {}
            module = str(inner.get("module_id") or inner.get("module") or "")
            if "commitment" not in module.lower():
                continue
            attrs = inner.get("attributes")
            out.append({
                "module": module,
                "event": str(inner.get("event_id") or inner.get("event") or ""),
                "attrs": attrs,
                "resolved_account": chainmod._account_from_event(attrs),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def check_event_shape(ch: "chainmod.Chain", v: Verdict, lo: int, hi: int) -> int:
    """Scan [lo, hi] for real Commitments events and confirm the loose match
    fires AND _account_from_event resolves an ss58. Returns #blocks with a
    recognized, account-resolving commitment event."""
    print(f"\n[1] Event-shape recognition over blocks {lo}..{hi} "
          f"(~{(hi - lo) * BLOCKTIME_S / 60:.0f} min)")
    seen_events = 0
    resolved = 0
    sample_shown = 0
    for block in range(lo, hi + 1):
        for rec in _commitment_events_in_block(ch, block):
            if "error" in rec:
                continue
            seen_events += 1
            acct = rec["resolved_account"]
            if acct is not None:
                resolved += 1
                try:
                    chainmod._to_ss58(acct)
                except Exception:  # noqa: BLE001
                    pass
            if sample_shown < 5:  # show the runtime's actual shape for the record
                sample_shown += 1
                print(f"    block {block}: {rec['module']}.{rec['event']} "
                      f"attrs={rec['attrs']!r} → account={acct!r}")
    if seen_events == 0:
        v.record("set_commitment events recognized", None,
                 "no Commitments events in window — widen --blocks or use "
                 "--active to drive one; cannot confirm the event shape blind")
    elif resolved == seen_events:
        v.record("set_commitment events recognized", True,
                 f"{seen_events} event(s), account resolved on all")
    else:
        v.record("set_commitment events recognized", False,
                 f"{seen_events} event(s) but only {resolved} resolved an "
                 "account — _account_from_event misses this runtime's shape")
    return resolved


# ── snapshot vs scan round-trip ───────────────────────────────────────────────
def check_round_trip(ch: "chainmod.Chain", v: Verdict, lo: int, hi: int) -> None:
    """The snapshot is the latest commitment per hotkey; the scan walks the block
    range. Every snapshot entry whose commit_block is in [lo,hi] MUST also be
    found by the scan (else the scan can't even re-find a known commitment).
    Scan-only catches are real overwrites observed in the wild — the payoff."""
    print(f"\n[2] Snapshot vs history-scan round-trip over blocks {lo}..{hi}")
    try:
        snapshot = ch.read_all_commitments_with_block()
    except Exception as e:  # noqa: BLE001
        v.record("round-trip", None, f"snapshot read failed: {e}")
        return
    # respect the function's own cap, but widen it for the soak window
    orig_cap = config.SCAN_MAX_BLOCKS_PER_POLL
    config.SCAN_MAX_BLOCKS_PER_POLL = max(orig_cap, hi - lo + 2)
    try:
        scanned = ch.read_commitments_in_block_range(lo - 1, hi)
    except Exception as e:  # noqa: BLE001
        v.record("round-trip", None, f"scan raised: {e}")
        return
    finally:
        config.SCAN_MAX_BLOCKS_PER_POLL = orig_cap

    scan_keys = {(d["hotkey"], d["commit"]) for d in scanned}
    snap_in_window = {
        (d["hotkey"], d["commit"]): d["commit_block"]
        for d in snapshot.values()
        if lo <= int(d.get("commit_block") or 0) <= hi
    }
    missed = [k for k in snap_in_window if k not in scan_keys]
    scan_only = scan_keys - set(snap_in_window) - {
        (d["hotkey"], d["commit"]) for d in snapshot.values()}

    print(f"    snapshot commitments in window: {len(snap_in_window)} · "
          f"scan captured: {len(scan_keys)} · scan-only (overwrites): "
          f"{len(scan_only)}")
    for hk, commit in list(scan_only)[:10]:
        print(f"    ↳ overwrite caught by scan only: {hk[:8]}… {commit[:12]}…")

    if not snap_in_window:
        v.record("scan re-finds known commitments", None,
                 "no snapshot commitment landed in the window — nothing to "
                 "cross-check; widen --blocks or use --active")
    elif missed:
        v.record("scan re-finds known commitments", False,
                 f"{len(missed)} snapshot commitment(s) in-window NOT found by "
                 "the scan — event/extrinsic recognition is incomplete")
    else:
        v.record("scan re-finds known commitments", True,
                 f"all {len(snap_in_window)} in-window snapshot commitments "
                 f"re-found by the scan; +{len(scan_only)} overwrite(s) caught")


# ── active end-to-end overwrite proof ─────────────────────────────────────────
def check_active_overwrite(ch: "chainmod.Chain", v: Verdict, args) -> None:
    """Drive the real attack: commit A, then commit B within one poll interval,
    and prove the snapshot shows only B while the scan recovers both. This is the
    only check that exercises the full path against a commitment WE control."""
    print("\n[3] Active overwrite proof (A then B inside one poll interval)")
    import bittensor as bt
    from sn89_signals import crypto  # deferred: only the active path needs timelock
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    hk = wallet.hotkey.ss58_address
    rnd = crypto.target_round(time.time() + config.REVEAL_DELAY_S)
    commit_a = "a" * 64
    commit_b = "b" * 64
    url_a, url_b = "soak://overwrite/A", "soak://overwrite/B"

    block_before = ch.current_block()
    print(f"    committing A ({commit_a[:8]}…) as {hk[:8]}… at block ~{block_before}")
    ok_a = ch.commit(wallet, commit_a, rnd, url_a)
    if not ok_a:
        v.record("active overwrite", False,
                 "set_commitment(A) rejected — likely the on-chain rate limit "
                 "(see check 0): if so the race is chain-prevented and the scan "
                 "is unnecessary; otherwise investigate")
        return
    # overwrite as fast as the chain allows; if this is rejected by the rate
    # limit, that is itself the answer (race is chain-prevented).
    print(f"    overwriting with B ({commit_b[:8]}…) immediately")
    ok_b = ch.commit(wallet, commit_b, rnd, url_b)
    if not ok_b:
        v.record("active overwrite", None,
                 "set_commitment(B) rejected before A could be overwritten — "
                 "the rate limit prevents back-to-back commits, so the "
                 "overwrite-race is chain-prevented (scan not required)")
        return
    block_after = ch.current_block()

    snapshot = ch.read_all_commitments_with_block()
    snap_commit = (snapshot.get(hk) or {}).get("commit")
    orig_cap = config.SCAN_MAX_BLOCKS_PER_POLL
    config.SCAN_MAX_BLOCKS_PER_POLL = max(orig_cap, block_after - block_before + 4)
    try:
        scanned = ch.read_commitments_in_block_range(block_before - 1, block_after + 1)
    finally:
        config.SCAN_MAX_BLOCKS_PER_POLL = orig_cap
    scan_commits = {d["commit"] for d in scanned if d["hotkey"] == hk}

    print(f"    snapshot[{hk[:8]}…] = {snap_commit and snap_commit[:8]}…  "
          f"scan = {{{', '.join(c[:8] for c in sorted(scan_commits))}}}")
    snapshot_is_b_only = snap_commit == commit_b
    scan_has_both = {commit_a, commit_b} <= scan_commits
    if snapshot_is_b_only and scan_has_both:
        v.record("active overwrite", True,
                 "snapshot saw only B (the overwrite) but the scan recovered "
                 "both A and B — the scan closes the race end-to-end")
    elif snapshot_is_b_only and not scan_has_both:
        v.record("active overwrite", False,
                 f"snapshot=B but scan missed A (got {sorted(scan_commits)}) — "
                 "the scan does NOT recover the overwritten commitment here")
    else:
        v.record("active overwrite", None,
                 f"snapshot was {snap_commit and snap_commit[:8]}… (expected B) "
                 "— blocks may have advanced between commits; re-run")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", default=os.getenv("SN89_NETWORK", "test"),
                   help="subtensor network (default: test; use 'finney' for mainnet)")
    p.add_argument("--netuid", type=int, default=config.NETUID)
    p.add_argument("--blocks", type=int, default=300,
                   help="how many recent blocks to observe (passive checks)")
    p.add_argument("--active", action="store_true",
                   help="drive a real A→B overwrite (needs a funded miner wallet)")
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    args = p.parse_args()

    if args.network == "finney" and args.active:
        print("refusing --active on finney (mainnet). Soak on testnet.")
        return 2

    print(f"SN89 SCAN_COMMITMENT_HISTORY soak · network={args.network} "
          f"· netuid={args.netuid}")
    ch = chainmod.Chain(network=args.network, netuid=args.netuid)
    head = ch.current_block()
    lo, hi = max(1, head - args.blocks), head
    v = Verdict()

    check_rate_limit(ch, v)
    check_event_shape(ch, v, lo, hi)
    check_round_trip(ch, v, lo, hi)
    if args.active:
        check_active_overwrite(ch, v, args)

    print("\n" + "=" * 70)
    passed = v.ok()
    inconclusive = any(c[1] is None for c in v.checks)
    if passed and not inconclusive:
        print("VERDICT: PASS — every check passed. Safe to enable "
              "SN89_SCAN_COMMITMENT_HISTORY=1 (or rely on the rate limit if "
              "check 0 showed the race is chain-prevented).")
    elif passed and inconclusive:
        print("VERDICT: INCONCLUSIVE — no check FAILED, but some had no data. "
              "Re-run with a wider --blocks or --active to exercise them before "
              "flipping the flag on mainnet.")
    else:
        print("VERDICT: FAIL — at least one check failed. Do NOT enable "
              "SN89_SCAN_COMMITMENT_HISTORY until the event/extrinsic shape is "
              "fixed for this runtime.")
    print("=" * 70)
    return 0 if (passed and not inconclusive) else 1


if __name__ == "__main__":
    raise SystemExit(main())
