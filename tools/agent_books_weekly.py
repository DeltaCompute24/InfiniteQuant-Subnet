#!/usr/bin/env python3
"""Agent competition v2: freeze a week of agent books, refresh the live book state, or
verify a published weekly file by recomputing it.

  freeze  (default)  the latest week whose ticks are final (AGENT_BOOKS_FREEZE_DELAY_S after
                     its close), or --week YYYY-MM-DD (a Monday). Writes
                     <out>/agent-books-week-<monday>.json (+ .sha256). Refuses to
                     overwrite a frozen week whose bytes would change (exit 3).
  --live             --live-out (config.AGENT_BOOKS_LIVE_PATH): every book replayed
                     through now, in the shape the intake and droplet read.
  --verify PATH      recompute the week PATH claims and compare bytes (exit 1 on mismatch).

Every book replays from the EPOCH (the first scored week, agent_books_io.epoch_ms(), or
--epoch) so HWM, the drawdown high and elimination carry across weeks. Ticks come from the
sealed HF tick shards (hf_grade.LOCAL_TICK_SRC) or, off-host, the published windows
(hf.HF_PUBLIC_BASE); each UTC day is reduced once to the ticks a book can read and cached
under <out>/thin/. Bond and registration are read from chain at the block of the week's
closing Monday 00:00 UTC (archive-backed), unless --bonds-json / --registry-json are given.

    python tools/agent_books_weekly.py [--week 2026-11-09] [--out DIR] [--live]
        [--receipts-dir DIR] [--receipts-url URL ...] [--tick-src DIR] [--tick-url BASE]
        [--bonds-json F] [--registry-json F] [--epoch 2026-11-09] [--require-anchor]
        [--verify PATH] [--force] [--no-verify-sig]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sn89_signals import agent_books as ab  # noqa: E402
from sn89_signals import agent_books_io as abio  # noqa: E402
from sn89_signals import config, hf  # noqa: E402


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [agent-books] {m}", flush=True)


def fetch_text(url: str, timeout: float = 20.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "sn89-agent-books/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError):
        return None


def monday_ms(s: str) -> int:
    d = dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    ms = int(d.timestamp() * 1000)
    if ab.week_start_ms(ms) != ms:
        raise SystemExit(f"{s} is not a Monday")
    return ms


def make_reader(tick_src: str, tick_url: str):
    def read(w: int):
        if tick_src:
            lines = abio.window_lines_local(tick_src, w)
            if lines is not None:
                return lines
        if tick_url:
            txt = fetch_text(f"{tick_url.rstrip('/')}/{w}/ticks.jsonl")
            if txt is not None:
                return txt.splitlines()
        return None
    return read


def load_receipts(args) -> tuple[list, dict]:
    def lines():
        if args.receipts_dir:
            yield from abio.iter_dir_lines(args.receipts_dir)
        urls = list(args.receipts_url or [])
        if not urls and config.AGENT_BOOKS_RECEIPTS_URLS:
            urls = [u.strip() for u in config.AGENT_BOOKS_RECEIPTS_URLS.split(",") if u.strip()]
        if urls:
            yield from abio.iter_url_lines(urls, fetch_text)
    verify = (lambda hk, m, s: True) if args.no_verify_sig else abio.sr25519_verify
    rs, d1 = abio.verified_receipts(lines(), verify=verify)
    # phase 3: from config.AGENT_RUNNER_FROM a receipt needs the book runner's countersignature
    def runner_lines():
        if args.receipts_dir:
            yield from abio.iter_dir_runner(args.receipts_dir)
        urls = list(args.receipts_url or [])
        if not urls and config.AGENT_BOOKS_RECEIPTS_URLS:
            urls = [u.strip() for u in config.AGENT_BOOKS_RECEIPTS_URLS.split(",") if u.strip()]
        if urls:
            yield from abio.iter_url_runner(urls, fetch_text)
    if config.AGENT_RUNNER_FROM_UNIX:
        rs, d_run = abio.filter_runner(rs, abio.runner_commitments(runner_lines()), verify=verify)
        d1 = abio.merge_drops(d1, d_run)
    d2 = {}
    if args.require_anchor:
        rs, d2 = abio.filter_anchored(rs, load_anchor_windows(args.anchor_dir))
    rs, d3 = abio.dedupe_receipts(rs)
    return rs, abio.merge_drops(d1, d2, d3)


def load_anchor_windows(log_dir: str) -> dict:
    """{w: {"agent_root", "agent_n", "anchor_ts_ms"}} from the v2 anchor records the HF anchor
    publisher writes (<log_dir>/<w>.anchor-v2.json locally, <w>/anchor-v2.json published).

    This reader trusts the local files (our own publisher wrote them). A third-party replayer
    must additionally check each one with hf.verify_anchor (v2 form) against the anchor
    hotkey's on-chain commitment history and take anchor_ts_ms from that inclusion block."""
    out = {}
    try:
        names = os.listdir(log_dir)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".anchor-v2.json"):
            continue
        try:
            with open(os.path.join(log_dir, n), encoding="utf-8") as fh:
                a = json.load(fh)
            if a.get("anchor_ts_ms") is None:
                continue
            out[int(a["w"])] = {"agent_root": a["agent_root"], "agent_n": int(a["agent_n"]),
                                "anchor_ts_ms": int(a["anchor_ts_ms"])}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def registry_and_bonds(args, week_end_ms: int) -> tuple[dict, dict, int | None]:
    if args.bonds_json:
        with open(args.bonds_json, encoding="utf-8") as fh:
            bonds = {k: float(v) for k, v in json.load(fh).items()}
        reg = dict(config.AGENT_BOOKS_GRANDFATHERED)
        if args.registry_json:
            with open(args.registry_json, encoding="utf-8") as fh:
                reg.update(json.load(fh))
        return dict(sorted(reg.items())), bonds, None
    from sn89_signals import chain
    ch = chain.Chain()
    block = ch.block_at_or_after_ms(week_end_ms)
    jpath = os.path.join(args.out, "agent-books-entities.json")
    try:
        with open(jpath, encoding="utf-8") as fh:
            journal = json.load(fh)
    except (OSError, ValueError):
        journal = {}
    journal = abio.merge_registry(journal, ch.read_entity_registrations())
    _atomic_write(jpath, (json.dumps(journal, sort_keys=True, indent=1) + "\n").encode())
    reg = abio.registry_as_of(journal, block)
    bonds = {e: ch.alpha_for_coldkey(ck, block) for e, ck in reg.items()}
    return reg, bonds, block


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def replay(args, receipts: list, end_ms: int, now_ms: int):
    epoch = monday_ms(args.epoch) if args.epoch else abio.epoch_ms()
    if not config.AGENT_BOOKS_FROM_UNIX and not args.epoch:
        raise SystemExit("SN89_AGENT_BOOKS_FROM is 0: pass --epoch YYYY-MM-DD")
    universe = abio.book_universe_through(end_ms / 1000)
    rs = [r for r in receipts if int(r["t_recv_ms"]) < end_ms]
    t0 = time.time()
    # --live also keeps per-minute high/low ticks for the display-only lifetime max drawdown;
    # separate cache, so the weekly freeze's thin set is exactly what it was.
    live = bool(getattr(args, "live", False))
    ticks, missing = abio.load_thin_range(
        epoch, end_ms, rs, universe, make_reader(args.tick_src, args.tick_url),
        cache_dir=os.path.join(args.out, "thin-x" if live else "thin"),
        final_before_ms=now_ms - config.AGENT_BOOKS_FREEZE_DELAY_S * 1000,
        extremes_ms=60_000 if live else 0)
    t1 = time.time()
    results = abio.replay_all(rs, abio.PairIndex(ticks), universe, start_ms=epoch,
                              end_ms=end_ms, keep_all=live)
    _log(f"epoch {abio.week_label(epoch)} → {end_ms}: {len(rs)} receipts, "
         f"{len(results)} books, {len(ticks)} thin ticks, {len(missing)} missing windows; "
         f"ticks {t1 - t0:.1f}s, replay {time.time() - t1:.1f}s")
    return epoch, results, missing


