#!/usr/bin/env python3
"""Build the HF (mechanism-1) scoreboard JSON for the website leaderboard.

Same row shape as the LF scoreboard, from HF sources:
  * accepted calls  — the ingest window logs (/var/lib/sn89-hf/*.jsonl): every
    accepted HF submission from ANY path (self-hosted + hosted/multiplexer).
  * outcomes        — the validator HF grade cache (won/lost/wash).
  * pool share      — the live mecid-1 weight vector (all-burn until anyone qualifies).
  * identity        — hotkey -> signals user via tenants.json (hosted) and
    signals_users.sn89_hotkey (self-hosted); name from signals_users.

A trader appears the moment they submit (pending rows), and their counts fill in
as calls resolve — no waiting for qualification to show up.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, hf, hf_grade, scoring     # noqa: E402

# The cadence strip + the network block (correlation / crowding / lean / pulse)
# are the SAME functions the LF board runs, imported rather than reimplemented —
# a second copy of that arithmetic is how two tabs end up describing the field
# differently. build_dashboard lives in the IQ-platform tree, not in this
# package; this tool is already IQ-platform-coupled (it reads data/live and
# writes the website snapshot), but the import is optional so a checkout without
# that tree still produces a board, just without the network section.
sys.path.insert(0, os.getenv("SN89_STANDING_DIR",
                             "/opt/iq-platform/services/sn89-standing"))
try:
    import build_dashboard as _bd          # noqa: E402
except Exception:                          # noqa: BLE001
    _bd = None

NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
REG_CACHE = Path(os.getenv("SN89_HF_REG_CACHE",
                           "/var/lib/sn89-hf/registered-cache.json"))
POOL_PATH = os.getenv("SN89_POOL_PATH", "/opt/iq-platform/data/live/sn89-pool.json")
TAO_USD_CACHE = os.getenv("SN89_TAO_USD_CACHE", "/opt/iq-platform/data/live/tao-usd.json")
TAO_USD_FALLBACK = float(os.getenv("SN89_TAO_USD_FALLBACK", "210.0"))
HF_EMIS_LEDGER = os.getenv("SN89_HF_EMISSIONS_LEDGER",
                           "/opt/iq-platform/data/live/sn89-hf-emissions-ledger.json")


def _hf_pool_tao_day() -> float:
    """mecid-1's share of the daily miner pool — what HF miners split. Written by
    registered_cache from the live MechanismEmissionSplit. 0.0 if absent."""
    try:
        return float(json.loads(Path(POOL_PATH).read_text()).get("hf_pool_tao_day") or 0.0)
    except Exception:
        return 0.0


def _tao_usd() -> float:
    """Last-good TAO/USD from the shared cache the LF publisher maintains. No
    network here — the scoreboard runs on a tight timer; a pinned fallback is fine."""
    try:
        return float(json.loads(Path(TAO_USD_CACHE).read_text())["usd"])
    except Exception:
        return TAO_USD_FALLBACK


def _accrue_hf_emissions(weight_by_hk: dict, hf_pool_tao_day: float, now: float) -> dict:
    """Cumulative per-hotkey HF (mecid-1) emission in TAO, integrated as
    weight × hf_pool_tao_day over wall-clock elapsed between runs — the exact
    mirror of the LF publisher's _accrue_emissions, on its own ledger so the two
    mechanisms never cross-contaminate. `weight_by_hk` is each miner's share of
    the mecid-1 vector (0..1, qualified miners only; burn is not a trader).

    A hotkey seen for the first time starts at cum=0 (integrate from now): unlike
    LF there is no pre-tracker HF emission to backfill — HF weight is zero until a
    miner clears the eligibility gate, and this ledger is created before any HF
    miner earns. Never raises (degrades to the prior/zero value)."""
    try:
        ledger = json.loads(Path(HF_EMIS_LEDGER).read_text())
        if not isinstance(ledger, dict):
            ledger = {}
    except Exception:
        ledger = {}
    out: dict = {}
    for hk, w in (weight_by_hk or {}).items():
        rate = float(w or 0.0) * float(hf_pool_tao_day or 0.0)      # TAO/day now
        e = ledger.get(hk)
        if isinstance(e, dict):
            elapsed = max(0.0, (now - float(e.get("last_ts") or now)) / 86400.0)
            cum = float(e.get("cum_tao") or 0.0) + rate * elapsed
        else:
            cum = 0.0
        ledger[hk] = {"cum_tao": cum, "last_ts": now}
        out[hk] = cum
    try:
        tmp = HF_EMIS_LEDGER + ".tmp"
        Path(tmp).write_text(json.dumps(ledger))
        os.replace(tmp, HF_EMIS_LEDGER)
        try:
            os.chmod(HF_EMIS_LEDGER, 0o644)
        except OSError:
            pass
    except Exception:
        pass
    return out


def _registered_hotkeys() -> set:
    """Hotkeys holding a UID on the subnet.

    The public board is a claim about who is competing, so it must only show
    hotkeys that CAN compete — mecid-1 weight is keyed by UID, so an unregistered
    hotkey can never earn. Until 2026-07-24 ingest accepted any keypair, and four
    of the six "traders" shown here were unregistered bring-up probes of our own.
    Ingest now refuses them, but this filter is the second line: the window logs
    are Merkle-anchored and immutable, so those four are in the history for good
    and must be filtered at display rather than erased.

    Cached to disk, and on failure the LAST KNOWN set is used. If there is no set
    at all, `main` aborts WITHOUT writing rather than publishing an unfiltered
    board — the stale snapshot is the safer thing to leave on the website.
    """
    try:
        import bittensor as bt
        hks = set(bt.Subtensor(NETWORK).metagraph(NETUID).hotkeys)
        if hks:
            REG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(REG_CACHE) + ".tmp"
            Path(tmp).write_text(json.dumps(sorted(hks)))
            os.replace(tmp, REG_CACHE)
            return hks
        print("!! metagraph returned an EMPTY hotkey set — falling back to cache")
    except Exception as e:      # noqa: BLE001
        print(f"!! metagraph read failed ({e}) — falling back to cache")
    try:
        return set(json.loads(REG_CACHE.read_text()))
    except Exception:      # noqa: BLE001
        return set()


# Tier classification — identical to the LF path (classify_signals_tiers.py) and
# the validator scoring, so an HF tier means the same thing as a main-program one.
def _wilson_lb(won: int, n: int) -> float:
    if n <= 0:
        return 0.0
    z = config.QUALIFY_Z
    p = won / n
    z2 = z * z
    center = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) + z2 / (4 * n)) / n) ** 0.5
    return max(0.0, (center - margin) / (1 + z2 / n))


def _shrunk(won: int, n: int) -> float:
    k = config.TIER_PRIOR_K
    return (won + k / 2) / (n + k) if n > 0 else 0.0


def _classify(won: int, dec: int):
    """None (not qualified) | 'QUALIFIED' | 'SHARP' | 'WOLF'."""
    if dec < config.QUALIFY_MIN_DECISIVE or _wilson_lb(won, dec) < config.QUALIFY_LB_FLOOR:
        return None
    s = _shrunk(won, dec)
    if s >= 0.70:
        return "WOLF"
    if s >= 0.60:
        return "SHARP"
    if s >= 0.55:
        return "QUALIFIED"
    return None

LOG_DIR = os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf")
GRADE_DBS = [os.path.expanduser("~/.sn89/hf-grade/hf_grades.db"),
             "/root/.sn89/hf-grade/hf_grades.db",
             os.path.join(LOG_DIR, "hf_grades.db")]
# Where hf_grade.py caches the published tick windows it grades against. The path
# metrics below are walked from the SAME series the grade came from, so MFE/MAE
# and the W/L can never describe different prices.
TICK_DIRS = [os.path.expanduser("~/.sn89/hf-grade/ticks"),
             "/root/.sn89/hf-grade/ticks",
             os.path.join(LOG_DIR, "ticks")]
# Derived, incremental, and safe to delete: per-call entry / excursion / outcome.
PATH_DB = os.getenv("SN89_HF_PATH_DB", os.path.join(LOG_DIR, "hf_paths.db"))
WINDOW_MS = hf_grade.WINDOW_MS            # hf_grade's publication window
ADMIN_DB = os.getenv("IQ_SIGNALS_DB", "/opt/iq-platform/data/live/iq_admin_dash.db")
TENANTS = os.getenv("SN89_MANAGED_TENANTS_PATH",
                    "/opt/iq-platform/data/live/sn89-managed-main/tenants.json")
OUT = os.getenv("IQ_HF_SCOREBOARD",
                "/opt/iq-platform/data/live/sn89-hf-scoreboard.json")
# world-readable grade snapshot for the webhook's private per-call HF view (the
# validator's own cache is under /root, unreadable by the iq service user)
GRADE_PUBLISH = "/var/lib/sn89-hf/hf_grades.db"


def _accepted_calls() -> dict:
    """(hk, seq) -> {hk, pair, grid_t0_ms} from the accepted-window logs.

    HF submissions ONLY. The ingest writes every competition into the same window
    logs, so a Closers vote (`payload.kind == "closers"`) lands here too — and
    since it is graded by closers.py and never enters the HF grade cache, it read
    as an HF call `pending` FOREVER. Measured 2026-08-06: 207 of the board's 209
    pending rows were Closers votes, every one of them already graded on the
    Closers board. `hf_grade.sync_and_grade` has skipped them from the start; this
    is the same filter, and the two must not drift.
    """
    calls = {}
    for f in glob.glob(os.path.join(LOG_DIR, "*.jsonl")):
        if not Path(f).stem.isdigit():
            continue
        try:
            for line in open(f, encoding="utf-8"):
                if not line.strip():
                    continue
                e = json.loads(line)
                sub, rcpt = e.get("submit") or {}, e.get("receipt") or {}
                hk, seq = sub.get("hk"), sub.get("seq")
                if not hk or seq is None:
                    continue
                p = sub.get("payload") or {}
                if str(p.get("kind", "")) == "closers":
                    continue        # graded by closers.py — not an HF call
                calls[(hk, seq)] = {
                    "hk": hk, "pair": p.get("trade_pair"),
                    "direction": p.get("direction"),
                    "asset_class": p.get("asset_class"),
                    "grid_t0_ms": rcpt.get("grid_t0_ms")}
        except (OSError, ValueError):
            continue
    return calls


REJECT_DIR = os.getenv("SN89_HF_REJECT_DIR", os.path.join(LOG_DIR, "rejects"))


def _refusals() -> dict:
    """hk -> [refused submissions], newest first, from the ingest reject log.

    Kept OUT of `leaderboard` rows on purpose. A refusal is not a call: it has no
    grid t0, no grade and no path, it must never touch a hit rate or the pace
    strip, and a hotkey whose every submission was refused must not appear on the
    public board as a trader. It rides as a separate top-level map so the
    per-miner page can answer "where did my call go" for ANY hotkey, including one
    with no board row at all — which is precisely the new miner most likely to be
    getting refused.
    """
    out: dict = {}
    for f in glob.glob(os.path.join(REJECT_DIR, "*.jsonl")):
        if not Path(f).stem.isdigit():
            continue
        try:
            for line in open(f, encoding="utf-8"):
                if not line.strip():
                    continue
                r = json.loads(line)
                hk = r.get("hk")
                if not hk:
                    continue
                p = r.get("payload") or {}
                out.setdefault(hk, []).append({
                    "seq": r.get("seq"),
                    "t_unix": (r.get("t_recv_us") or 0) / 1e6,
                    "comp": r.get("comp") or "hf",     # "hf" | "closers"
                    "reason": r.get("reason"),
                    "trade_pair": p.get("trade_pair"),
                    "direction": p.get("direction"),
                    "asset_class": p.get("asset_class"),
                })
        except (OSError, ValueError):
            continue
    for hk in out:
        out[hk] = sorted(out[hk], key=lambda r: r["t_unix"],
                         reverse=True)[:HF_RECENT_CALLS_CAP]
    return out


def _grades():
    """Returns (status_by_key, rows, held_by_key) from the first readable grade
    cache: status_by_key = {'hk:seq': status}; rows = [(hk, t0_ms, status), ...];
    held_by_key = {'hk:seq': open_until_ms} — the resolving touch for a decisive
    call, the horizon for a wash, which is where the path walk stops."""
    for db in GRADE_DBS:
        if not os.path.exists(db):
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            by_key = {k: s for k, s in c.execute("SELECT key, status FROM grades")}
            rows = list(c.execute("SELECT hk, t0_ms, status FROM grades"))
            held = {k: v for k, v in c.execute(
                "SELECT key, open_until_ms FROM grades WHERE open_until_ms IS NOT NULL")}
            c.close()
            _publish_grade_db(db)
            return by_key, rows, held
        except sqlite3.Error:
            continue
    return {}, [], {}


def _publish_grade_db(src: str) -> None:
    """Republish the grade cache to a world-readable path so the webhook's
    private per-call HF view can exclude already-graded calls. Best-effort."""
    if os.path.abspath(src) == os.path.abspath(GRADE_PUBLISH):
        return
    try:
        c = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
        dst = sqlite3.connect(GRADE_PUBLISH, timeout=5)
        c.backup(dst)
        dst.close()
        c.close()
        os.chmod(GRADE_PUBLISH, 0o644)
    except (sqlite3.Error, OSError) as e:
        print(f"grade-db publish skipped: {e}", file=sys.stderr)


def _identity():
    """hotkey -> (signals_user_id, display_name)."""
    hk2uid: dict = {}
    try:
        for v in json.load(open(TENANTS)).values():
            if v.get("hotkey_ss58") and v.get("signals_user_id"):
                hk2uid[v["hotkey_ss58"]] = int(v["signals_user_id"])
    except Exception:      # noqa: BLE001
        pass
    names: dict = {}
    try:
        con = sqlite3.connect(f"file:{ADMIN_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT id, telegram_username, first_name, sn89_hotkey "
                             "FROM signals_users"):
            nm = (r["telegram_username"] and f"@{r['telegram_username']}") \
                or r["first_name"] or f"user {r['id']}"
            names[int(r["id"])] = nm
            if r["sn89_hotkey"]:
                hk2uid.setdefault(r["sn89_hotkey"], int(r["id"]))
        con.close()
    except sqlite3.Error:
        pass
    return hk2uid, names


def _submissions_by_hk(grades_rows) -> dict:
    """hotkey -> [t0_ms of every resolved submission] (won/lost/wash/void), the
    accepted-submission set the HF eligibility gate counts. Resolved-only, so it
    matches the validator's own source exactly (hf_grade._history)."""
    subs: dict = {}
    for hk, t0_ms, _status in grades_rows:
        subs.setdefault(hk, []).append(int(t0_ms))
    return subs


