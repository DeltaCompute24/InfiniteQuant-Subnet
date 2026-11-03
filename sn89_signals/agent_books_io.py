"""Agent competition v2 plumbing around the pure engine (agent_books.py).

Everything here is deterministic given its inputs; the network, the chain and the clock
come in as arguments or injected callables, so a test (or a third-party replayer) feeds
the same bytes and gets the same weekly file.

  receipts     load_receipts(): JSON lines from a directory or URLs -> verified, deduped,
               ordered receipts plus a {reason: count} of what was dropped.
  anchoring    v2 HF window anchor that also binds the window's agent receipts and the
               bid/ask of its ticks (§ ANCHOR below). Pure functions only; the live HF
               anchor publisher is NOT changed here.
  ticks        thin_day(): one streaming pass over a UTC day of HF tick shards keeping only
               the ticks any book can read; book_ticks(): one book's exact slice of that.
               Both are EXACT: replay_book over the slice equals replay_book over the full
               stream (test_agent_books_io proves it on random books).
  registry     merge_registry()/registry_as_of(): sticky `sn89entity:<coldkey>` journal plus
               config.AGENT_BOOKS_GRANDFATHERED.
  weekly       build_week(): the FROZEN weekly file, dumps_week(): its canonical bytes.
  validator    pay_week_start_ms(), load_week_shares(), agentbook_vector().

ANCHOR (decision, 2026-11-03)
-----------------------------
Agent receipts are bound to time by the HF anchor, not by the entity's LF batch commit.
  * The entity hotkey's single CommitmentOf slot already carries LF batches AND Markets
    oracle anchors under one lock; it is latest-wins and validators do not scan its
    history, so an hourly receipts root written there could be overwritten unseen. The HF
    anchor hotkey commits every 180 s, does nothing else, and its windows are already
    published (receipts + ticks) and replayed by every validator.
  * The book is graded against those same ticks. One window root that binds the
    receipts, the agent receipts AND the quotes the books fill at is what makes the
    result falsifiable: neither the targets nor the prices can be restated afterwards.
  * The v1 tick root binds only (a, t, p). Books fill at bid/ask, so v2 adds a quote root
    over (a, t, b, k, p).
v2 anchor string: `sn89hf:2:<w_s>:<n>:<tick_n>:<agent_n>:<window_root_v2>` (<= 128 B,
~100 B charged; 24 per 72-min epoch = ~2,400 of the 3,100 B budget, as today).
window_root_v2 = blake2b-256(root || tick_root || quote_root || agent_root).
A receipt counts only if (1) the window containing its t_recv_ms is anchored with an
agent_root equal to receipts_root(all receipts of that window) and (2) the anchor block's
timestamp is no later than window end + AGENT_ANCHOR_MAX_LAG_MS.

What the live HF anchor publisher (neurons/hf_anchor.py) has to add, NOT done here:
  1. in _roots(w): read the agent receipts with window_start_ms(t_recv_ms) == w from the
     intake's books/*.jsonl (load_receipts, signatures checked), agent_root =
     agent_books.receipts_root(them), agent_n = len; quote_root = quote_root(ticks);
  2. commit encode_anchor_v2(...) instead of hf.encode_anchor(...) from the arm instant,
     and write agent_root / agent_n / quote_root (+ the anchor block's timestamp as
     anchor_ts_ms once the commit lands) into <w>.anchor.json; publish the window's agent
     receipts as <w>/agent.jsonl beside receipts.jsonl / ticks.jsonl;
  3. anchor EVERY window that has ticks, not only receipt-bearing ones: a book's marks
     and day-end equity come from quiet windows too. 480 anchors/day fits the budget
     (24 per epoch, ~100 B each, 3,100 B MaxSpace);
  4. every reader of the HF anchor (hf.decode_anchor / verify_anchor callers, hf_cycle)
     must accept the v2 prefix, and the HF grader keeps verifying `root` and `tick_root`
     through window_root_v2.
The intake must write a receipt before its window is sealed (TICK_SETTLE_WINDOWS = 2
windows after close today); a receipt written later is unanchored and never counts.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import json
import math
import os

from . import agent_books as ab
from . import config, entity, hf

RECEIPT_V = 1
RECEIPT_KEYS = ("v", "entity", "account", "seq", "t_recv_ms", "targets", "sig")
WINDOW_MS = hf.ANCHOR_WINDOW_S * 1000


# ── receipts ─────────────────────────────────────────────────────────────────
def sr25519_verify(hotkey: str, message: bytes, sig_hex: str) -> bool:
    """The entity hotkey's sr25519 signature, checked the way hf_ingest checks miners'."""
    from bittensor_wallet import Keypair
    return bool(Keypair(ss58_address=hotkey).verify(message, bytes.fromhex(sig_hex)))


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def check_shape(r) -> str | None:
    if not isinstance(r, dict):
        return "bad_json"
    if r.get("v") != RECEIPT_V:
        return "bad_version"
    ent, acct = r.get("entity"), r.get("account")
    if not isinstance(ent, str) or not isinstance(acct, str):
        return "bad_shape"
    parsed = entity.parse(acct)
    if parsed is None:
        return "bad_account"
    if parsed[0] != ent:
        return "account_not_entity"
    if ent in config.SIGNALS_ENTITY_HOTKEYS and parsed[1] < config.AGENT_ACCOUNT_MIN:
        # Our entity's payouts route through the IQ Agent account numbering
        # (Markets signals_split.agent_payee); a lower number is a Markets/LF account.
        return "account_not_agent"
    if not _is_int(r.get("seq")) or r["seq"] < 0:
        return "bad_seq"
    if not _is_int(r.get("t_recv_ms")) or r["t_recv_ms"] <= 0:
        return "bad_time"
    tg = r.get("targets")
    if not isinstance(tg, dict) or not all(isinstance(k, str) for k in tg):
        return "bad_targets"
    sig = r.get("sig")
    if not isinstance(sig, str) or not sig:
        return "bad_sig"
    try:
        bytes.fromhex(sig)
    except ValueError:
        return "bad_sig"
    return None


def load_receipts(lines, verify=sr25519_verify, entities=None) -> tuple[list, dict]:
    """Lines -> (receipts ordered by agent_books.receipt_order_key, {drop_reason: count}).

    verify(hotkey, message, sig_hex) -> bool is injected (sr25519_verify in production).
    `entities`: optional allow-set of entity hotkeys (None = any).
    The same (account, seq) twice with identical content is one receipt ("duplicate");
    twice with DIFFERENT content is equivocation by the signer and BOTH are dropped
    ("seq_conflict"), so an entity can never choose afterwards which one counted."""
    drops: dict = {}

    def drop(reason):
        drops[reason] = drops.get(reason, 0) + 1

    by_key: dict = {}
    conflict: set = set()
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            drop("bad_json")
            continue
        why = check_shape(r)
        if why:
            drop(why)
            continue
        if entities is not None and r["entity"] not in entities:
            drop("unknown_entity")
            continue
        r = {k: r[k] for k in RECEIPT_KEYS}
        if not ab.verify_receipt(r, lambda m, s, hk=r["entity"]: verify(hk, m, s)):
            drop("bad_signature")
            continue
        key = (r["account"], r["seq"])
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = r
        elif hf.canonical_json(prev) == hf.canonical_json(r):
            drop("duplicate")
        else:
            conflict.add(key)
    for key in sorted(conflict):
        by_key.pop(key, None)
        drops["seq_conflict"] = drops.get("seq_conflict", 0) + 2
    out = sorted(by_key.values(), key=ab.receipt_order_key)
    return out, dict(sorted(drops.items()))


def iter_dir_lines(path: str):
    """Every line of every *.jsonl under `path`, files in name order."""
    try:
        names = sorted(n for n in os.listdir(path) if n.endswith(".jsonl"))
    except OSError:
        return
    for n in names:
        try:
            with open(os.path.join(path, n), encoding="utf-8") as fh:
                yield from fh
        except OSError:
            continue


def index_urls(index_url: str, idx) -> list:
    """Per-account log URLs from a books/index.json. Accepts a list or {"books"|"accounts":
    [...]} or a dict keyed by account; each entry may be a URL, a file name, an account id
    `<hk>_<n>`, a number n, or a dict carrying "url"/"file"/"n"/"account". Relative names
    resolve against the index's directory as `<n>.jsonl`."""
    base = index_url.rsplit("/", 1)[0]
    if isinstance(idx, dict):
        items = idx.get("books", idx.get("accounts"))
        if items is None:
            items = list(idx)
        elif isinstance(items, dict):
            items = list(items)
    else:
        items = idx
    out = []
    for it in items or []:
        if isinstance(it, dict):
            it = it.get("url") or it.get("file") or it.get("n") or it.get("account")
        if it is None or isinstance(it, bool):
            continue
        it = str(it)
        if "://" in it:
            out.append(it)
            continue
        if it.endswith(".jsonl"):
            out.append(f"{base}/{it}")
            continue
        parsed = entity.parse(it)
        n = str(parsed[1]) if parsed else it
        if n.isdigit():
            out.append(f"{base}/{n}.jsonl")
    return sorted(set(out))


