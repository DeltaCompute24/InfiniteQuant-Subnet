"""Markets V4 oracle price record: format, integrity, anchoring and the consensus reader.

Up/Down markets that start at or after config.MARKETS_ORACLE_FROM_UNIX settle on Hyperliquid's
oraclePx instead of the HF book mid. This module is the only definition of that record. It is a
separate series from the HF/LF tick windows (`hf.py`, `/var/lib/sn89-hf/ticks`), which it neither
reads nor writes.

Record
    The recorder (neurons/markets_oracle_recorder.py) writes one row whenever an asset's oraclePx
    changes, plus a heartbeat row every HEARTBEAT_S while it does not:
        {"t": <receive ms>, "a": "<market asset>", "o": "<oraclePx exactly as published>"}
    Rows are grouped into WINDOW_S windows ordered by (t, a, o). Each window publishes
        <base>/<w>/oracle.jsonl   the rows
        <base>/<w>/oracle.json    {"w", "n", "root"}   root = merkle_root(blake2b(row_bytes))
    `o` stays a decimal string end to end: the root is over the exact published characters, and
    every price computation goes through Decimal.

Anchor (hash chain)
    Every BATCH_WINDOWS windows the anchor service publishes a batch record
        {"w0", "n", "root", "prev", "link"}   root = merkle over (w, window root) pairs,
                                              link = blake2b(prev || w0 || n || root)
    and commits `sn89mo:1:<w0 s>:<n>:<link>` from the Markets entity hotkey (UID 199). The on-chain
    slot holds only the latest commitment, so the chain is what makes all history checkable from one
    read: walking the published batches from genesis must reproduce every link, and the link on
    chain must be one of them. Rewriting any published window breaks every later link.
    (The owner hotkey's slot carries the HF anchors and is never used here.)

Consensus reader
    `rows_for(...)` fetches the windows covering a span, re-derives each window's root from its rows
    and refuses (reports missing) any window that does not match its published root. Grading then
    uses `value_at`, `twap` and `fill_at`, all in Decimal on the value IN EFFECT: the last row at or
    before an instant, valid while it is at most config.MARKETS_ORACLE_MAX_AGE_S old.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal

from . import config, hf

WINDOW_S = 180
WINDOW_MS = WINDOW_S * 1000
HEARTBEAT_S = 30
BATCH_WINDOWS = 3                 # one commitment per 9 min: 8 per tempo x ~100 B < 3100 B space
ANCHOR_PREFIX = "sn89mo:1:"
GENESIS = "00" * 32


def window_of(t_ms: int) -> int:
    return (int(t_ms) // WINDOW_MS) * WINDOW_MS


# ── format and integrity ──────────────────────────────────────────────────────
def row_bytes(r: dict) -> bytes:
    return f"{int(r['t'])}|{r['a']}|{r['o']}".encode()


def row_order(r: dict):
    return (int(r["t"]), str(r["a"]), str(r["o"]))


def window_root(rows: list[dict]) -> str:
    return hf.merkle_root([hashlib.blake2b(row_bytes(r), digest_size=32).digest()
                           for r in sorted(rows, key=row_order)])


def batch_root(windows: list[tuple[int, str]]) -> str:
    return hf.merkle_root([hashlib.blake2b(f"{int(w)}|{root}".encode(), digest_size=32).digest()
                           for w, root in sorted(windows)])


def chain_link(prev: str, w0: int, n: int, root: str) -> str:
    return hashlib.blake2b(bytes.fromhex(prev) + f"|{int(w0)}|{int(n)}|".encode() + bytes.fromhex(root),
                           digest_size=32).hexdigest()


def encode_anchor(w0: int, n: int, link: str) -> str:
    s = f"{ANCHOR_PREFIX}{int(w0) // 1000}:{int(n)}:{link}"
    if len(s.encode()) > hf.ANCHOR_MAX_BYTES:
        raise ValueError("oracle anchor exceeds the commitment field")
    return s


def decode_anchor(s: str) -> dict | None:
    if not s or not str(s).startswith(ANCHOR_PREFIX):
        return None
    try:
        w0, n, link = str(s)[len(ANCHOR_PREFIX):].split(":")
        return {"w0": int(w0) * 1000, "n": int(n), "link": link}
    except ValueError:
        return None


def verify_chain(batches: list[dict], window_roots: dict[int, str] | None = None) -> tuple[bool, str]:
    """Walk published batches from genesis. Each must link to the previous one; when window roots
    are given, each batch root must be the root of exactly those windows. -> (ok, why)."""
    prev = GENESIS
    for b in sorted(batches, key=lambda x: int(x["w0"])):
        if b.get("prev") != prev:
            return False, f"batch {b.get('w0')}: prev does not match the chain"
        if chain_link(prev, int(b["w0"]), int(b["n"]), b["root"]) != b.get("link"):
            return False, f"batch {b.get('w0')}: link does not recompute"
        if window_roots is not None:
            ws = [int(b["w0"]) + i * WINDOW_MS for i in range(int(b["n"]))]
            if any(w not in window_roots for w in ws):
                return False, f"batch {b.get('w0')}: window missing"
            if batch_root([(w, window_roots[w]) for w in ws]) != b["root"]:
                return False, f"batch {b.get('w0')}: root does not recompute"
        prev = b["link"]
    return True, "ok"


# ── consensus reader ──────────────────────────────────────────────────────────
_UA = {"User-Agent": "sn89-validator/1.0"}


def _fetch(url: str) -> str | None:
    from .hf_grade import _fetch_text
    return _fetch_text(url, attempts=2)


def load_window(base: str, cache_dir: str, w: int) -> list[dict] | None:
    """One window's rows, verified against its published root; cached once verified. None when the
    window is not published (yet) or does not verify."""
    os.makedirs(cache_dir, exist_ok=True)
    local = os.path.join(cache_dir, f"{w}.oracle.jsonl")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    meta_txt = _fetch(f"{base.rstrip('/')}/{w}/oracle.json")
    rows_txt = _fetch(f"{base.rstrip('/')}/{w}/oracle.jsonl")
    if meta_txt is None or rows_txt is None:
        return None
    try:
        meta = json.loads(meta_txt)
        rows = [json.loads(l) for l in rows_txt.splitlines() if l.strip()]
    except ValueError:
        return None
    if int(meta.get("w", -1)) != int(w) or int(meta.get("n", -1)) != len(rows) \
            or window_root(rows) != meta.get("root") \
            or any(window_of(int(r["t"])) != int(w) for r in rows):
        return None
    tmp = local + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in sorted(rows, key=row_order)))
    os.replace(tmp, local)
    return rows


def rows_for(base: str, cache_dir: str, asset: str, t0_ms: int, t1_ms: int) -> tuple[list[dict], list[int]]:
    """(rows of `asset` from one max-age before t0 through t1, sorted; windows that are missing).
    The lead-in reaches back far enough to find the value in effect at t0."""
    lo = window_of(int(t0_ms) - config.MARKETS_ORACLE_MAX_AGE_S * 1000)
    out, missing = [], []
    w = lo
    while w <= window_of(int(t1_ms)):
        rows = load_window(base, cache_dir, w)
        if rows is None:
            missing.append(w)
        else:
            out.extend(r for r in rows if r["a"] == asset)
        w += WINDOW_MS
    out.sort(key=row_order)
    return out, missing


def value_at(rows: list[dict], t_ms: int) -> tuple[int, Decimal] | None:
    """(row t, value) in effect at t_ms: the last row at or before it, if not older than max age."""
    best = None
    for r in rows:
        if int(r["t"]) <= t_ms:
            best = r
        else:
            break
    if best is None or int(best["t"]) < t_ms - config.MARKETS_ORACLE_MAX_AGE_S * 1000:
        return None
    return int(best["t"]), Decimal(str(best["o"]))


def twap(rows: list[dict], t0_ms: int, t1_ms: int) -> Decimal | None:
    """Time-weighted average of the value in effect over [t0, t1]. None if any instant of it has
    no value in effect (a gap longer than the max age)."""
    start = value_at(rows, t0_ms)
    if start is None:
        return None
    max_age = config.MARKETS_ORACLE_MAX_AGE_S * 1000
    total, cur_t, cur_v, last_row_t = Decimal(0), int(t0_ms), start[1], start[0]
    for r in rows:
        t = int(r["t"])
        if t <= t0_ms:
            continue
        if t > t1_ms:
            break
        if t - last_row_t > max_age:
            return None
        total += cur_v * (t - cur_t)
        cur_t, cur_v, last_row_t = t, Decimal(str(r["o"])), t
    if t1_ms - last_row_t > max_age:
        return None
    total += cur_v * (int(t1_ms) - cur_t)
    return total / Decimal(int(t1_ms) - int(t0_ms))


def resolve(rows: list[dict], start: int, end: int) -> tuple[str, float | None, float | None]:
    """-> (UP|DOWN|VOID, target, settle): 60 s time-weighted oracle averages ending at start / end."""
    avg = config.MARKETS_AVG_S * 1000
    tg = twap(rows, start * 1000 - avg, start * 1000)
    st = twap(rows, end * 1000 - avg, end * 1000)
    target = float(tg) if tg is not None else None
    settle = float(st) if st is not None else None
    if tg is None or st is None:
        return "VOID", target, settle
    return ("UP" if st >= tg else "DOWN"), target, settle


def fill_at(rows: list[dict], receipt_ms: int) -> tuple[int, float] | None:
    """(fill instant ms, value) for a bet received at receipt_ms: the value in effect FILL_DELAY on."""
    t = int(receipt_ms) + config.MARKETS_ORACLE_FILL_DELAY_S * 1000
    v = value_at(rows, t)
    return (t, float(v[1])) if v is not None else None