def _outcome_history(grades_rows):
    """(decisive_by_hk, graded_by_hk, first_seen_by_hk) from the grade cache, in
    the shapes scoring.* expects: decisive = [(t0_unix, won, is_copy)] (HF runs no
    copy detection, so is_copy is always False); graded = [(t0_unix, is_wash)] over
    every resolved outcome, which is what prices washes."""
    dec: dict = {}
    graded: dict = {}
    fs: dict = {}
    for hk, t0_ms, status in grades_rows:
        t0 = t0_ms / 1000.0
        fs[hk] = min(fs.get(hk, t0), t0)
        if status in ("won", "lost", "wash"):
            graded.setdefault(hk, []).append((t0, status == "wash"))
        if status in ("won", "lost"):
            dec.setdefault(hk, []).append((t0, status == "won", False))
    for v in dec.values():
        v.sort(key=lambda x: x[0])
    for v in graded.values():
        v.sort()
    return dec, graded, fs


def _mecid1_weights(decisive_by_hk, first_seen_by_hk, subs_by_hk, now):
    """hotkey -> pool share, from the read-only grade history via the SAME
    hf_compute_weights the validator commits. No chain call, no cache mutation —
    the scoreboard only reads; the validator remains the sole writer of on-chain
    weights. Shares normalise, so a synthetic uid map gives each hotkey its share."""
    # Synthetic uids MUST NOT start at 0. scoring.compute_weights parks the burn
    # residual on config.BURN_UID (0), so whichever hotkey lands in slot 0
    # silently absorbs it — and the map is built from sorted(), so slot 0 is just
    # "alphabetically first hotkey". Live on 2026-07-31: 5C5hiP… (@MatPod) was
    # published at 66.7% of the HF pool, ranked #1, and accruing emissions, while
    # being absent from the on-chain mecid-1 vector entirely. The `if qualified`
    # gate on the row had been masking it; removing that gate (correctly, for the
    # no-cliff design, 30f65d2) unmasked it. Offset by 1 so BURN_UID stays empty
    # and the hk_by_uid filter below drops the burn as it was always meant to.
    hks = sorted(first_seen_by_hk)
    uid_by_hk = {hk: i + 1 for i, hk in enumerate(hks)}
    try:
        w = hf.hf_compute_weights(decisive_by_hk, first_seen_by_hk, uid_by_hk,
                                  now, subs_by_hk)
    except Exception as e:      # noqa: BLE001
        print(f"weights unavailable: {e}", file=sys.stderr)
        return {}
    hk_by_uid = {i: hk for hk, i in uid_by_hk.items()}
    return {hk_by_uid[u]: x for u, x in w.items() if u in hk_by_uid}