def iter_url_lines(urls, fetch):
    """`fetch(url) -> str | None`. A URL ending in index.json lists the per-account logs
    (index_urls)."""
    for u in urls:
        txt = fetch(u)
        if txt is None:
            continue
        if u.endswith("index.json"):
            try:
                idx = json.loads(txt)
            except ValueError:
                continue
            yield from iter_url_lines(index_urls(u, idx), fetch)
            continue
        yield from txt.splitlines()


# ── anchoring (pure) ─────────────────────────────────────────────────────────
AGENT_ANCHOR_PREFIX = "sn89hf:2:"
# A receipt's window must be anchored no later than this after the window closes. The HF
# anchor lands ~2 windows after close today (TICK_SETTLE_WINDOWS); 15 min leaves room for a
# retry and is far too short to trade on knowledge of the window's own outcome.
AGENT_ANCHOR_MAX_LAG_MS = 15 * 60_000


def quote_tick_bytes(t: dict) -> bytes:
    return hf.canonical_json({k: t[k] for k in ("a", "b", "k", "p", "t") if k in t}).encode()


def quote_root(ticks: list) -> str:
    return hf.merkle_root([hashlib.blake2b(quote_tick_bytes(t), digest_size=32).digest()
                           for t in sorted(ticks, key=hf.tick_order_key)])


