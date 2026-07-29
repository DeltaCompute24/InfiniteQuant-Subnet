#!/usr/bin/env python3
"""SN89 HF anchor cycle — seal → publish → (optionally) commit on chain.

Runs as a daemon. Each pass, for every window the ingest and tick recorder have
sealed but this service has not yet processed:

  1. compute both roots (receipts + ticks) and the window root
  2. publish the receipt log and tick log to the public bucket (if configured)
  3. submit the on-chain anchor commitment (ONLY if an anchor wallet is configured)

The on-chain step is gated on SN89_HF_ANCHOR_WALLET because it needs a hotkey
REGISTERED on the subnet — an unregistered key gets AccountNotAllowedCommit, and
CommitmentOf is one latest-wins slot per hotkey, so the anchor hotkey must anchor
and nothing else. Until that hotkey exists, this runs in PREVIEW posture: windows
are sealed, signed and retained locally, which is sufficient while emission is 0%
and the handful of preview traders trust the operator directly. The fraud-proof
guarantee turns on with the chain anchor, at the same time emissions do.

State: <state_dir>/anchored.txt — window ids already fully processed, so a restart
never re-anchors (which would burn the per-epoch commitment space budget).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import hf

LOG_DIR = Path(os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf"))
TICK_DIR = Path(os.getenv("SN89_HF_TICK_DIR", str(LOG_DIR / "ticks")))
STATE = Path(os.getenv("SN89_HF_ANCHOR_STATE", str(LOG_DIR / "anchored.txt")))
POLL_S = int(os.getenv("SN89_HF_ANCHOR_POLL_S", "20"))
NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
ANCHOR_WALLET = os.getenv("SN89_HF_ANCHOR_WALLET", "")     # name; empty = preview
ANCHOR_HOTKEY = os.getenv("SN89_HF_ANCHOR_HOTKEY", "default")
COMMIT_MAX_TRIES = int(os.getenv("SN89_HF_ANCHOR_MAX_TRIES", "5"))
PUBLIC_DIR = os.getenv("SN89_HF_PUBLIC_DIR", "")           # webhook-served; empty = no publish
# How far behind `now` ANY window must sit before we root/publish/anchor it. The
# tick recorder and the ingest seal the same window independently and within
# seconds of each other, so with no margin a sweep can land between them and
# retire the window half-written. This is SYMMETRIC and both directions have
# happened:
#   - tick-only sweep first  -> window published receipt-less, real receipts stranded
#   - receipt sweep first    -> window published tick-less AND anchored on-chain with
#                               tick_n=0, permanently mis-binding it (5 windows,
#                               2026-07-26..29; see the incident note in sweep()).
# The margin therefore gates the RECEIPT branch as well, not just the tick branch.
TICK_SETTLE_WINDOWS = int(os.getenv("SN89_HF_TICK_SETTLE_WINDOWS", "2"))
WINDOW_MS = hf.ANCHOR_WINDOW_S * 1000


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-anchor] {m}", flush=True)


def _done() -> set:
    try:
        return set(STATE.read_text().split())
    except FileNotFoundError:
        return set()


def _mark(w: int) -> None:
    with STATE.open("a") as f:
        f.write(f"{w}\n")


class Anchorer:
    def __init__(self):
        self.sub = None
        self.wallet = None
        self._fails: dict = {}
        if ANCHOR_WALLET:
            import bittensor as bt
            self.sub = bt.Subtensor(NETWORK)
            self.wallet = bt.Wallet(name=ANCHOR_WALLET, hotkey=ANCHOR_HOTKEY)
            self.wallet.unlock_hotkey()
            _log(f"anchor hotkey {self.wallet.hotkey.ss58_address} on {NETWORK}/{NETUID}")
        else:
            _log("PREVIEW posture — no anchor wallet, sealing + retaining only "
                 "(on-chain anchor turns on with emissions)")

    def _roots(self, w: int):
        rlog = LOG_DIR / f"{w}.jsonl"
        tlog = TICK_DIR / f"{w}.ticks.jsonl"
        if not rlog.exists() and not tlog.exists():
            return None
        # A window with no receipts is a QUIET window, not a missing one — its
        # receipt root is the all-zero root, exactly as seal_window() documents.
        # Returning None here (the old behaviour) made sweep() skip it, so it was
        # never published, and the tick log it carries is the LF grading substrate
        # once config.TOUCH_TICKS_FROM arms the touch_ticks rule.
        receipts = ([json.loads(l)["receipt"] for l in rlog.open() if l.strip()]
                    if rlog.exists() else [])
        ticks = [json.loads(l) for l in tlog.open()] if tlog.exists() else []
        root = hf.merkle_root([hf.leaf(hf.receipt_signing_bytes(
            r["hk"], r["seq"], r["ph"], r["t_recv_us"], r["grid_t0_ms"], r["ing"]))
            for r in sorted(receipts, key=hf.leaf_order_key)])
        troot = hf.tick_root(ticks)
        return {"n": len(receipts), "tick_n": len(ticks),
                "root": root, "tick_root": troot,
                "window_root": hf.window_root(root, troot)}

    def _publish(self, w: int) -> None:
        """Copy the sealed window into the webhook-served public dir so ANY
        validator can fetch the exact grading inputs and reproduce our weights.
        This is what makes mecid-1 replayable rather than trusted — the same role
        R2_PUBLIC_BASE plays for LF blobs."""
        if not PUBLIC_DIR:
            return
        import shutil
        dst = pathlib.Path(PUBLIC_DIR) / str(w)
        dst.mkdir(parents=True, exist_ok=True)
        pairs = ((LOG_DIR / f"{w}.jsonl", dst / "receipts.jsonl"),
                 (TICK_DIR / f"{w}.ticks.jsonl", dst / "ticks.jsonl"),
                 (TICK_DIR / f"{w}.ticks.json", dst / "ticks.json"),
                 (LOG_DIR / f"{w}.anchor.json", dst / "anchor.json"))
        for src, out in pairs:
            if src.exists():
                shutil.copyfile(src, out)
            elif out.name == "receipts.jsonl":
                # A quiet window has ZERO receipts, which is not the same thing as
                # an unfetchable one. hf_grade.lock_index fails CLOSED on a 404 —
                # correctly, since under-reading it would silently un-enforce the
                # cross-mechanism pair lock — so publishing a window without this
                # file stalls every grade cycle. Write it empty: it parses to zero
                # receipts and keeps "quiet" distinguishable from "missing".
                out.write_text("")
            else:
                continue
            try:
                out.chmod(0o644)
            except OSError:
                pass
        try:
            dst.chmod(0o755)
        except OSError:
            pass

    def _commit(self, w: int, r: dict) -> bool:
        if not self.wallet:
            return False
        data = hf.encode_anchor(w, r["n"], r["tick_n"], r["root"], r["tick_root"])
        ok = self.sub.set_commitment(wallet=self.wallet, netuid=NETUID,
                                     data=data, wait_for_finalization=False)
        _log(f"window {w} on-chain anchor {len(data.encode())}B -> "
             + ("OK" if ok else "FAILED"))
        return bool(ok)

    def sweep(self) -> int:
        done = _done()
        # Windows to process = receipt logs the ingest sealed UNION tick logs the
        # recorder sealed. Enumerating only receipt logs (the old behaviour) tied
        # the public tick feed to HF traffic: with no HF submissions no window was
        # published, and mechanism-0 grading — which reads its ticks from that same
        # feed once config.TOUCH_TICKS_FROM arms the touch_ticks substrate — simply
        # stopped, with every call stuck PENDING on a NULL entry_price and no error.
        # PUBLICATION is now decoupled from receipts. The ON-CHAIN ANCHOR is not:
        # committing every quiet window would spend ~480 commitments/day of the
        # per-epoch budget on all-zero receipt roots, and a window with no receipts
        # carries no HF weight for a missing anchor to invalidate.
        cutoff = int(time.time() * 1000) - TICK_SETTLE_WINDOWS * WINDOW_MS
        # ⚠ The cutoff gates BOTH branches. It used to gate only the tick branch,
        # so a window whose receipts existed but whose tick log had not yet been
        # written was swept immediately: _roots() computed tick_root over an EMPTY
        # tick list, _publish() skipped the absent tick file, _commit() anchored
        # tick_n=0 on chain, and _mark() retired it forever. Measured 2026-07-29:
        # 5 of 2812 published windows had anchor.json + receipts.jsonl and NO tick
        # file, all 5 anchored `ticks=0` while the recorder held 850-3201 real
        # ticks. Because LF grading reads its prices from this feed, ONE such
        # window stalled every LF call spanning it (13 calls) for the full 6h
        # GRADE_ABANDON_S and then force-washed them. Only receipt-bearing windows
        # were affected — quiet ones went through the tick branch, which was
        # already gated.
        cand = {int(p.stem) for p in LOG_DIR.glob("*.jsonl")
                if p.stem.isdigit() and int(p.stem) <= cutoff}
        for tp in TICK_DIR.glob("*.ticks.jsonl"):
            stem = tp.name.split(".", 1)[0]
            if stem.isdigit() and int(stem) <= cutoff:
                cand.add(int(stem))
        sealed = sorted(w for w in cand if str(w) not in done)
        n = 0
        for w in sealed:
            r = self._roots(w)
            if r is None:
                continue
            if r["n"] and not r["tick_n"]:
                # Past the settle margin this can no longer be a race — it means the
                # tick recorder is not sealing. Anchoring an empty tick_root binds
                # the window to prices that do not exist and cannot be undone, so
                # say so loudly rather than retiring it quietly.
                _log(f"!! window {w}: {r['n']} receipts but ZERO ticks past the "
                     f"settle margin — check iq-sn89-hf-ticks. Anchoring an empty "
                     f"tick_root permanently mis-binds this window.")
            self._publish(w)
            committed = self._commit(w, r) if r["n"] else False
            # Only retire the window once it is actually anchored. Marking on a
            # failed commit leaves a permanently unanchored window, which per
            # spec §6 invalidates that window's HF weights — and does it
            # silently. Bounded so a hard-failing window cannot spin forever
            # against the per-epoch commitment-space budget.
            if self.wallet and r["n"] and not committed:
                self._fails[w] = self._fails.get(w, 0) + 1
                if self._fails[w] < COMMIT_MAX_TRIES:
                    _log(f"window {w}: commit failed "
                         f"({self._fails[w]}/{COMMIT_MAX_TRIES}), will retry")
                    continue
                _log(f"!! window {w}: GIVING UP after {COMMIT_MAX_TRIES} "
                     f"commit attempts — window retired UNANCHORED")
            _mark(w)
            n += 1
            _log(f"window {w}: n={r['n']} ticks={r['tick_n']} "
                 f"wroot={r['window_root'][:16]}… "
                 f"{'ANCHORED' if (self.wallet and committed) else ('quiet(published)' if not r['n'] else ('UNANCHORED' if self.wallet else 'sealed(preview)'))}")
        return n

    def run(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _log(f"watching {LOG_DIR} every {POLL_S}s")
        while True:
            try:
                self.sweep()
            except Exception as e:      # noqa: BLE001
                _log(f"sweep error: {e}")
            time.sleep(POLL_S)


if __name__ == "__main__":
    Anchorer().run()
