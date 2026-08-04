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

# Bump to force every validator to re-derive its grades from the published feed on
# upgrade. A grading bug fixed in code is only half a fix: the wrong grades are
# already written and `grades` is never revisited, so without this the corrected
# rule applies to future calls only and the two halves of the board disagree
# forever. v2: the incomplete-tick-series wash bug below. v3: the same-mechanism
# open-position gate (hf.check_pair_open) — it voids calls already written as graded,
# so every validator must re-derive rather than apply it to new calls only.
GRADER_VERSION = 3

# A window is published shortly AFTER it closes, so a call whose horizon has only
# just elapsed is graded against a series that is still arriving. Wait this long
# past the horizon before grading. Measured on the 07-24..07-26 corpus: 42 of 307
# calls (13.7%) were graded on an incomplete series, 41 of them recorded as `wash`
# when the tick record shows they were decisive.
GRADE_SETTLE_S = int(os.getenv("SN89_HF_GRADE_SETTLE_S", "900"))

# ...but an EMPTY window is never sealed (anchor_loop only seals windows that got a
# receipt), so a permanent gap is indistinguishable from a slow publish and would
# wedge the call in `pending` forever. After this long past the horizon, grade on
# whatever the feed ever published. Every validator crosses the deadline against
# the same (by then final) published set, so they still converge.
GRADE_ABANDON_S = int(os.getenv("SN89_HF_GRADE_ABANDON_S", "21600"))


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
            payload = sub.get("payload") or {}
            # Closers receipts share these windows with HF calls. A HOLD/CLOSE
            # vote is not a directional call on the pair — it is a vote on our
            # open position — so it must NOT lock the pair for the miner's own
            # LF call. Without this filter a closers vote voided the miner's
            # next LF call on that pair (Brian, 2026-08-04).
            if str(payload.get("kind", "")) == "closers":
                continue
            pair = payload.get("trade_pair")
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
              "key TEXT PRIMARY KEY, hk TEXT, t0_ms INTEGER, pair TEXT, status TEXT, "
              "open_until_ms INTEGER)")
    # `open_until_ms` is v3. A GRADER_VERSION bump clears the ROWS but CREATE TABLE
    # IF NOT EXISTS leaves an existing table's SHAPE alone, so an upgraded validator
    # needs the column added explicitly or every insert below fails.
    if not any(r[1] == "open_until_ms"
               for r in c.execute("PRAGMA table_info(grades)")):
        c.execute("ALTER TABLE grades ADD COLUMN open_until_ms INTEGER")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = c.execute("SELECT v FROM meta WHERE k='grader_version'").fetchone()
    have = int(row[0]) if row else 0
    if have != GRADER_VERSION:
        # Drop everything DERIVED and let it rebuild from the published windows.
        # The tick cache is untouched — it is the raw feed, not a derivation.
        c.execute("DELETE FROM grades")
        c.execute("DELETE FROM pending")
        c.execute("DELETE FROM windows_seen")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('grader_version',?)",
                  (str(GRADER_VERSION),))
        c.commit()
    return c