def window_start_ms(t_ms: int) -> int:
    return hf.window_start_ms(t_ms)


def window_root_v2(root_hex: str, tick_root_hex: str, quote_root_hex: str,
                   agent_root_hex: str) -> str:
    return hashlib.blake2b(bytes.fromhex(root_hex) + bytes.fromhex(tick_root_hex)
                           + bytes.fromhex(quote_root_hex) + bytes.fromhex(agent_root_hex),
                           digest_size=32).hexdigest()


def encode_anchor_v2(w_start_ms: int, n: int, tick_n: int, agent_n: int, root_hex: str,
                     tick_root_hex: str, quote_root_hex: str, agent_root_hex: str) -> str:
    s = (f"{AGENT_ANCHOR_PREFIX}{int(w_start_ms) // 1000}:{int(n)}:{int(tick_n)}:"
         f"{int(agent_n)}:"
         f"{window_root_v2(root_hex, tick_root_hex, quote_root_hex, agent_root_hex)}")
    if len(s.encode()) > hf.ANCHOR_MAX_BYTES:
        raise ValueError(f"anchor {len(s.encode())}B exceeds the {hf.ANCHOR_MAX_BYTES}B field")
    return s


def decode_anchor_v2(s) -> dict | None:
    if not isinstance(s, str) or not s.startswith(AGENT_ANCHOR_PREFIX):
        return None
    try:
        w_s, n, tick_n, agent_n, wroot = s[len(AGENT_ANCHOR_PREFIX):].split(":")
        if len(wroot) != 64:
            return None
        bytes.fromhex(wroot)
        return {"w": int(w_s) * 1000, "n": int(n), "tick_n": int(tick_n),
                "agent_n": int(agent_n), "window_root": wroot}
    except ValueError:
        return None