# ── per-call path metrics (MFE / MAE / realized bps) ─────────────────────────
# The LF board gets these from signals_submissions, where the grader writes
# mfe_bps/mae_bps at grade time. The HF grade cache stores only the verdict, so
# they are walked here from the same published tick windows the grade came from.
# Derived and cached incrementally: a call is walked once, ever.

def _tick_dir() -> str | None:
    for d in TICK_DIRS:
        if os.path.isdir(d):
            return d
    return None


def _path_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PATH_DB) or ".", exist_ok=True)
    c = sqlite3.connect(PATH_DB, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS paths ("
              "key TEXT PRIMARY KEY, entry REAL, mfe_bps REAL, mae_bps REAL, "
              "out_bps REAL)")
    return c


def _compute_paths(calls: dict, grades: dict, held_by_key: dict) -> dict:
    """key -> {entry, mfe_bps, mae_bps, out_bps} for every RESOLVED HF call.

    One streaming pass over the tick windows the outstanding calls span, so a
    cold build walks the corpus once and every later run walks only the windows
    covering calls graded since. Signs follow the LF convention the table renders:
    mfe_bps >= 0 (furthest in the call's favour), mae_bps <= 0 (furthest against),
    out_bps signed by direction at the resolving tick.

    Entry is the last tick AT OR BEFORE t0 — hf.price_at's rule, not the first
    tick after. Getting that backwards is what voided every crypto call once
    already (see hf_grade._ticks_for): a crypto tick almost never lands exactly on
    the grid point.
    """
    tick_dir = _tick_dir()
    if not tick_dir:
        return {}
    db = _path_db()
    have = {r[0] for r in db.execute("SELECT key FROM paths")}
    todo = []
    for (hk, seq), c in calls.items():
        key = f"{hk}:{seq}"
        if key in have or grades.get(key) not in ("won", "lost", "wash"):
            continue
        t0, end = c.get("grid_t0_ms"), held_by_key.get(key)
        if not t0 or not end or not c.get("pair") or not c.get("direction"):
            continue
        todo.append({"key": key, "pair": c["pair"], "up": 1 if c["direction"] == "LONG" else -1,
                     "t0": int(t0), "end": int(end)})
    if todo:
        todo.sort(key=lambda x: x["t0"])
        w = (min(t["t0"] for t in todo) // WINDOW_MS - 1) * WINDOW_MS
        w_last = (max(t["end"] for t in todo) // WINDOW_MS) * WINDOW_MS
        # Everything is bucketed BY PAIR. The entry anchor is the last tick at or
        # before t0 *on that pair*, so a call must be activated on its own pair's
        # first post-t0 tick — activating on any tick would anchor to a price the
        # pair had already moved past.
        pending_by_pair: dict = {}
        for c in todo:
            pending_by_pair.setdefault(c["pair"], []).append(c)
        prev_px: dict = {}          # pair -> last price seen STRICTLY before now
        live_by_pair: dict = {}
        done: dict = {}
        while w <= w_last:
            try:
                with open(os.path.join(tick_dir, f"{w}.ticks.jsonl"), encoding="utf-8") as fh:
                    ticks = [json.loads(ln) for ln in fh if ln.strip()]
            except (OSError, ValueError):
                ticks = []          # window evicted or truncated — those calls
                                    # simply record no path, never a wrong one
            ticks.sort(key=lambda d: int(d["t"]))
            for d in ticks:
                try:
                    t, pair, px = int(d["t"]), d["a"], float(d["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                pend = pending_by_pair.get(pair)
                while pend and pend[0]["t0"] < t:
                    c = pend.pop(0)
                    e = prev_px.get(pair)
                    if not e:
                        continue    # no pre-t0 price in hand for that pair
                    c.update(entry=e, best=e, worst=e, exit_px=e)
                    live_by_pair.setdefault(pair, []).append(c)
                lives = live_by_pair.get(pair)
                if lives:
                    still = []
                    for c in lives:
                        if t > c["end"]:
                            done[c["key"]] = c
                            continue
                        c["best"] = max(c["best"], px)
                        c["worst"] = min(c["worst"], px)
                        c["exit_px"] = px
                        still.append(c)
                    live_by_pair[pair] = still
                prev_px[pair] = px
            w += WINDOW_MS
        for lives in live_by_pair.values():
            for c in lives:
                done[c["key"]] = c
        rows = []
        for key, c in done.items():
            e, up = c["entry"], c["up"]
            fav, adv = (c["best"], c["worst"]) if up > 0 else (c["worst"], c["best"])
            rows.append((key, e,
                         round(up * (fav - e) / e * 10000.0, 2),
                         round(up * (adv - e) / e * 10000.0, 2),
                         round(up * (c["exit_px"] - e) / e * 10000.0, 2)))
        if rows:
            db.executemany("INSERT OR REPLACE INTO paths VALUES (?,?,?,?,?)", rows)
            db.commit()
            print(f"   walked {len(rows)} new HF call path(s)")
    out = {r[0]: {"entry": r[1], "mfe_bps": r[2], "mae_bps": r[3], "out_bps": r[4]}
           for r in db.execute("SELECT key, entry, mfe_bps, mae_bps, out_bps FROM paths")}
    db.close()
    try:
        os.chmod(PATH_DB, 0o644)
    except OSError:
        pass
    return out


# ── per-win VALUE and the win ledger ─────────────────────────────────────────
RECENT_WINS_CAP = 8


def _value_by_hk(decisive_by_hk: dict, graded_by_hk: dict,
                 eligible_from: dict, now: float) -> dict:
    """hotkey -> the Value× column and its two factors, plus each recent win with
    the × it was STAMPED at.

    Runs inside hf.hf_scoring_config(), so tier ladder, wash efficiency and the
    per-win stamp are computed against the HF constants the mecid-1 vector was
    actually built from. Computing them against the LF constants would put a
    different × on the HF tab than the one that pays.

    A win banks the × the miner held at that moment and keeps paying it until it
    decays out of the emission window — which is why a miner below the gate can
    still be earning. scoring.qualified_wins is the SAME function compute_weights
    runs, so these are the exact weights in the tally; a win earned while
    unqualified stamps None and renders "—".
    """
    out: dict = {}
    with hf.hf_scoring_config():
        for hk, decisive in decisive_by_hk.items():
            eligible = eligible_from.get(hk)
            graded = graded_by_hk.get(hk) or []
            eff = round(scoring.efficiency_multiplier(graded, now), 3)
            wins = [(t0, won) for t0, won, _c in decisive if won]
            if eligible is None:
                # Not past the volume gate: no tier and no stamps, so every win in
                # the ledger reads "—". The ledger is still shown, because "these
                # are your wins and none of them banked anything yet" is the point
                # of the drawer for a miner who has not cleared the gate.
                out[hk] = {"tier_multiplier": None, "efficiency": eff,
                           "value_multiplier": None, "raw_hit_pct": None,
                           "rep_wins": 0, "rep_decisive": 0,
                           "wins": [{"t0_unix": t0, "mult": None}
                                    for t0, _ in sorted(wins, reverse=True)[:RECENT_WINS_CAP]]}
                continue
            rep_w, rep_d, _wa, _wo, _cp, _tw = scoring.score_inputs(decisive, eligible, now)
            qual = scoring.is_qualified(rep_w, rep_d)
            tier = round(max(1.0, scoring.tier_multiplier(rep_w, rep_d, now)), 3) if qual else None
            try:
                stamped = dict(scoring.qualified_wins(
                    sorted(decisive), eligible, habitual=False, graded=graded))
            except Exception:      # noqa: BLE001
                stamped = {}
            wins.sort(reverse=True)
            out[hk] = {
                "tier_multiplier": tier,
                "efficiency": eff,
                "value_multiplier": round(tier * eff, 3) if tier is not None else None,
                "raw_hit_pct": round(100.0 * rep_w / rep_d, 1) if rep_d else None,
                "rep_wins": rep_w, "rep_decisive": rep_d,
                "wins": [{"t0_unix": t0, "mult": stamped.get(t0)}
                         for t0, _ in wins[:RECENT_WINS_CAP]],
            }
    return out


def _ago(days: float) -> str:
    """Human age, matching the LF ledger's wording."""
    if days < 1 / 24:
        return f"{max(1, int(days * 1440))}m ago"
    if days < 1:
        return f"{int(days * 24)}h ago"
    return f"{int(days)}d ago"


# How many of a hotkey's own HF calls the board carries. This is the ONLY
# per-miner HF history that exists anywhere: the grade cache holds a verdict with
# no direction, the published receipts hold a direction with no verdict, and the
# private /me/hf-calls endpoint shows in-flight calls only — so a 30-minute call
# is invisible minutes after it fires. Cap matches the LF board's RECENT_CALLS_CAP
# in spirit; HF runs ~10x the cadence, so it is larger.
HF_RECENT_CALLS_CAP = int(os.getenv("SN89_HF_RECENT_CALLS_CAP", "60"))


def _recent_calls(entries: list) -> list:
    """A hotkey's own newest HF calls, every status, newest first.

    Shaped like the LF `calls` rows the miner page already renders (t0_unix /
    trade_pair / direction / status / outcome_bps), plus the HF-only fields a
    trader needs to read the result: the band it was graded against and how far
    the path ran either way.
    """
    return sorted(entries, key=lambda e: e["t0_unix"] or 0, reverse=True)[:HF_RECENT_CALLS_CAP]


def _win_ledger(v: dict | None, calls_by_t0: dict, paths: dict, now: float) -> list:
    """The drawer's 'wins & their live value' — each recent win, the × it banked,
    and how much of that value is left after HF's linear emission decay."""
    if not v:
        return []
    decay_days = hf.HF_EMISSION_DECAY_S / 86400.0
    out = []
    for w in v["wins"]:
        t0 = w["t0_unix"]
        age_d = max(0.0, (now - t0) / 86400.0)
        c = calls_by_t0.get(t0) or {}
        p = paths.get(c.get("key") or "") or {}
        out.append({
            "when": _ago(age_d),
            "pair": c.get("pair") or "—",
            "dir": c.get("direction") or "",
            "bps": round(p["out_bps"]) if p.get("out_bps") is not None else 0,
            "mult": round(float(w["mult"]), 2) if w["mult"] is not None else None,
            "decay": round(max(0.0, min(1.0, 1.0 - age_d / decay_days)), 2),
        })
    return out


# ── cadence + network (the SAME functions the LF board runs) ─────────────────
# HF is denser than LF (30/day vs 3), so the strip is capped higher; the window
# stays 30 days so both tabs' strips share one axis and read the same way.
HF_PACE_CAP = int(os.getenv("SN89_HF_PACE_CAP", "400"))
HF_PACE_WINDOW_S = float(os.getenv("SN89_HF_PACE_WINDOW_DAYS", "30")) * 86400
HF_NET_WINDOW_S = float(os.getenv("SN89_HF_NETWORK_WINDOW_DAYS", "30")) * 86400
# HF has weeks of history where LF has months, so the pairwise-overlap floor is
# lower. It is still a floor, and the permutation null in _network_block is what
# keeps a short overlap from reading as a correlated cluster.
HF_NET_MIN_DAYS = int(os.getenv("SN89_HF_NETWORK_MIN_OVERLAP_DAYS", "3"))
# A minute, not LF's 30: at 30 calls/day and horizons in minutes, half an hour is
# most of a session — everything would read as crowded and the number would say
# nothing.
HF_NET_CO_WINDOW_S = float(os.getenv("SN89_HF_NETWORK_CO_WINDOW_MIN", "1")) * 60
HF_NET_TAIL_RHO = float(os.getenv("SN89_HF_NETWORK_TAIL_RHO", "0.6"))


def _signals_view(calls: dict, grades: dict, held: dict, paths: dict, registered: set):
    """An in-memory table shaped like the validator journal's `signals`, so
    build_dashboard's _pace_by_hk and _network_block run on HF data unchanged.
    Reimplementing that arithmetic is how two tabs end up disagreeing about the
    same field."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE signals (hotkey TEXT, t0_unix REAL, status TEXT, "
                "outcome_bps REAL, plaintext TEXT, exit_at_ms INTEGER)")
    rows = []
    for (hk, seq), c in calls.items():
        if hk not in registered or not c.get("grid_t0_ms"):
            continue
        key = f"{hk}:{seq}"
        st = grades.get(key)
        if st == "wash":
            st = "washed"            # the journal's spelling, which DECISIVE and
                                     # the tick legend both key off
        elif st is None:
            st = "pending"
        elif st not in ("won", "lost"):
            continue                 # void: a real submission, but no outcome
        rows.append((hk, int(c["grid_t0_ms"]) / 1000.0, st,
                     (paths.get(key) or {}).get("out_bps"),
                     json.dumps({"trade_pair": c.get("pair"),
                                 "direction": c.get("direction")}),
                     held.get(key)))
    con.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return con


def _pace_and_network(con, now: float, max_per_day: int | None):
    """(pace_by_hk, network_block) — or ({}, None) when build_dashboard isn't
    importable, in which case the board simply renders without those sections."""
    if _bd is None:
        return {}, None
    try:
        pace = _bd._pace_by_hk(con, now, HF_PACE_WINDOW_S, HF_PACE_CAP)
    except Exception as e:      # noqa: BLE001
        print(f"pace unavailable: {e}", file=sys.stderr)
        pace = {}
    try:
        net = _bd._network_block(con, now, HF_NET_WINDOW_S, HF_NET_MIN_DAYS,
                                 HF_NET_CO_WINDOW_S, HF_NET_TAIL_RHO)
        net["computed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        net["max_per_utc_day"] = max_per_day
    except Exception as e:      # noqa: BLE001
        print(f"network block unavailable: {e}", file=sys.stderr)
        net = None
    return pace, net


def _n_submitters_last_days(days: int = 3) -> int:
    """Distinct users who submitted an HF call in the last `days` days.

    Not the same as n_miners_active (non-zero emission_weight): a trader can be
    submitting steadily and still earn nothing while below the gate. On
    2026-07-28 that was 11 submitting vs 0 earning, so the earning count alone
    made HF look dead when it was not.
    """
    try:
        con = sqlite3.connect(f"file:{ADMIN_DB}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT COUNT(DISTINCT signals_user_id) FROM hf_submissions "
                "WHERE submitted_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)",
                (f"-{int(days)} days",),
            )
            return int((cur.fetchone() or [0])[0] or 0)
        finally:
            con.close()
    except Exception:
        return 0


def main() -> int:
    now = time.time()
    calls = _accepted_calls()
    grades, grade_rows, held_by_key = _grades()
    hk2uid, names = _identity()
    # HF eligibility (>=50 accepted submissions across >=8 distinct UTC trading
    # days) — the same gate the validator applies; the qualified badge must not
    # claim a miner is qualified before it can actually earn.
    subs_by_hk = _submissions_by_hk(grade_rows)
    eligible_from = {hk: hf.hf_eligible_from(ts) for hk, ts in subs_by_hk.items()}
    eligible_by_hk = {hk: v is not None for hk, v in eligible_from.items()}
    # ...and the PROGRESS toward it, not just the verdict. This gate is the one
    # thing a trader cannot infer from anything else on the board: the Wilson floor
    # is visible in the hit rate, but "8 distinct UTC trading days" is invisible, so
    # a miner with 154 submissions and a good hit rate reads as though it should be
    # earning and is in fact 2 days short. Derived HERE, from the same
    # subs_by_hk the gate itself consumes, so the number on the page can never
    # disagree with the number the validator applied (trap-1 discipline).
    gate_by_hk = {}
    for hk, ts in subs_by_hk.items():
        days = sorted({int(t) // 86_400_000 for t in ts})
        gate_by_hk[hk] = {
            "submissions": len(ts),
            "submissions_required": hf.HF_QUALIFY_MIN_SUBMISSIONS,
            "trading_days": len(days),
            "trading_days_required": hf.HF_QUALIFY_MIN_TRADING_DAYS,
            "trading_days_remaining": max(0, hf.HF_QUALIFY_MIN_TRADING_DAYS - len(days)),
            "submissions_remaining": max(0, hf.HF_QUALIFY_MIN_SUBMISSIONS - len(ts)),
            "last_trading_day": (dt.datetime.fromtimestamp(days[-1] * 86_400, dt.timezone.utc)
                                 .strftime("%Y-%m-%d") if days else None),
        }
    decisive_by_hk, graded_by_hk, first_seen_by_hk = _outcome_history(grade_rows)
    # hotkey -> pool share (empty/zero until anyone qualifies)
    weights = _mecid1_weights(decisive_by_hk, first_seen_by_hk, subs_by_hk, now)
    # Value×, its two factors, and the per-win stamps behind the ledger drawer.
    value_by_hk = _value_by_hk(decisive_by_hk, graded_by_hk, eligible_from, now)

    registered = _registered_hotkeys()
    if not registered:
        print("!! no registered-hotkey set (chain unreachable, no cache) — "
              "REFUSING to write an unfiltered board; leaving the last snapshot")
        return 1
    skipped = {hk for (hk, _seq) in calls if hk not in registered}
    if skipped:
        # Never silent: these are real, signed, anchored submissions being held
        # off the public board, and that decision has to be auditable.
        print(f"   filtering {len(skipped)} unregistered hotkey(s) off the board: "
              + ", ".join(sorted(h[:10] + '…' for h in skipped)))

    # entry / MFE / MAE / realized bps per call, walked from the same published
    # tick windows the grade came from (incremental — each call once, ever)
    paths = _compute_paths(calls, grades, held_by_key)

    # aggregate per hotkey, and per (hotkey, pair) for the drawer breakdown
    agg: dict = {}
    assets_by: dict = {}      # hk -> {pair -> AssetRow-shaped dict}
    excur: dict = {}          # hk -> {"r": [...], "mfe": [...], "mae": [...]}
    calls_by_t0: dict = {}    # hk -> {t0_unix -> call}, for the win ledger
    recent_by_hk: dict = {}   # hk -> [call rows], for the miner page's HF history
    for (hk, seq), c in calls.items():
        if hk not in registered:
            continue
        key = f"{hk}:{seq}"
        a = agg.setdefault(hk, {"won": 0, "lost": 0, "wash": 0, "pending": 0})
        pair = c["pair"] or "?"
        if c.get("grid_t0_ms"):
            calls_by_t0.setdefault(hk, {})[int(c["grid_t0_ms"]) / 1000.0] = dict(c, key=key)
        pa = assets_by.setdefault(hk, {}).setdefault(pair, {
            "asset": pair, "asset_class": c.get("asset_class"),
            "n": 0, "longs": 0, "shorts": 0, "won": 0, "lost": 0, "washed": 0})
        pa["n"] += 1
        if c.get("direction") == "SHORT":
            pa["shorts"] += 1
        else:
            pa["longs"] += 1
        st = grades.get(key)
        # Path metrics run on the DECISIVE calls only, the same population Hit %
        # and Conf % read — the LF board had these two column groups describing
        # two different sets of calls once, and a trader had no way to tell.
        if st in ("won", "lost"):
            ex = excur.setdefault(hk, {"r": [], "mfe": [], "mae": []})
            board = hf.hf_bands_as_of(int(c["grid_t0_ms"]) / 1000.0) if c.get("grid_t0_ms") else None
            band = (board or {}).get(pair)
            if band:
                tp_bps, sl_bps = float(band[0]), float(band[1])
                # every call's stop is 1R, so a win pays tp/sl R and a loss -1R
                ex["r"].append(tp_bps / sl_bps if st == "won" and sl_bps else -1.0)
            p = paths.get(key)
            if p and p.get("mfe_bps") is not None:
                ex["mfe"].append(p["mfe_bps"])
                ex["mae"].append(p["mae_bps"])
        if st == "won":
            a["won"] += 1
            pa["won"] += 1
        elif st == "lost":
            a["lost"] += 1
            pa["lost"] += 1
        elif st == "wash":
            a["wash"] += 1
            pa["washed"] += 1
        elif st is None:
            a["pending"] += 1             # not yet in the grade cache
        # else: a resolved 'void' (no valid price at the grid point) — a real
        # submission (counted in n/longs/shorts) but neither decisive, wash, nor
        # pending. Falls through so it never inflates the pending count.

        # the miner's own call history. Built for EVERY status including void and
        # pending: a trader chasing "where did my call go" is asking about exactly
        # the rows an outcome-only view drops.
        t0_unix = int(c["grid_t0_ms"]) / 1000.0 if c.get("grid_t0_ms") else None
        band = (hf.hf_bands_as_of(t0_unix) or {}).get(pair) if t0_unix else None
        p = paths.get(key) or {}
        recent_by_hk.setdefault(hk, []).append({
            "seq": seq,
            "t0_unix": t0_unix,
            "trade_pair": c.get("pair"),
            "asset_class": c.get("asset_class"),
            "direction": c.get("direction"),
            "status": {"wash": "washed", None: "pending"}.get(st, st),
            "outcome_bps": round(p["out_bps"]) if p.get("out_bps") is not None else None,
            "entry_price": p.get("entry"),
            "mfe_bps": p.get("mfe_bps"),
            "mae_bps": p.get("mae_bps"),
            "tp_bps": round(float(band[0])) if band else None,
            "sl_bps": round(float(band[1])) if band else None,
            "horizon_s": int(band[2]) if band else None,
        })

    # cadence strip + the subnet-level network block, both from the LF builder
    con = _signals_view(calls, grades, held_by_key, paths, registered)
    max_per_day = hf.hf_rules_as_of(now)[0]     # the "today 4/30" quota readout
    pace_by_hk, network = _pace_and_network(con, now, max_per_day)
    net_by_hk = (network or {}).get("by_hotkey") or {}
    con.close()

    rows = []
    for hk, a in agg.items():
        uid = hk2uid.get(hk)
        nm = names.get(uid, hk[:8] + "…") if uid else hk[:8] + "…"
        # the table keys rows by signals_user_id; a self-hosted miner has no user,
        # so give it a STABLE unique negative id from its hotkey (never collides
        # with real positive user ids, stable across refreshes).
        row_id = uid or -(int(hashlib.blake2b(hk.encode(), digest_size=6).hexdigest(), 16))
        won, lost = a["won"], a["lost"]
        dec = won + lost
        hit = round(100 * won / dec, 1) if dec else None
        conf = round(100 * _shrunk(won, dec), 1) if dec else None
        # A passing edge tier is not enough — the miner must also be past the HF
        # eligibility gate (50 submissions / 8 trading days) to count as qualified,
        # exactly as the validator's warmup replacement requires.
        tier = _classify(won, dec)
        qualified = tier is not None and eligible_by_hk.get(hk, False)
        if not qualified:
            tier = None
        ex = excur.get(hk) or {"r": [], "mfe": [], "mae": []}
        v = value_by_hk.get(hk)
        # % SHARE OF POOL is the miner's share of the MINER POOL, not of the whole
        # weight vector — the same denominator the LF board divides by
        # (publish_signals_snapshots._load_sn89_maps). hf_compute_weights returns
        # the on-chain vector, in which the residual up to 1.0 is BURN: with one
        # earner that vector reads 0.60 to the miner and 0.40 to burn, and the
        # column rendered "60%" for a trader taking the entire HF pool. Divide by
        # the FIXED cap, never by the non-burn sum, so a dust miner stays a
        # fraction of a percent instead of inflating to ~1/N when nobody earns.
        # NO current-qualification gate on the weight. hf_compute_weights is the
        # vector the validator commits, and it deliberately has no such gate:
        # earning is sized by the DECAYED tally of past qualified wins, so a miner
        # that drops below the gate keeps earning while that tally decays to zero
        # over HF_EMISSION_DECAY_S (7 days since 2026-07-31). Qualification governs whether NEW wins
        # enter the tally, never whether the existing tally pays.
        #
        # This line used to read `... if qualified else 0.0`, re-imposing exactly
        # the cliff the no-cliff design removes. Harold, 2026-07-31: one qualified
        # win 13.8h old (mult 1.083, decay 0.713, tally 0.772) — the real vector
        # paid him 0.600 while the board rendered 0.000 and the ledger below
        # accrued him nothing. The site under-reported live on-chain earnings.
        w_chain = float(weights.get(hk, 0.0))
        cap = hf.HF_MINER_EMISSION_CAP or 1.0
        rows.append({
            "signals_user_id": row_id,
            "name": nm,
            "wallet_truncated": None, "wallet_chain": None,
            "status": "active",
            "won_mtd": won, "lost_mtd": lost, "washed_mtd": a["wash"], "open_mtd": a["pending"],
            "won_lifetime": won, "lost_lifetime": lost,
            "hit_rate_pct": hit,
            "payout_lifetime_usd": 0.0, "emissions_usd": 0.0, "earned_total_usd": 0.0,
            "qualified": qualified, "badge": tier,
            # the volume half of the gate, so the UI can say "6 of 8 trading days"
            # instead of only "not qualified". eligible == both halves satisfied.
            "eligible": bool(eligible_by_hk.get(hk, False)),
            "gate": gate_by_hk.get(hk),
            # tracked data points (published, non-gating — hit rate decides tier)
            "avg_r": round(sum(ex["r"]) / len(ex["r"]), 4) if ex["r"] else None,
            "avg_mfe_bps": round(sum(ex["mfe"]) / len(ex["mfe"]), 1) if ex["mfe"] else None,
            "avg_mae_bps": round(sum(ex["mae"]) / len(ex["mae"]), 1) if ex["mae"] else None,
            "path_window_decisive": dec,
            "path_window_days": round(hf.HF_HIT_RATE_WINDOW_S / 86400.0),
            "bittensor_hotkey": hk,
            "bittensor_hotkey_truncated": f"{hk[:6]}…{hk[-4:]}",
            # pool share flows ONLY to qualified miners; the residual is burn, which
            # hf_compute_weights parks on a UID slot we must not paint as a trader.
            "emission_weight": min(1.0, w_chain / cap),
            # the raw mecid-1 vector entry, kept because it is what the TAO accrual
            # below integrates against — the displayed share is post-cap and would
            # over-pay by 1/cap if it were used here too
            "emission_weight_chain": w_chain,
            # wins & their values — Value× column + per-win ledger drawer
            "multiplier": (v or {}).get("value_multiplier"),
            "tier_multiplier": (v or {}).get("tier_multiplier"),
            "efficiency": (v or {}).get("efficiency"),
            "raw_hit_pct": (v or {}).get("raw_hit_pct"),
            "tier_raw": tier,
            "wins": _win_ledger(v, calls_by_t0.get(hk) or {}, paths, now),
            # the miner's own HF calls, every status — the per-miner page reads this
            "recent_calls": _recent_calls(recent_by_hk.get(hk) or []),
            # HF has no seal/reveal timelock (instant receipts), so nothing is ever
            # "sealed awaiting reveal" — washes live in the per-pair breakdown.
            "sn89_won": won, "sn89_lost": lost, "sn89_sealed": 0,
            "sn89_pending": a["pending"],
            "sn89_status": ("qualified" if qualified else "not qualified"),
            "sn89_tier": tier, "sn89_conf_hit_pct": conf,
            "sn89_assets": sorted(assets_by.get(hk, {}).values(),
                                  key=lambda x: -x["n"]),
            # when they submit, and how much of their result is just the field's
            "sn89_pace": pace_by_hk.get(hk),
            "sn89_independence": (net_by_hk.get(hk) or {}).get("independence"),
            "sn89_rho_vs_network": (net_by_hk.get(hk) or {}).get("rho_vs_network"),
            "sn89_co_submission_pct": (net_by_hk.get(hk) or {}).get("co_submission_pct"),
        })

    # HF (mecid-1) emission → USD, the mirror of the LF publisher's per-miner
    # accrual. It accrues off emission_weight_chain — the REAL vector, including a
    # decaying residual for a miner that has dropped below the gate. While that
    # value was being zeroed for unqualified miners this ledger silently stopped
    # accruing for someone the chain was still paying. hf_pool_tao_day is mecid-1's share of the
    # miner pool (0 to all miners while HF is 100% burn — the plumbing is correct
    # and simply reads zero until someone clears the gate).
    hf_pool = _hf_pool_tao_day()
    tao_px = _tao_usd()
    cum_by_hk = _accrue_hf_emissions(
        {r["bittensor_hotkey"]: r["emission_weight_chain"] for r in rows}, hf_pool, now)
    for r in rows:
        em_usd = round(cum_by_hk.get(r["bittensor_hotkey"], 0.0) * tao_px, 2)
        r["emissions_usd"] = em_usd
        r["earned_total_usd"] = round(em_usd + (r["payout_lifetime_usd"] or 0.0), 2)

    # rank: qualified + emission_weight first, then decisive volume
    rows.sort(key=lambda r: (r["emission_weight"], r["won_lifetime"] + r["lost_lifetime"]),
              reverse=True)
    n_qual = sum(1 for r in rows if r["qualified"])
    n_active = sum(1 for r in rows if (r["emission_weight"] or 0) > 0)

    doc = {
        "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mechanism": 1,
        "n_traders": len(rows),
        "n_traders_qualified": n_qual,
        "n_miners_active": n_active,
        "n_submitters_3d": _n_submitters_last_days(3),
        "hf_pool_tao_day": round(hf_pool, 4),          # mecid-1 daily pool (HF share)
        "hf_field_tao_day": round(hf_pool * (hf.HF_MINER_EMISSION_CAP), 4),
        "miner_cap_pct": round(hf.HF_MINER_EMISSION_CAP * 100, 1),
        "burn_pct": round((1.0 - hf.HF_MINER_EMISSION_CAP) * 100, 1),
        "total_emissions_usd": round(sum(r["emissions_usd"] for r in rows), 2),
        # HF-only crowding / correlation / pulse, same shape as the LF board's
        # sn89_network so the panel component is shared rather than forked
        "sn89_network": network,
        # linear emission decay for the win ledger — 48h on HF, 7d on LF
        "win_decay_days": round(hf.HF_EMISSION_DECAY_S / 86400.0, 2),
        "leaderboard": rows,
        # refused submissions, keyed by hotkey and OUTSIDE the leaderboard — read
        # by the per-miner page only. See _refusals for why they are not rows.
        "refusals": _refusals(),
    }
    tmp = OUT + ".tmp"
    Path(tmp).write_text(json.dumps(doc, separators=(",", ":")))
    os.replace(tmp, OUT)
    try:
        os.chmod(OUT, 0o644)
    except OSError:
        pass
    print(f"wrote {OUT}: {len(rows)} traders, {n_qual} qualified, {n_active} earning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
