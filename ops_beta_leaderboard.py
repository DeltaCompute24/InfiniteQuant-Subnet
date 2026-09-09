"""Publish the Custom Sizing (Testnet) beta leaderboard.

READ-ONLY on the validator's grade cache, and it never re-derives a rule. It
calls the same _history() and hf_compute_weights() the validator calls, one step
after sync_and_grade -- so the board on the website is the vector the chain got,
not a second implementation that agrees until it doesn't.

That is the whole point of the file. tools/qualify_report.py reimplemented the
gate and reported against the retired QUALIFY_MIN_HIT for two months while
claiming to match the validator. The rule here is: if a number is on this board,
a validator function produced it.

Deliberately does NOT call sync_and_grade -- the validator owns that cache and a
second writer racing it is how a shared grade cache goes wrong. The board is at
most one validator cycle stale, which is stated in the payload as `as_of`.

  ssh iq-main 'cd /opt/sn89-signals && set -a && . ./.env.test && set +a && \
               .venv/bin/python ops_beta_leaderboard.py'
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time

from sn89_signals import config, hf, hf_grade, scoring

OUT = os.getenv("SN89_BETA_BOARD_OUT",
                "/opt/iq-platform/data/live/sn89-beta-leaderboard.json")
ROSTER = os.getenv("SN89_BETA_ROSTER",
                   "/opt/iq-platform/data/live/sn89-beta-testnet-hotkeys.json")
# Handles live on the MAINNET roster, joined through the beta key's mainnet_hotkey.
# The testnet key is an alias we minted; the trader's own name is attached to the
# key they already had.
MAIN_STANDING = os.getenv("SN89_MAIN_STANDING",
                          "/opt/iq-platform/data/live/sn89-standing-main.json")
CACHE = os.path.expanduser(os.getenv("SN89_HF_GRADE_CACHE", "~/.sn89/hf-grade"))


def _handles() -> tuple[dict, dict]:
    """testnet hotkey -> the handle the trader knows themselves by.

    The beta roster maps testnet_hotkey -> mainnet_hotkey; the mainnet standing
    snapshot carries the name. Falls back to a truncated key, and NEVER to a
    fuzzy match -- serving one trader's handle against another trader's row is
    the caller_profiles bare-handle bug, and on a public board it is worse.
    """
    names = {}
    try:
        with open(MAIN_STANDING) as fh:
            for r in json.load(fh).get("roster", []):
                hk = r.get("hotkey")
                if hk:
                    names[hk] = (r.get("x_handle") or r.get("tenant_user")
                                 or r.get("name") or None)
    except (FileNotFoundError, ValueError):
        pass

    out = {}
    try:
        with open(ROSTER) as fh:
            issued = json.load(fh).get("issued", [])
    except (FileNotFoundError, ValueError):
        issued = []
    for r in issued:
        thk = r.get("testnet_hotkey")
        if not thk:
            continue
        out[thk] = names.get(r.get("mainnet_hotkey")) or (thk[:6] + "\u2026")
    return out, issued


def _uids() -> dict:
    """hotkey -> uid for the WHOLE metagraph, deliberately unfiltered.

    Neither roster file carries a uid and neither standing snapshot does either
    (checked: 0 of 275 rows). The metagraph is the only source, and it is the
    same source the validator passes into hf_compute_weights -- so reading it
    here keeps the board on the vector's own uid map rather than a cached one
    that can drift after a dereg.

    !! Do NOT narrow this to the beta cohort. hf_compute_weights NORMALIZES over
    the uids it is handed, so a filtered map produces a different vector than the
    validator's and the board would report percentages that never went on chain.
    The cohort is a DISPLAY concern and is applied after the vector exists.
    """
    try:
        import bittensor as bt
        mg = bt.Subtensor(config.NETWORK).metagraph(config.NETUID)
        return {hk: i for i, hk in enumerate(mg.hotkeys)}
    except Exception as e:                    # a board is better than no board
        print("metagraph unavailable (%s); weights will render as unknown" % e)
        return {}



# Carrying a mainnet record: moved to sn89_signals/beta_carry.py so the
# VALIDATOR applies it too (Whit 2026-09-09: carried miners earn from call one).
from sn89_signals import beta_carry
mainnet_record = beta_carry.mainnet_record
_lf_board_sigma = beta_carry.carry_sigma_for
LF_DB = beta_carry.LF_DB


def custom_detail(hk):
    """Per-pair breakdown of THIS trader's beta calls, for the row drawer.

    Beta calls only. The qualification test also reads their mainnet record, but
    that record is not what this competition is about, and mixing the two into one
    count is what made the Calls column read 354/30 next to an unqualified badge.
    """
    out = {}
    try:
        db = sqlite3.connect("file:%s/hf_grades.db?mode=ro" % CACHE, uri=True)
        for pair, st, tp, hz in db.execute(
                "SELECT pair, status, tp_bps, horizon_s FROM grades WHERE hk=?", (hk,)):
            r = out.setdefault(pair, {"pair": pair, "n": 0, "won": 0, "lost": 0,
                                      "wash": 0, "_tp": [], "_hz": []})
            r["n"] += 1
            if st in ("won", "lost", "wash"):
                r[st] += 1
            if tp:
                r["_tp"].append(float(tp))
            if hz:
                r["_hz"].append(int(hz))
        db.close()
    except Exception as e:                                          # noqa: BLE001
        print("  ! detail unreadable for %s: %s" % (hk[:10], e))
        return []
    rows = []
    for r in out.values():
        tp, hz = r.pop("_tp"), r.pop("_hz")
        r["band_bps"] = round(sum(tp) / len(tp), 1) if tp else None
        r["mins"] = round(sum(hz) / len(hz) / 60) if hz else None
        rows.append(r)
    rows.sort(key=lambda r: -r["n"])
    return rows


# How many calls the drawer carries per trader. The qualify rule is stated over
# 30 resolved calls, so a reader can see the whole window that decides it plus a
# little history; `n` on the row still counts every call.
CALLS_SHOWN = 40


def custom_calls(hk, calls, now):
    """Every beta call by THIS trader, newest first, each with the points it
    banked -- for the row drawer.

    `calls` is scoring.qualified_calls' output for the same hotkey: the signed
    points the VALIDATOR credited, keyed by t0. That join is the whole design.
    A call missing from it earned nothing, and the drawer says so per row
    rather than re-deriving the gate here and disagreeing with the chain later
    (the qualify_report.py failure, again).

    Per-call fields:
      shape  what a WIN on this band/window pays (points_for). Informational:
             it is what the trader chose to risk, whether or not it banked.
      pts    signed points that entered the tally: +shape on a win, -shape on a
             loss, 0 on a wash. None = never entered (below the gate at t0,
             void, or unpriceable), with `note` saying which.
      live   pts after the rolling-window decay and the per-day cap, i.e. this
             call's contribution to the tally right now. None outside the
             window or when the day's cap dropped it (`note`: expired / capped).
    The per-call `live` values are summed and checked against
    scoring.decayed_points_tally so the drawer cannot drift from the number on
    the row.
    """
    banked = {}
    for t0, p in calls:
        banked[round(float(t0), 3)] = p
    rows = []
    try:
        db = sqlite3.connect("file:%s/hf_grades.db?mode=ro" % CACHE, uri=True)
        q = db.execute(
            "SELECT t0_ms, pair, direction, status, tp_bps, sl_bps, horizon_s "
            "FROM grades WHERE hk=? ORDER BY t0_ms ASC", (hk,))
        for t0_ms, pair, direction, st, tp, sl, hz in q:
            t0 = t0_ms / 1000.0
            sigma = hf._board_sigma_for(pair, t0) if (pair and tp and hz) else 0.0
            shape = (scoring.points_for(float(tp), int(hz), sigma)
                     if (tp and hz and sigma > 0) else None)
            pts = None
            note = None
            if st == "wash":
                pts = 0.0
            elif st in ("won", "lost"):
                p = banked.get(round(t0, 3))
                if p is not None:
                    pts = p
                elif shape is None:
                    note = "unpriceable"
                else:
                    note = "gate"
            elif st == "void":
                note = "void"
            rows.append({
                "t0": int(t0), "pair": pair, "dir": direction or "",
                "result": st,
                "tp_bps": float(tp) if tp else None,
                "sl_bps": float(sl) if sl else None,
                "mins": round(int(hz) / 60) if hz else None,
                "shape": round(shape, 2) if shape is not None else None,
                "pts": round(pts, 2) if pts is not None else None,
                "live": None, "note": note,
            })
        db.close()
    except Exception as e:                                          # noqa: BLE001
        print("  ! calls unreadable for %s: %s" % (hk[:10], e))
        return []

    # Live contribution: the same window + chronological per-day cap that
    # decayed_points_tally applies, written per call so the drawer can show
    # which calls are still paying. A wash is in the window but worth 0.
    W = config.HF_POINTS_WINDOW_S
    per_day = {}
    total = 0.0
    for r in rows:                                # ascending t0 already
        if r["pts"] is None:
            continue
        age = now - r["t0"]
        if not (0.0 <= age < W):
            r["note"] = r["note"] or "expired"
            continue
        day = int(r["t0"] // 86_400)
        k = per_day.get(day, 0)
        if k >= config.HF_POINTS_DAILY_CAP:
            r["note"] = r["note"] or "capped"
            continue
        per_day[day] = k + 1
        r["live"] = round(r["pts"] * (1.0 - age / W), 3)
        total += r["pts"] * (1.0 - age / W)
    ref = scoring.decayed_points_tally(calls, now)
    if abs(total - ref) > 0.05:
        print("  ! per-call live sum %.3f != tally %.3f for %s -- drawer and "
              "row disagree, check the cap/window replica" % (total, ref, hk[:10]))
    rows.reverse()                                # newest first for the reader
    return rows[:CALLS_SHOWN]


def main() -> None:
    now = time.time()
    handles, issued = _handles()
    main_of = {r["testnet_hotkey"]: r.get("mainnet_hotkey")
               for r in issued if r.get("testnet_hotkey")}
    uid_of = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % LF_DB, uri=True)
        by_main = {m: i for i, m in c.execute(
            "SELECT id, sn89_hotkey FROM signals_users "
            "WHERE sn89_hotkey IS NOT NULL AND sn89_hotkey<>''")}
        uid_of = {t: by_main[m] for t, m in main_of.items() if m in by_main}
        c.close()
    except Exception as e:                                         # noqa: BLE001
        print("  ! could not map hotkeys to signals users: %s" % e)
    uid_by_hk = _uids()

    # The validator's own history read. as_of=now so the causal windows match.
    dec, fs, subs, graded, washes = hf_grade._history(CACHE, as_of=now)

    # The validator's own weight vector, from the same five structures.
    prior_by_hk = beta_carry.load_prior_by_hk()
    weights = hf.hf_compute_weights(dec, fs, uid_by_hk, now, subs, graded, washes,
                                    prior_by_hk=prior_by_hk or None,
                                    prior_sigma_for=beta_carry.carry_sigma_for)
    wsum = sum(weights.values()) or 1.0

    # Per-wash emission cut (testnet only): when a miner is inside the window,
    # the board says so and until when, read from the same resolved-wash times
    # the vector was built from. None = not cut, never a stale timestamp.
    cut_armed = config.wash_cut_enforced_as_of(now)

    def cut_until(hk):
        if not cut_armed:
            return None
        r = hf._wash_resolved(washes.get(hk), now)
        live = [t for t in r if 0.0 <= now - t < config.HF_WASH_CUT_S]
        return int(max(live) + config.HF_WASH_CUT_S) if live else None

    rows = []
    for hk, d in dec.items():
        prior = prior_by_hk.get(hk, [])
        calls = scoring.qualified_calls(d, fs.get(hk, 0.0),
                                        sigma_for=hf._board_sigma_for,
                                        prior=prior,
                                        prior_sigma_for=beta_carry.carry_sigma_for)
        pts = scoring.decayed_points_tally(calls, now)
        # QUALIFY ON THE FULL RECORD: this beta's calls plus whatever the trader
        # already did on mainnet HF and LF. Same statistic, more evidence.
        gate = scoring.points_test(list(d) + prior, sigma_for=_lf_board_sigma)
        uid = uid_by_hk.get(hk)
        w = weights.get(uid) if uid is not None else None
        rows.append({
            "hotkey": hk,
            "handle": handles.get(hk, hk[:6] + "…"),
            "points": round(pts, 3),
            "staked": round(gate["staked"], 1),
            "t": round(gate["t"], 3),
            # CALLS IS THIS COMPETITION'S CALLS. The gate reads the carried mainnet
            # record too, but showing that total here put "354/30" beside a row
            # that was not qualified, which reads as a broken board.
            "n": len(d),
            "n_carried": max(0, gate["n"] - len(d)),
            "detail": custom_detail(hk),
            "calls": custom_calls(hk, calls, now),
            "wash_cut_until": cut_until(hk),
            "qualified": gate["qualified"],
            "beta": hk in handles,
            # None (renders as an em dash), never 0.0 -- a miner absent from the
            # vector has NOT been assigned a zero weight, and printing one is the
            # not-fetched-value-as-a-measured-zero bug.
            "weight_pct": None if w is None else round(w / wsum * 100.0, 2),
        })

    # A trader who already qualified on mainnet belongs on the board BEFORE their
    # first beta call. The panel promises "already qualified on the standard or
    # high-frequency board? You are qualified here", and a promise the reader
    # cannot see applied to them is one they have to take on trust.
    # They carry points 0 -- they have earned nothing in the beta yet -- but their
    # record and their qualified flag are real.
    for thk, mhk in (main_of or {}).items():
        if thk in dec:
            continue
        prior = prior_by_hk.get(thk, [])
        if not prior:
            continue
        g = scoring.points_test(prior, sigma_for=_lf_board_sigma)
        if not g["qualified"]:
            continue
        uid = uid_by_hk.get(thk)
        w = weights.get(uid) if uid is not None else None
        rows.append({
            "hotkey": thk, "handle": handles.get(thk, thk[:6] + "\u2026"),
            "points": 0.0, "staked": round(g["staked"], 1), "t": round(g["t"], 3),
            "n": 0, "n_carried": g["n"], "detail": [], "calls": [],
            "qualified": True, "beta": True, "carried": True,
            "weight_pct": None if w is None else round(w / wsum * 100.0, 2),
        })

    # Rank on the board's own subject: points. Qualified first, since an
    # unqualified miner is not competing for the pool yet.
    rows.sort(key=lambda r: (not r["qualified"], -r["points"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    payload = {
        "as_of": int(now),
        "netuid": config.NETUID,
        "network": config.NETWORK,
        "points_armed": config.points_enforced_as_of(now),
        "wash_cut_armed": cut_armed,
        "wash_cut_keep": config.wash_cut_keep() if cut_armed else None,
        "wash_cut_hours": round(config.HF_WASH_CUT_S / 3600, 1),
        "gamma": config.HF_POINTS_GAMMA,
        "window_days": round(config.HF_POINTS_WINDOW_S / 86400, 1),
        "rows": rows,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)          # atomic: a reader never sees half a board
    print("beta board: %d miners · %d qualified · netuid %s · -> %s"
          % (len(rows), sum(1 for r in rows if r["qualified"]),
             config.NETUID, OUT))


if __name__ == "__main__":
    main()