def verify_anchor_v2(onchain: str, root_hex: str, tick_root_hex: str, quote_root_hex: str,
                     agent_root_hex: str, n: int, tick_n: int, agent_n: int) -> bool:
    a = decode_anchor_v2(onchain)
    return bool(a and a["n"] == n and a["tick_n"] == tick_n and a["agent_n"] == agent_n
                and a["window_root"] == window_root_v2(root_hex, tick_root_hex,
                                                       quote_root_hex, agent_root_hex))


def receipts_by_window(receipts: list) -> dict:
    out: dict = {}
    for r in receipts:
        out.setdefault(window_start_ms(r["t_recv_ms"]), []).append(r)
    return out


def filter_anchored(receipts: list, windows: dict,
                    max_lag_ms: int = AGENT_ANCHOR_MAX_LAG_MS) -> tuple[list, dict]:
    """Keep the receipts whose window is anchored in time with a matching agent root.

    windows: {w_start_ms: {"agent_root": hex, "agent_n": int, "anchor_ts_ms": int}}, built
    from the published anchor.json halves after verify_anchor_v2 against the chain, with
    anchor_ts_ms = the anchor block's timestamp. All or nothing per window: a root mismatch
    means the published log is not what was anchored, so none of it counts."""
    kept, drops = [], {}
    for w, rs in sorted(receipts_by_window(receipts).items()):
        a = windows.get(w)
        if a is None:
            why = "unanchored"
        elif int(a.get("anchor_ts_ms", 1 << 62)) > w + WINDOW_MS + int(max_lag_ms):
            why = "anchored_late"
        elif int(a.get("agent_n", -1)) != len(rs) or a.get("agent_root") != ab.receipts_root(rs):
            why = "anchor_mismatch"
        else:
            kept.extend(rs)
            continue
        drops[why] = drops.get(why, 0) + len(rs)
    return sorted(kept, key=ab.receipt_order_key), dict(sorted(drops.items()))


# ── universe ─────────────────────────────────────────────────────────────────
def book_universe_through(t_unix: float) -> dict:
    """{PAIR: asset_class}: every pair ever listed on the HF board or in the custom
    universe at or before t (latest class wins). A UNION, so a pair later delisted cannot
    retroactively change an already-frozen week; FX stays in it and is refused by the
    rules' excluded_classes."""
    rows = []
    for eff, board in hf.HF_BANDS_HISTORY:
        if t_unix >= eff and board:
            rows.append((eff, 0, {p: r[3] for p, r in board.items()}))
    for eff, uni in hf.HF_CUSTOM_UNIVERSE_HISTORY:
        if t_unix >= eff and uni:
            rows.append((eff, 1, {p: v[0] for p, v in uni.items()}))
    out: dict = {}
    for _eff, _k, m in sorted(rows, key=lambda x: (x[0], x[1])):
        out.update(m)
    return dict(sorted(out.items()))


# ── ticks: exact thinning ────────────────────────────────────────────────────
def touched_pairs(receipts: list, universe: dict) -> set:
    return {p for r in receipts for p in (r.get("targets") or {}) if p in universe}


def _fill_delay(rules) -> int:
    return int(rules["fill_delay_ms"])


def _pair_of_line(line: str) -> str | None:
    if line.startswith('{"a":"'):
        j = line.find('"', 6)
        if j > 6:
            return line[6:j]
    return None


def window_lines_local(src_dir: str, w: int):
    """A SEALED local shard's lines, or None. The `.ticks.json` manifest is the seal."""
    p = os.path.join(src_dir, f"{w}.ticks.jsonl")
    if not (os.path.exists(p) and os.path.exists(p[:-6] + ".json")):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.readlines()
    except OSError:
        return None


