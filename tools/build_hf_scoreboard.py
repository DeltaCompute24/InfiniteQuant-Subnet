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

import glob
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, hf     # noqa: E402

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
ADMIN_DB = os.getenv("IQ_SIGNALS_DB", "/opt/iq-platform/data/live/iq_admin_dash.db")
TENANTS = os.getenv("SN89_MANAGED_TENANTS_PATH",
                    "/opt/iq-platform/data/live/sn89-managed-main/tenants.json")
OUT = os.getenv("IQ_HF_SCOREBOARD",
                "/opt/iq-platform/data/live/sn89-hf-scoreboard.json")
# world-readable grade snapshot for the webhook's private per-call HF view (the
# validator's own cache is under /root, unreadable by the iq service user)
GRADE_PUBLISH = "/var/lib/sn89-hf/hf_grades.db"


def _accepted_calls() -> dict:
    """(hk, seq) -> {hk, pair, grid_t0_ms} from the accepted-window logs."""
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
                calls[(hk, seq)] = {
                    "hk": hk, "pair": p.get("trade_pair"),
                    "direction": p.get("direction"),
                    "asset_class": p.get("asset_class"),
                    "grid_t0_ms": rcpt.get("grid_t0_ms")}
        except (OSError, ValueError):
            continue
    return calls


def _grades():
    """Returns (status_by_key, rows) from the first readable grade cache:
    status_by_key = {'hk:seq': status}; rows = [(hk, t0_ms, status), ...]."""
    for db in GRADE_DBS:
        if not os.path.exists(db):
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            by_key = {k: s for k, s in c.execute("SELECT key, status FROM grades")}
            rows = list(c.execute("SELECT hk, t0_ms, status FROM grades"))
            c.close()
            _publish_grade_db(db)
            return by_key, rows
        except sqlite3.Error:
            continue
    return {}, []


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


def _mecid1_weights(grades_rows, subs_by_hk, now):
    """hotkey -> pool share, from the read-only grade history via the SAME
    hf_compute_weights the validator commits. No chain call, no cache mutation —
    the scoreboard only reads; the validator remains the sole writer of on-chain
    weights. Shares normalise, so a synthetic uid map gives each hotkey its share."""
    dec: dict = {}
    fs: dict = {}
    for hk, t0_ms, status in grades_rows:
        t0 = t0_ms / 1000.0
        fs[hk] = min(fs.get(hk, t0), t0)
        if status in ("won", "lost"):
            dec.setdefault(hk, []).append((t0, status == "won", False))
    for v in dec.values():
        v.sort(key=lambda x: x[0])
    hks = sorted(fs)
    uid_by_hk = {hk: i for i, hk in enumerate(hks)}
    try:
        w = hf.hf_compute_weights(dec, fs, uid_by_hk, now, subs_by_hk)
    except Exception as e:      # noqa: BLE001
        print(f"weights unavailable: {e}", file=sys.stderr)
        return {}
    hk_by_uid = {i: hk for hk, i in uid_by_hk.items()}
    return {hk_by_uid[u]: x for u, x in w.items() if u in hk_by_uid}


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
    grades, grade_rows = _grades()
    hk2uid, names = _identity()
    # HF eligibility (>=50 accepted submissions across >=8 distinct UTC trading
    # days) — the same gate the validator applies; the qualified badge must not
    # claim a miner is qualified before it can actually earn.
    subs_by_hk = _submissions_by_hk(grade_rows)
    eligible_by_hk = {hk: hf.hf_eligible_from(ts) is not None
                      for hk, ts in subs_by_hk.items()}
    # hotkey -> pool share (empty/zero until anyone qualifies)
    weights = _mecid1_weights(grade_rows, subs_by_hk, now)

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

    # aggregate per hotkey, and per (hotkey, pair) for the drawer breakdown
    agg: dict = {}
    assets_by: dict = {}      # hk -> {pair -> AssetRow-shaped dict}
    for (hk, seq), c in calls.items():
        if hk not in registered:
            continue
        a = agg.setdefault(hk, {"won": 0, "lost": 0, "wash": 0, "pending": 0})
        pair = c["pair"] or "?"
        pa = assets_by.setdefault(hk, {}).setdefault(pair, {
            "asset": pair, "asset_class": c.get("asset_class"),
            "n": 0, "longs": 0, "shorts": 0, "won": 0, "lost": 0, "washed": 0})
        pa["n"] += 1
        if c.get("direction") == "SHORT":
            pa["shorts"] += 1
        else:
            pa["longs"] += 1
        st = grades.get(f"{hk}:{seq}")
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
            "avg_r": None, "avg_mfe_bps": None, "avg_mae_bps": None,
            "bittensor_hotkey": hk,
            "bittensor_hotkey_truncated": f"{hk[:6]}…{hk[-4:]}",
            # pool share flows ONLY to qualified miners; the residual is burn, which
            # hf_compute_weights parks on a UID slot we must not paint as a trader.
            "emission_weight": float(weights.get(hk, 0.0)) if qualified else 0.0,
            # HF has no seal/reveal timelock (instant receipts), so nothing is ever
            # "sealed awaiting reveal" — washes live in the per-pair breakdown.
            "sn89_won": won, "sn89_lost": lost, "sn89_sealed": 0,
            "sn89_pending": a["pending"],
            "sn89_status": ("qualified" if qualified else "not qualified"),
            "sn89_tier": tier, "sn89_conf_hit_pct": conf,
            "sn89_assets": sorted(assets_by.get(hk, {}).values(),
                                  key=lambda x: -x["n"]),
        })

    # HF (mecid-1) emission → USD, the mirror of the LF publisher's per-miner
    # accrual. emission_weight is already the qualified-only share of the pool, so
    # unqualified rows accrue at rate 0. hf_pool_tao_day is mecid-1's share of the
    # miner pool (0 to all miners while HF is 100% burn — the plumbing is correct
    # and simply reads zero until someone clears the gate).
    hf_pool = _hf_pool_tao_day()
    tao_px = _tao_usd()
    cum_by_hk = _accrue_hf_emissions(
        {r["bittensor_hotkey"]: r["emission_weight"] for r in rows}, hf_pool, now)
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
        "total_emissions_usd": round(sum(r["emissions_usd"] for r in rows), 2),
        "leaderboard": rows,
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
