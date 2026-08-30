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


def main() -> None:
    now = time.time()
    handles, issued = _handles()
    uid_by_hk = _uids()

    # The validator's own history read. as_of=now so the causal windows match.
    dec, fs, subs, graded, washes = hf_grade._history(CACHE, as_of=now)

    # The validator's own weight vector, from the same five structures.
    weights = hf.hf_compute_weights(dec, fs, uid_by_hk, now, subs, graded, washes)
    wsum = sum(weights.values()) or 1.0

    rows = []
    for hk, d in dec.items():
        calls = scoring.qualified_calls(d, fs.get(hk, 0.0),
                                        sigma_for=hf._board_sigma_for)
        pts = scoring.decayed_points_tally(calls, now)
        gate = scoring.points_test(d, sigma_for=hf._board_sigma_for)
        uid = uid_by_hk.get(hk)
        w = weights.get(uid) if uid is not None else None
        rows.append({
            "hotkey": hk,
            "handle": handles.get(hk, hk[:6] + "…"),
            "points": round(pts, 3),
            "staked": round(gate["staked"], 1),
            "t": round(gate["t"], 3),
            "n": gate["n"],
            "qualified": gate["qualified"],
            "beta": hk in handles,
            # None (renders as an em dash), never 0.0 -- a miner absent from the
            # vector has NOT been assigned a zero weight, and printing one is the
            # not-fetched-value-as-a-measured-zero bug.
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