def thin_day(day_ms: int, receipts: list, pairs: set, read_window, rules=None,
             until_ms: int | None = None) -> tuple[list, list]:
    """One streaming pass over the tick shards of UTC day [day_ms, day_ms + 1d).

    Keeps exactly what any book can read during that day:
      * every tick group (same t) holding a FILL CANDIDATE: for some receipt touching
        pair p with eligible time e = t_recv + delay in this day, the first p tick at or
        after e; plus the first tick of every relevant pair in the day (covers an eligible
        time from an earlier day whose pair did not tick again until today);
      * at each such group, the last tick before it of every relevant pair (the marks
        the book's equity is computed from at that fill);
      * at the day's end, the last tick of every relevant pair (EOD equity, elimination,
        the weekly HWM row, and the marks the next day starts from).
    read_window(w) -> list of lines | None. Returns (ticks sorted by (t, a), missing).
    `until_ms` stops the pass early (a live refresh of today); windows after it are not
    read and not reported missing.
    Including a tick a book does not need never changes its replay (it is a real tick and
    every read still sees the latest real tick before it), so one union serves every book.
    """
    rules = rules or ab.book_rules_as_of(day_ms / 1000)
    delay = _fill_delay(rules)
    end = day_ms + ab.DAY_MS
    elig: dict = {}
    for r in receipts:
        e = int(r["t_recv_ms"]) + delay
        if day_ms <= e < end:
            for p in (r.get("targets") or {}):
                if p in pairs:
                    elig.setdefault(p, []).append(e)
    for p in elig:
        elig[p].sort()
    ptr = {p: 0 for p in elig}
    last: dict = {}            # pair -> (order, tick)
    keep: dict = {}            # order -> tick
    seen: set = set()
    missing: list = []
    order = 0
    w = day_ms
    stop = end if until_ms is None else min(end, int(until_ms))
    while w < stop:
        lines = read_window(w)
        if lines is None:
            missing.append(w)
            w += WINDOW_MS
            continue
        rows = []
        for line in lines:
            pa = _pair_of_line(line)
            if pa is not None and pa not in pairs:
                continue
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("a") not in pairs:
                continue
            t = int(d["t"])
            if not day_ms <= t < end:
                continue
            tk = {k: d[k] for k in ("a", "t", "p", "b", "k") if k in d}
            tk["t"] = t
            rows.append(tk)
        rows.sort(key=hf.tick_order_key)
        i = 0
        while i < len(rows):
            t = rows[i]["t"]
            j = i
            while j < len(rows) and rows[j]["t"] == t:
                j += 1
            group = rows[i:j]
            cand = False
            for tk in group:
                p = tk["a"]
                if p not in seen:
                    cand = True
                q = elig.get(p)
                if q is not None:
                    k = ptr[p]
                    while k < len(q) and q[k] <= t:
                        k += 1
                        cand = True
                    ptr[p] = k
            go = []
            for tk in group:
                go.append((order, tk))
                order += 1
            if cand:
                for o, tk in last.values():
                    keep[o] = tk
                for o, tk in go:
                    keep[o] = tk
            for o, tk in go:
                last[tk["a"]] = (o, tk)
                seen.add(tk["a"])
            i = j
        w += WINDOW_MS
    for o, tk in last.values():
        keep[o] = tk
    return [keep[o] for o in sorted(keep)], missing


class PairIndex:
    """Per-pair times over a (t, a)-sorted thinned tick list, for book_ticks()."""

    def __init__(self, ticks: list):
        self.ticks = ticks
        self.t: dict = {}
        self.i: dict = {}
        for n, tk in enumerate(ticks):
            self.t.setdefault(tk["a"], []).append(int(tk["t"]))
            self.i.setdefault(tk["a"], []).append(n)


def book_ticks(account_receipts: list, idx: PairIndex, universe: dict, *, start_ms: int,
               end_ms: int, rules=None) -> list:
    """The exact tick slice one book's replay reads, from the union thin."""
    rules = rules or ab.book_rules_as_of(start_ms / 1000)
    delay = _fill_delay(rules)
    pairs = sorted(p for p in touched_pairs(account_receipts, universe) if p in idx.t)
    sel: set = set()
    events = set()

    def add_at(p, tt):
        T, I = idx.t[p], idx.i[p]
        a = bisect.bisect_left(T, tt)
        b = bisect.bisect_right(T, tt)
        sel.update(I[a:b])

    def add_before(p, tt):
        T, I = idx.t[p], idx.i[p]
        a = bisect.bisect_left(T, tt)
        if a:
            add_at(p, T[a - 1])

    for r in account_receipts:
        e = int(r["t_recv_ms"]) + delay
        for p in (r.get("targets") or {}):
            if p not in idx.t:
                continue
            T = idx.t[p]
            k = bisect.bisect_left(T, e)
            if k < len(T) and T[k] <= end_ms:
                events.add(T[k])
    for F in events:
        for p in pairs:
            add_at(p, F)
            add_before(p, F)
    b = ab.day_start_ms(start_ms) + ab.DAY_MS
    while b <= end_ms:
        for p in pairs:
            add_before(p, b)
        b += ab.DAY_MS
    for p in pairs:
        add_before(p, end_ms + 1)
    return [idx.ticks[n] for n in sorted(sel) if int(idx.ticks[n]["t"]) <= end_ms]


