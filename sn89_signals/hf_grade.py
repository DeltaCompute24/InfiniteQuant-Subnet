"""SN89 HF grading from the PUBLIC window logs (CONSENSUS).

The validator (any validator) computes mecid-1 weights by fetching the published
HF windows — receipts + ticks — from HF_PUBLIC_BASE, grading each resolved call,
and running the same gated scoring as chef. This is what makes mecid-1 REPLAYABLE:
a validator that pulls the release and restarts sets mecid-1 weights out of the box,
identically to every other validator, exactly like it already does for mecid-0 off
the on-chain journal + R2 blobs.

Fetch is incremental and cached on disk so a running validator only pulls new
windows each tempo, and grading only happens once a call's horizon has elapsed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request

from . import config, hf

WINDOW_MS = 180_000
_UA = {"User-Agent": "sn89-validator/1.0"}


def _fetch_text(url: str, timeout: float = 15.0) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception:      # noqa: BLE001 — a missing/late window is normal
        return None


def _index(base: str) -> list[int]:
    txt = _fetch_text(base.rstrip("/") + "/index.json")
    if not txt:
        return []
    try:
        return sorted(int(w) for w in json.loads(txt).get("windows", []))
    except Exception:      # noqa: BLE001
        return []


def load_hf_lock_rows(base: str, since_ms: int) -> list:
    """HF submissions as cross-mechanism lock rows, from the PUBLIC window logs.

    The LF half of the §9.1 pair lock (`validator.reveal`) resolves through here.
    It must read the PUBLISHED logs, not our live ingest state: an LF void has to
    be reproducible by anyone replaying the journal, and only the published,
    Merkle-anchored windows are available to them.

    Returns rows in `hf.build_lock_index` shape: (hotkey, PAIR, hf.MECID, ts_ms),
    timestamped at `grid_t0_ms` — the same deterministic grid point the call is
    priced and graded at, never `t_recv_us`, which no third party can replay.

    RAISES `hf.HFLockFeedError` rather than returning a short list. An empty feed
    is indistinguishable from "nobody submitted", so a swallowed fetch error
    silently disables the lock and re-opens the double-pay — which is exactly how
    the LF side sat unenforced while the bot told users it was locked. The caller
    must treat a raise as "cannot decide yet" and leave the call sealed.
    """
    idx = _fetch_text(base.rstrip("/") + "/index.json")
    if idx is None:
        raise hf.HFLockFeedError(f"index.json unreachable at {base}")
    try:
        windows = sorted(int(w) for w in json.loads(idx).get("windows", []))
    except Exception as e:  # noqa: BLE001
        raise hf.HFLockFeedError(f"index.json unparseable: {e}") from e

    rows = []
    for w in windows:
        # A window that closed before the lock horizon can hold nothing that could
        # still be locking anything. Skipping it keeps this O(24h) forever.
        if w + WINDOW_MS < since_ms:
            continue
        txt = _fetch_text(f"{base.rstrip('/')}/{w}/receipts.jsonl")
        if txt is None:
            # Inside the horizon and indexed, but not fetchable. Unlike grading —
            # where a late window is normal and simply retries — this one decides
            # whether a call is voided, so under-reading it would silently
            # under-enforce. Fail closed and let the caller retry.
            raise hf.HFLockFeedError(f"window {w} indexed but not fetchable")
        for line in txt.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
            pair = (sub.get("payload") or {}).get("trade_pair")
            hk, ts = sub.get("hk"), rcpt.get("grid_t0_ms")
            if hk and pair and ts and int(ts) >= since_ms:
                rows.append((hk, str(pair).upper(), hf.MECID, int(ts)))
    return rows


def _db(cache_dir: str) -> sqlite3.Connection:
    os.makedirs(cache_dir, exist_ok=True)
    c = sqlite3.connect(os.path.join(cache_dir, "hf_grades.db"), timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS windows_seen (w INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS pending ("
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, "
              "direction TEXT, end_ms INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS grades ("
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, status TEXT)")
    return c


def _ticks_for(base: str, tick_dir: str, pair: str, t0_ms: int, end_ms: int) -> list:
    """Ticks for `pair` over [t0_ms, end_ms], fetching each covering window once and
    caching it on disk (the horizon of a 2h call spans ~40 windows)."""
    os.makedirs(tick_dir, exist_ok=True)
    out = []
    w = (t0_ms // WINDOW_MS) * WINDOW_MS
    while w <= end_ms:
        local = os.path.join(tick_dir, f"{w}.ticks.jsonl")
        if not os.path.exists(local):
            txt = _fetch_text(f"{base.rstrip('/')}/{w}/ticks.jsonl")
            if txt is not None:
                tmp = local + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(txt)
                os.replace(tmp, local)
        try:
            with open(local, encoding="utf-8") as fh:
                for line in fh:
                    d = json.loads(line)
                    if d.get("a") == pair and t0_ms <= int(d["t"]) <= end_ms:
                        out.append(d)
        except (OSError, ValueError):
            pass
        w += WINDOW_MS
    out.sort(key=lambda d: int(d["t"]))
    return out


def sync_and_grade(base: str, cache_dir: str, now: float) -> None:
    """Pull new windows' receipts into `pending`, then grade any pending call whose
    horizon has elapsed. Incremental — safe to call every tempo."""
    now_ms = int(now * 1000)
    db = _db(cache_dir)
    tick_dir = os.path.join(cache_dir, "ticks")

    seen = {r[0] for r in db.execute("SELECT w FROM windows_seen")}
    graded = {r[0] for r in db.execute("SELECT key FROM grades")}
    for w in _index(base):
        if w in seen:
            continue
        txt = _fetch_text(f"{base.rstrip('/')}/{w}/receipts.jsonl")
        if txt is None:
            continue                        # not published yet — retry next tempo
        for line in txt.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
            hk, seq = sub.get("hk"), sub.get("seq")
            key = f"{hk}:{seq}"
            if not hk or key in graded:
                continue
            p = sub.get("payload") or {}
            pair = p.get("trade_pair")
            t0_ms = rcpt.get("grid_t0_ms")
            board = hf.hf_bands_as_of(t0_ms / 1000.0) if t0_ms else None
            if not board or pair not in board:
                continue
            _, _, horizon_s, _ = board[pair]
            db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?)",
                       (key, hk, int(t0_ms), pair, p.get("direction"),
                        int(t0_ms) + horizon_s * 1000))
        db.execute("INSERT OR IGNORE INTO windows_seen VALUES (?)", (w,))
    db.commit()

    # grade everything now resolved
    due = db.execute("SELECT key, hk, t0_ms, pair, direction, end_ms FROM pending "
                     "WHERE end_ms <= ?", (now_ms,)).fetchall()
    for key, hk, t0_ms, pair, direction, end_ms in due:
        board = hf.hf_bands_as_of(t0_ms / 1000.0)
        if not board or pair not in board:
            db.execute("DELETE FROM pending WHERE key=?", (key,))
            continue
        tp, sl, horizon_s, _ = board[pair]
        ticks = _ticks_for(base, tick_dir, pair, int(t0_ms), int(end_ms))
        entry = hf.price_at(ticks, int(t0_ms))
        g = hf.grade(pair, direction, entry, tp, sl, int(t0_ms), horizon_s, ticks)
        db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?)",
                   (key, hk, int(t0_ms), pair, g["status"]))
        db.execute("DELETE FROM pending WHERE key=?", (key,))
    db.commit()
    db.close()


def _history(cache_dir: str):
    db = _db(cache_dir)
    dec: dict = {}
    fs: dict = {}
    for hk, t0_ms, status in db.execute("SELECT hk, t0_ms, status FROM grades"):
        t0 = t0_ms / 1000.0
        fs[hk] = min(fs.get(hk, t0), t0)
        if status in ("won", "lost"):
            dec.setdefault(hk, []).append((t0, status == "won", False))
    db.close()
    for v in dec.values():
        v.sort(key=lambda x: x[0])
    return dec, fs


def mecid1_weights(uid_by_hk: dict, now: float | None = None,
                   base: str | None = None, cache_dir: str | None = None) -> dict:
    """{uid: weight} for mecid 1, graded from the PUBLIC logs. The validator's
    single entry point — mirrors replay.weights_from_journal for mecid 0."""
    now = time.time() if now is None else now
    base = base or hf.HF_PUBLIC_BASE
    cache_dir = cache_dir or os.path.expanduser("~/.sn89/hf-grade")
    sync_and_grade(base, cache_dir, now)
    dec, fs = _history(cache_dir)
    return hf.hf_compute_weights(dec, fs, uid_by_hk, now)
