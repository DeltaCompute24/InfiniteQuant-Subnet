#!/usr/bin/env python3
"""Publish SN89 mechanism state for the website.

The webhook venv has no bittensor, so the chain read happens here (sn89 venv) and
lands in a JSON file the bands API serves. Values change at most every ~2.4 h
(OwnerHyperparamRateLimit), so a few-minute refresh is ample.

Writes: emission split per mechanism, the HF board with horizons, and the wash
target the bands were sized against.
"""
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, hf                              # noqa: E402

OUT = Path(os.environ.get(
    "IQ_SN89_MECH_STATE",
    "/opt/iq-platform/data/live/sn89-mechanisms.json"))
NETUID = int(os.environ.get("SN89_NETUID", "89"))
NETWORK = os.environ.get("SN89_NETWORK", "finney")


def chain_split():
    try:
        import bittensor as bt
        sub = bt.Subtensor(NETWORK).substrate
        n = sub.query("SubtensorModule", "MechanismCountCurrent", [NETUID]).value
        raw = sub.query("SubtensorModule", "MechanismEmissionSplit", [NETUID]).value
        if not raw:
            return int(n or 1), [100.0] + [0.0] * (int(n or 1) - 1), None
        tot = sum(raw) or 1
        return int(n or len(raw)), [round(100.0 * x / tot, 2) for x in raw], list(raw)
    except Exception as e:                                       # noqa: BLE001
        print(f"chain read failed: {e}", file=sys.stderr)
        return None, None, None


def main() -> int:
    n_mech, pct, raw = chain_split()
    # AS OF NOW, not pinned to launch. Pinned, this published the v1 board forever:
    # the HF bands tab on the website went on listing EURUSD, GBPUSD and USDJPY for
    # the nine days after the 08-13 fx cull removed them, never listed TAOUSD,
    # HYPEUSD or AUDUSD, and quoted XAUUSD at 12 bps against a live 10. A trader
    # planning entries off that page was planning against a board the ingest
    # refuses. The board is as-of-versioned so a reader can resolve the one in
    # force at a given instant; for a live display that instant is now.
    board_t = time.time()
    board = hf.hf_bands_as_of(board_t) or {}
    pairs = {}
    for pair, (tp, sl, horizon_s, cls) in board.items():
        pairs[pair] = {
            "asset_class": cls,
            "tp_bps": tp, "sl_bps": sl,
            "horizon_s": horizon_s,
            "wash_after_s": horizon_s,      # unresolved at the horizon = wash
            "grid_ms": hf.grid_ms_for(pair, board_t),
            "spread_ratio": hf.band_spread_ratio(pair, board_t),
        }
    # AS OF NOW, like the board above and like mechanism 0 below. Pinned, this
    # published the launch-era limits forever, so the open-position gate
    # (max_open_per_pair 1 from HF_OPEN_GATE_FROM) would have gone on advertising
    # the 4 that was never enforced.
    cap_day, gap_ms, max_open = hf.hf_rules_as_of(time.time())

    # Combined era: the chain split no longer describes where miner rewards go
    # ([65535, 0] since the 2026-08-03 cutover — everything flows through
    # mecid-0 and divides by the validator's competition weights). Publish the
    # per-competition pool percentages so the site never renders the raw split
    # as "LF 100% / HF 0%".
    now = time.time()
    combined = config.combined_weights_active(now)
    pools_pct = None
    if combined and pct:
        shares = config.comp_weights_as_of(now)
        m0 = pct[0]
        pools_pct = {c: round(m0 * sh, 2)
                     for c, sh in shares.items() if c != "reserve"}
        # Referrers are exclusively mecid-1. Display their pool as the mecid-1
        # chain slice plus any in-band `reserve` placeholder (the 20% burned
        # inside mecid-0 while the split waits out its 24h rate limit) — the
        # reserve IS the referrer carve-out, held as burn until the chain
        # routes it to mechanism 1.
        pools_pct["referrers"] = round(
            m0 * shares.get("reserve", 0.0)
            + (pct[1] if len(pct) > 1 else 0.0), 2)

    # Per-competition daily TAO, live (the website's scoreboard.json is baked at
    # deploy time, so a tab that reads only that shows a stale pool forever).
    pools_tao = None
    try:
        _pool = json.loads(Path("/opt/iq-platform/data/live/sn89-pool.json")
                           .read_text())
        gross = float(_pool.get("miner_pool_tao_day") or 0.0)
        cap = 1.0
        try:
            _st = json.loads(Path("/opt/iq-platform/data/live/"
                                  "validator-standing.json").read_text())
            cap = float((_st.get("weights") or {}).get("miner_cap_pct")
                        or 100.0) / 100.0
        except Exception:  # noqa: BLE001
            pass
        if gross and pools_pct:
            pools_tao = {"gross_day": round(gross, 4),
                         "miner_cap_pct": round(cap * 100, 2)}
            for comp, ppct in pools_pct.items():
                pools_tao[comp] = round(gross * ppct / 100.0, 4)
                pools_tao[comp + "_field"] = round(
                    gross * ppct / 100.0 * cap, 4)
    except Exception:  # noqa: BLE001
        pools_tao = None

    doc = {
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pools_tao_day": pools_tao,
        "netuid": NETUID,
        "mechanism_count": n_mech,
        "emission_split_pct": pct,          # index = mecid (raw chain view)
        "emission_split_raw": raw,
        "combined": combined,
        "pools_pct": pools_pct,             # {lf, hf, closers, referrers} of miner emission
        "mechanisms": {
            "0": {"name": "Signals", "slug": "lf", "live": True,
                  "emission_pct": (pools_pct.get("lf", 0.0) if pools_pct
                                   else (pct or [100.0])[0]),
                  "max_per_utc_day": config.submission_rules_as_of(time.time())[0],
                  "min_gap_s": config.submission_rules_as_of(time.time())[1]},
            "1": {"name": "Signals HF", "slug": "hf",
                  "live": (bool(pools_pct and pools_pct.get("hf", 0) > 0)
                           if combined else
                           bool(pct and len(pct) > 1 and pct[1] > 0)),
                  # board_from is when the HF board began to exist; it is NOT a
                  # promise about earning. `live` above is the only earning signal.
                  "board_from": time.strftime(
                      "%Y-%m-%dT%H:%M:%SZ", time.gmtime(hf.HF_LAUNCH_FROM)),
                  "emission_pct": (pools_pct.get("hf", 0.0) if pools_pct else
                                   ((pct or [100.0, 0.0])[1]
                                    if pct and len(pct) > 1 else 0.0)),
                  "max_per_utc_day": cap_day,
                  "min_gap_ms": gap_ms,
                  "max_open_per_pair": max_open,
                  "pair_lock_s": hf.PAIR_LOCK_S,
                  "resolve_target_pct": 80,
                  "board": pairs},
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, OUT)
    try:
        os.chmod(OUT, 0o644)
    except OSError:
        pass
    print(f"wrote {OUT}  split={pct}  hf_pairs={len(pairs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