def replay_all(receipts: list, idx: PairIndex, universe: dict, *, start_ms: int, end_ms: int,
               rules=None) -> dict:
    """{account: agent_books.replay_book result}, each over its own exact slice."""
    by_acct: dict = {}
    for r in receipts:
        by_acct.setdefault(r["account"], []).append(r)
    out = {}
    for acct in sorted(by_acct):
        rs = by_acct[acct]
        tk = book_ticks(rs, idx, universe, start_ms=start_ms, end_ms=end_ms, rules=rules)
        out[acct] = ab.replay_book(acct, rs, tk, universe, start_ms=start_ms, end_ms=end_ms,
                                   rules=rules, ticks_sorted=True)
    return out


# ── day thin cache ───────────────────────────────────────────────────────────
def thin_digest(day_receipts: list, pairs) -> str:
    """What a day's thin depends on: the receipts whose fill can be eligible in the day
    and the set of pairs any book has touched by the day's end."""
    root = ab.receipts_root(day_receipts) if day_receipts else "00" * 32
    return hashlib.sha256(hf.canonical_json({"pairs": sorted(pairs), "root": root}).encode()
                          ).hexdigest()


def thin_cache_path(cache_dir: str, day_ms: int) -> str:
    return os.path.join(cache_dir, f"{day_ms}.thin.jsonl")


def read_thin_cache(path: str, digest: str):
    try:
        with open(path, encoding="utf-8") as fh:
            head = json.loads(fh.readline())
            if head.get("digest") != digest:
                return None
            return [json.loads(l) for l in fh if l.strip()], list(head.get("missing", []))
    except (OSError, ValueError):
        return None


def write_thin_cache(path: str, digest: str, ticks: list, missing: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"digest": digest, "missing": missing}, sort_keys=True) + "\n")
        for tk in ticks:
            fh.write(hf.canonical_json(tk) + "\n")
    os.replace(tmp, path)


def load_thin_range(start_ms: int, end_ms: int, receipts: list, universe: dict,
                    read_window, cache_dir: str | None = None, final_before_ms: int = 0,
                    rules=None) -> tuple[list, list]:
    """Thin ticks for every UTC day overlapping [start_ms, end_ms). A day is cached when
    it ends at or before `final_before_ms` (its shards are final); the cache is keyed on
    thin_digest, so a late receipt or a newly touched pair recomputes the day."""
    rules = rules or ab.book_rules_as_of(start_ms / 1000)
    delay = _fill_delay(rules)
    rs = sorted(receipts, key=ab.receipt_order_key)
    times = [int(r["t_recv_ms"]) for r in rs]
    out, missing = [], []
    pairs: set = set()
    k = 0
    d = ab.day_start_ms(start_ms)
    while d < end_ms:
        end = d + ab.DAY_MS
        while k < len(rs) and int(rs[k]["t_recv_ms"]) < end:
            pairs.update(p for p in (rs[k].get("targets") or {}) if p in universe)
            k += 1
        day_rs = rs[bisect.bisect_left(times, d - delay):k]
        digest = thin_digest(day_rs, pairs)
        cacheable = bool(cache_dir) and end <= final_before_ms
        hit = read_thin_cache(thin_cache_path(cache_dir, d), digest) if cacheable else None
        if hit is None:
            ticks, miss = thin_day(d, day_rs, set(pairs), read_window, rules=rules,
                                   until_ms=end_ms if end_ms < end else None)
            if cacheable:
                write_thin_cache(thin_cache_path(cache_dir, d), digest, ticks, miss)
        else:
            ticks, miss = hit
        out.extend(ticks)
        missing.extend(miss)
        d = end
    return out, missing