def _ticks_for(base: str, tick_dir: str, pair: str,
               t0_ms: int, end_ms: int) -> tuple[list, list]:
    """(ticks, missing_windows) for `pair` covering the ENTRY at t0 and the walk
    to end_ms.

    `missing_windows` is the whole point of the second return value. This used to
    swallow an unfetchable window and hand grade() whatever it happened to have —
    and since a short series simply never touches a level, the call resolved
    `wash`. `wash` is indistinguishable from "the market did nothing", so the
    defect was invisible: on the 2026-07-24..26 corpus 42 of 307 calls (13.7%)
    were graded against an incomplete series, 41 of them recorded `wash` when the
    tick record shows they were decisive. Every one of them spanned a window this
    function could not fetch. It is a CONSENSUS defect as much as an accuracy one:
    two validators reading the feed at different moments saw different series and
    wrote different grades for the same call.

    A gap is reported, never interpreted — the caller decides whether to wait for
    the window or accept that it will never arrive.

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
    out, missing = [], []
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
        rows = []
        try:
            with open(local, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("a") == pair and int(d["t"]) <= end_ms:
                        rows.append(d)
        except (OSError, ValueError, KeyError):
            # unreachable OR truncated/corrupt — either way this window's prices
            # are not in hand, and a partial parse is exactly the silent
            # short-series that produced the bogus washes. Report, drop, retry.
            missing.append(w)
        else:
            out.extend(rows)
        w += WINDOW_MS
    out.sort(key=lambda d: int(d["t"]))
    return out, missing


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
            if str(p.get("kind", "")) == "closers":
                continue        # closers competition — graded by closers.py, not HF
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

    # Grade everything now resolved AND settled. The settle delay is not politeness
    # — the window covering the last second of a horizon publishes after that
    # second, so grading at end_ms exactly is grading a series that is still
    # arriving.
    # ORDER BY end_ms is load-bearing, not tidiness: the open-position gate below
    # asks whether an EARLIER call on the same pair was still open, which is only
    # answerable once that earlier call is itself graded. Same pair -> same horizon,
    # so earlier t0 means earlier end_ms, and this ordering settles predecessors
    # first within a batch.
    due = db.execute("SELECT key, hk, t0_ms, pair, direction, end_ms FROM pending "
                     "WHERE end_ms <= ? ORDER BY end_ms, t0_ms, key",
                     (now_ms - GRADE_SETTLE_S * 1000,)).fetchall()
    for key, hk, t0_ms, pair, direction, end_ms in due:
        board = hf.hf_bands_as_of(t0_ms / 1000.0)
        if not board or pair not in board:
            db.execute("DELETE FROM pending WHERE key=?", (key,))
            continue
        tp, sl, horizon_s, _ = board[pair]
        # ...and ordering alone is not enough: a predecessor stuck in `pending` on a
        # tick gap is NOT in this batch, so grading its successor now would read an
        # empty prior set and pass a call the gate should void. Wait for it. It
        # either grades on a later tempo or is abandoned, and either way it lands
        # before this one — its end_ms is smaller.
        blocked = db.execute(
            "SELECT 1 FROM pending WHERE hk=? AND pair=? AND t0_ms<? LIMIT 1",
            (hk, pair, int(t0_ms))).fetchone()
        if blocked:
            continue
        ticks, missing = _ticks_for(base, tick_dir, pair, int(t0_ms), int(end_ms))
        if missing and now_ms < int(end_ms) + GRADE_ABANDON_S * 1000:
            # Prices we do not hold. Leave the call in `pending` and retry next
            # tempo rather than grading a hole as `wash` — a grade is written once
            # and never revisited, so guessing here is permanent.
            continue
        entry = hf.price_at(ticks, int(t0_ms))
        # The same-mechanism open-position gate. A prior call still holding this pair
        # at t0 voids this one — the trader stacked a second position on a view the
        # board had not answered yet. Only NON-VOID predecessors hold the pair
        # (open_until_ms is written as t0_ms for a void), so a refusal never chains.
        prior_open = [r[0] for r in db.execute(
            "SELECT open_until_ms FROM grades WHERE hk=? AND pair=? AND t0_ms<? "
            "AND status!='void' AND open_until_ms IS NOT NULL",
            (hk, pair, int(t0_ms)))]
        try:
            hf.check_pair_open(prior_open, int(t0_ms), t0_ms / 1000.0)
        except hf.HFRejected:
            db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?)",
                       (key, hk, int(t0_ms), pair, "void", int(t0_ms)))
            db.execute("DELETE FROM pending WHERE key=?", (key,))
            continue
        g = hf.grade(pair, direction, entry, tp, sl, int(t0_ms), horizon_s, ticks)
        # grade()'s own exit_ms IS open_until_ms on a complete series — the touch for
        # won/lost, t_end for wash — so take it rather than re-walking. Deriving the
        # held-until from the grade makes the two impossible to drift apart, which is
        # the failure that would void legal calls.
        held = int(t0_ms) if g["status"] == "void" else int(g["exit_ms"])
        db.execute("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?)",
                   (key, hk, int(t0_ms), pair, g["status"], int(held)))
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
    cache_dir = cache_dir or os.path.expanduser(
        os.getenv("SN89_HF_GRADE_CACHE", "~/.sn89/hf-grade"))
    sync_and_grade(base, cache_dir, now)
    dec, fs, subs, graded = _history(cache_dir)
    return hf.hf_compute_weights(dec, fs, uid_by_hk, now, subs, graded)
