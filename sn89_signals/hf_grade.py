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


def _receipt_cache_dir() -> str:
    return os.getenv("SN89_HF_RECEIPT_CACHE",
                     os.path.join(os.path.expanduser("~/.sn89/hf-grade"), "receipts"))


def _cached_receipts(base: str, w: int) -> str | None:
    """receipts.jsonl for one window, fetched once and kept on disk.

    A published window is SEALED — the anchor retires it and never rewrites it —
    so refetching it is pure waste. It matters because load_hf_lock_rows walks
    every window in the 24 h lock horizon (480 of them at a 3-minute window), and
    it is rebuilt every HF_LOCK_REFRESH_S; without this the rebuild takes longer
    than the refresh interval and the validator loop does nothing but refetch.
    A MISS is never cached — that is the fail-closed signal the caller needs.

    Keyed by (base, window), never window alone: window ids are just wall-clock
    grid points, so two different feeds — another validator's base, or a test's
    tmp dir — collide on them, and a cache hit from the wrong feed would satisfy
    a fetch that must fail closed."""
    import hashlib
    d = os.path.join(_receipt_cache_dir(),
                     hashlib.blake2b(base.rstrip("/").encode(),
                                     digest_size=8).hexdigest())
    os.makedirs(d, exist_ok=True)
    local = os.path.join(d, f"{w}.receipts.jsonl")
    try:
        with open(local, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        pass
    txt = _fetch_text(f"{base.rstrip('/')}/{w}/receipts.jsonl")
    if txt is None:
        return None
    tmp = local + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(tmp, local)
    except OSError:
        pass                                # cache is an optimisation, not a source
    return txt


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
        txt = _cached_receipts(base, w)
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
    """Ticks for `pair` covering the ENTRY at t0 and the walk to end_ms.

    Starts ONE window before t0's window and keeps every tick from there through
    end_ms. price_at needs the last tick AT OR BEFORE t0 for the entry, and a
    crypto tick (~250 ms apart, non-zero ms) almost never lands exactly on the
    250 ms grid point, so the entry tick is the one just BEFORE t0. The old bound
    `t0_ms <= t` dropped it and returned entry=None → every crypto call voided,
    while forex/gold survived only because their quotes carry ms=000 and align to
    the 1 s grid. The extra leading window guarantees a pre-t0 tick even when t0
    sits at the very start of its own window. grade() ignores ticks at or before
    t0 for the TP/SL walk, so carrying them is harmless.

    Each window is fetched once and cached on disk (a 2 h call spans ~40)."""
    os.makedirs(tick_dir, exist_ok=True)
    out = []
    w = (t0_ms // WINDOW_MS - 1) * WINDOW_MS
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
                    if d.get("a") == pair and int(d["t"]) <= end_ms:
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
    """(decisive_by_hk, first_seen_by_hk, submissions_by_hk, graded_by_hk).

    submissions_by_hk carries EVERY resolved submission's t0_ms (won/lost/wash/
    void) — the HF eligibility gate counts accepted participation, not just
    decisive outcomes. Resolved-only (not the ephemeral `pending` table) so the
    set is deterministic from the published windows and every validator computes
    the same eligibility instant; a call's horizon is at most 2h, so the lag is
    immaterial to an 8-trading-day gate.
    """
    db = _db(cache_dir)
    dec: dict = {}
    fs: dict = {}
    subs: dict = {}
    graded: dict = {}
    for hk, t0_ms, status in db.execute("SELECT hk, t0_ms, status FROM grades"):
        t0 = t0_ms / 1000.0
        fs[hk] = min(fs.get(hk, t0), t0)
        subs.setdefault(hk, []).append(int(t0_ms))
        if status in ("won", "lost", "wash"):
            # graded (not void): what scoring.efficiency_multiplier prices. Note HF
            # writes 'wash', LF writes 'washed'.
            graded.setdefault(hk, []).append((t0, status == "wash"))
        if status in ("won", "lost"):
            dec.setdefault(hk, []).append((t0, status == "won", False))
    db.close()
    for v in dec.values():
        v.sort(key=lambda x: x[0])
    for v in graded.values():
        v.sort(key=lambda x: x[0])
    return dec, fs, subs, graded


def mecid1_weights(uid_by_hk: dict, now: float | None = None,
                   base: str | None = None, cache_dir: str | None = None) -> dict:
    """{uid: weight} for mecid 1, graded from the PUBLIC logs. The validator's
    single entry point — mirrors replay.weights_from_journal for mecid 0."""
    now = time.time() if now is None else now
    base = base or hf.HF_PUBLIC_BASE
    cache_dir = cache_dir or os.path.expanduser("~/.sn89/hf-grade")
    sync_and_grade(base, cache_dir, now)
    dec, fs, subs, graded = _history(cache_dir)
    return hf.hf_compute_weights(dec, fs, uid_by_hk, now, subs, graded)