# ── entity registry and bond ────────────────────────────────────────────────
def merge_registry(journal: dict, observed: dict) -> dict:
    """Sticky journal of `sn89entity:` registrations: {hk: [{"bond_coldkey", "block"}...]}.
    A new row is appended when an entity's observed bond coldkey differs from its last."""
    out = {hk: list(v) for hk, v in (journal or {}).items()}
    for hk in sorted(observed or {}):
        o = observed[hk]
        rows = out.setdefault(hk, [])
        if not rows or rows[-1]["bond_coldkey"] != o["bond_coldkey"]:
            rows.append({"bond_coldkey": o["bond_coldkey"], "block": int(o["commit_block"])})
            rows.sort(key=lambda x: x["block"])
    return dict(sorted(out.items()))


def registry_as_of(journal: dict, block: int, grandfathered: dict | None = None) -> dict:
    """{entity_hk: bond_coldkey} in force at `block`. Grandfathered entities are registered
    by config (their commitment slot is busy with LF batches); a commitment never overrides
    them."""
    out: dict = {}
    for hk, rows in (journal or {}).items():
        cur = None
        for row in sorted(rows, key=lambda x: x["block"]):
            if int(row["block"]) <= int(block):
                cur = row["bond_coldkey"]
        if cur:
            out[hk] = cur
    gf = config.AGENT_BOOKS_GRANDFATHERED if grandfathered is None else grandfathered
    out.update(gf)
    return dict(sorted(out.items()))