def freeze_bytes(args, receipts: list, drops: dict, ws: int, now_ms: int) -> bytes:
    we = ws + ab.WEEK_MS
    epoch, results, missing = replay(args, receipts, we, now_ms)
    if ws < epoch:
        raise SystemExit("week is before the epoch")
    reg, bonds, block = registry_and_bonds(args, we)
    week_rs = [r for r in receipts if ws <= int(r["t_recv_ms"]) < we]
    miss_week = sorted(w for w in missing if ws <= w < we)
    doc = abio.build_week(results, ws, reg, bonds, week_rs, extra={
        "epoch": epoch, "snapshot_block": block, "missing_windows": len(miss_week)})
    return abio.dumps_week(doc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=config.AGENT_BOOKS_DIR)
    ap.add_argument("--week")
    ap.add_argument("--epoch")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--live-out", default=config.AGENT_BOOKS_LIVE_PATH)
    ap.add_argument("--trades-dir", default=config.AGENT_BOOKS_TRADES_DIR,
                    help="with --live: per-book trades for the IQ Terminal ('' = off)")
    ap.add_argument("--verify")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--receipts-dir", default=config.AGENT_BOOKS_RECEIPTS_DIR)
    ap.add_argument("--receipts-url", action="append")
    ap.add_argument("--tick-src", default=os.getenv("SN89_HF_LOCAL_TICK_SRC",
                                                    "/var/lib/sn89-hf/ticks"))
    ap.add_argument("--tick-url", default=hf.HF_PUBLIC_BASE)
    ap.add_argument("--bonds-json")
    ap.add_argument("--registry-json")
    ap.add_argument("--require-anchor", action="store_true")
    ap.add_argument("--anchor-dir", default=os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf"))
    ap.add_argument("--no-verify-sig", action="store_true",
                    help="skip sr25519 checks (local simulation only)")
    args = ap.parse_args()
    now_ms = int(time.time() * 1000)
    receipts, drops = load_receipts(args)
    if drops:
        _log(f"receipt drops: {drops}")

    if args.live:
        _, results, _ = replay(args, receipts, now_ms, now_ms)
        doc = abio.build_live(results, now_ms)
        _atomic_write(args.live_out,
                      (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode())
        _log(f"live: {len(doc['books'])} books -> {args.live_out}")
        if args.trades_dir:
            os.makedirs(args.trades_dir, exist_ok=True)
            for acct, tdoc in abio.build_trades(results, now_ms).items():
                n = acct.rsplit("_", 1)[-1]
                if n.isdigit():
                    _atomic_write(os.path.join(args.trades_dir, f"{n}.json"),
                                  (json.dumps(tdoc, sort_keys=True, separators=(",", ":"),
                                              allow_nan=False) + "\n").encode())
        return 0

    if args.verify:
        with open(args.verify, "rb") as fh:
            claimed = fh.read()
        ws = int(json.loads(claimed)["week_start"])
        mine = freeze_bytes(args, receipts, drops, ws, now_ms)
        ok = mine == claimed
        _log(f"verify {abio.week_label(ws)}: claimed {hashlib.sha256(claimed).hexdigest()[:16]} "
             f"recomputed {hashlib.sha256(mine).hexdigest()[:16]} -> "
             + ("MATCH" if ok else "MISMATCH"))
        return 0 if ok else 1

    ws = (monday_ms(args.week) if args.week
          else abio.freezable_week_start_ms(now_ms))
    if not args.force and now_ms < ws + ab.WEEK_MS + config.AGENT_BOOKS_FREEZE_DELAY_S * 1000:
        _log(f"week {abio.week_label(ws)} is not final until "
             f"{config.AGENT_BOOKS_FREEZE_DELAY_S // 3600}h after it closes (use --force)")
        return 2
    data = freeze_bytes(args, receipts, drops, ws, now_ms)
    path = abio.week_file_path(args.out, ws)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            old = fh.read()
        if old == data:
            _log(f"{path} unchanged")
            return 0
        if not args.force:
            _log(f"!! {path} exists and recomputation DIFFERS — not overwriting a frozen week")
            return 3
    _atomic_write(path, data)
    _atomic_write(path + ".sha256", (hashlib.sha256(data).hexdigest() + "\n").encode())
    _log(f"froze {path} sha256={hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