# ── the frozen weekly file ───────────────────────────────────────────────────
def week_label(week_start_ms: int) -> str:
    return _dt.datetime.fromtimestamp(week_start_ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")


def week_file_path(out_dir: str, week_start_ms: int) -> str:
    return os.path.join(out_dir, f"agent-books-week-{week_label(week_start_ms)}.json")


def _f(x: float) -> float:
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("non-finite figure in weekly file")
    return x


def build_week(results: dict, week_start: int, registry: dict, bonds: dict,
               week_receipts: list, rules=None, extra: dict | None = None) -> dict:
    """The FROZEN weekly file.

    results:  {account: replay_book result} replayed through at least week_start + 1 week.
    registry: {entity_hk: bond_coldkey} at the snapshot block (registry_as_of).
    bonds:    {entity_hk: alpha on netuid 89 of its bond coldkey at the snapshot block}.
    An account is paid only if its entity is registered AND bonded (agent_books.
    weekly_weights); shares sum to 1 over paid accounts, or the file has no shares and
    the pool burns."""
    rules = rules or ab.book_rules_as_of(week_start / 1000)
    books_by_entity: dict = {}
    rows: dict = {}
    for acct in sorted(results):
        ent = entity.parse(acct)[0]
        w = next((x for x in results[acct]["weeks"] if x["week_start_ms"] == week_start), None)
        gain = w["gain"] if w else 0.0
        active = bool(w["active"]) if w else False
        elim = bool(w["eliminated"]) if w else results[acct]["eliminated_at_ms"] is not None
        paid = w["paid"] if w else 0.0
        rows[acct] = {"entity": ent, "gain": _f(gain), "paid": _f(paid), "active": active,
                      "eliminated": elim, "share": 0.0}
        books_by_entity.setdefault(ent, {})[acct] = paid
    ents = sorted(set(books_by_entity) | set(registry))
    gate = {e: (float(bonds.get(e, 0.0)) if e in registry else 0.0) for e in ents}
    shares = ab.weekly_weights(books_by_entity, gate, rules)
    for a, s in shares.items():
        rows[a]["share"] = _f(s)
    entities = {e: {"registered": e in registry, "bond_coldkey": registry.get(e),
                    "bond": _f(bonds.get(e, 0.0)) if e in registry else 0.0,
                    "eligible": e in registry and ab.entity_eligible(bonds.get(e, 0.0), rules)}
                for e in ents}
    doc = {"v": 1, "week_start": int(week_start), "week_end": int(week_start + ab.WEEK_MS),
           "rules_version": int(rules["v"]), "accounts": rows, "entities": entities,
           "receipts_root": ab.receipts_root(week_receipts),
           "receipts_n": len(week_receipts)}
    if extra:
        doc.update(extra)
    return doc


def dumps_week(doc: dict) -> bytes:
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n").encode()


def build_live(results: dict, now_ms: int) -> dict:
    """Current book state per account. Shape agreed with the intake and the hosted droplet
    (2026-11-03): {"as_of_ms", "books": {account: {"positions": {PAIR: lev}, "equity",
    "balance", "hwm", "eliminated_at_ms", "week_active_buckets"}}}; extra keys are additive.
    The intake refuses new receipts for a book once eliminated_at_ms is set. Not consensus:
    nothing pays from it."""
    books = {}
    for acct in sorted(results):
        r = results[acct]
        eq = r["equity"]
        unr = r["unrealised"]
        x = r["balance"] + unr if ab.RULES_V1["hwm_basis"] == "equity" else r["balance"]
        books[acct] = {
            "positions": {p: ((v["qty"] * v["mark"] / eq) if eq else 0.0)
                          for p, v in r["positions"].items()},
            "equity": eq, "balance": r["balance"], "hwm": r["hwm"],
            "eliminated_at_ms": r["eliminated_at_ms"],
            "week_active_buckets": r["open_week_buckets"],
            # additive detail
            "unrealised": unr, "ath_eod": r["ath"],
            "elim_floor": r["ath"] * (1 - ab.RULES_V1["elim_dd"]),
            "week_gain_so_far": max(0.0, x - r["hwm"] + min(0.0, unr)),
            "declared": r["declared"],
            "position_detail": r["positions"],
            "fills": len(r["fills"]), "refused": len(r["refused"])}
    return {"v": 1, "as_of_ms": int(now_ms), "week_start": ab.week_start_ms(now_ms),
            "books": books}


# ── validator side ───────────────────────────────────────────────────────────
def pay_week_start_ms(now_unix: float) -> int:
    """The week the validator pays at `now`: the most recently ended week, from
    AGENT_BOOKS_FREEZE_DELAY_S after its close."""
    t = int(now_unix * 1000) - config.AGENT_BOOKS_FREEZE_DELAY_S * 1000
    return ab.week_start_ms(t) - ab.WEEK_MS


def epoch_ms() -> int:
    """First scored week = the week paid at AGENT_BOOKS_FROM. Every book replays from it."""
    return pay_week_start_ms(config.AGENT_BOOKS_FROM_UNIX)


def load_week_shares(out_dir: str, week_start_ms: int) -> dict | None:
    """{account: share} of a frozen week, or None if the file is absent or unreadable."""
    try:
        with open(week_file_path(out_dir, week_start_ms), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if int(doc.get("week_start", -1)) != int(week_start_ms):
        return None
    return {a: float(r["share"]) for a, r in sorted((doc.get("accounts") or {}).items())
            if float(r.get("share", 0)) > 0}


def agentbook_vector(shares: dict | None, uid_map: dict, burn_uid: int = None) -> dict | None:
    """{account: share} -> {uid: weight}. An account resolves to its own (pseudo) uid when
    the map has one (entity_split), else to its entity hotkey's uid; an entity with no UID
    burns its accounts' part. None/empty shares -> None (combine() burns the whole share)."""
    if not shares:
        return None
    burn = config.BURN_UID if burn_uid is None else burn_uid
    out: dict = {}
    for acct in sorted(shares):
        s = float(shares[acct])
        if s <= 0:
            continue
        uid = uid_map.get(acct)
        if uid is None:
            parsed = entity.parse(acct)
            uid = uid_map.get(parsed[0]) if parsed else None
        if uid is None:
            uid = burn
        out[uid] = out.get(uid, 0.0) + s
    return out or None


def cycle_shares(now_unix: float, out_dir: str | None = None) -> dict | None:
    """What the validator pays this cycle, or None when the flag is off or the frozen file
    for the paid week is missing (then the share burns)."""
    if not config.agent_books_active(now_unix):
        return None
    return load_week_shares(out_dir or config.AGENT_BOOKS_DIR, pay_week_start_ms(now_unix))
