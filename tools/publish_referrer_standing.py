#!/usr/bin/env python3
"""Publish the SN89 REFERRER standing — who earns the recruiter pool, and why.

The referrer mechanism pays a recruiter in proportion to what their recruits are
currently earning:

    score[recruiter] = Σ decayed_qwin_tally(recruit)   over valid, unsuspended pairs
    weight[recruiter] = MINER_EMISSION_CAP × score / Σ scores,  remainder burns

`replay.referrer_weights_from_journal` returns only the final {uid: weight} the
validator commits, which answers "how much" and not "why". This mirrors the same
pipeline through the same primitives, keeps the intermediate per-recruit tallies,
and then **asserts its own vector equals replay's** — so the published breakdown
can never drift from the vector actually committed to mecid-1. On mismatch it
writes nothing and exits nonzero; a stale file is better than a plausible wrong one.

Pool figures come from sn89-mechanisms.json rather than being recomputed, so the
recruiter pool here and the pool on the mechanisms tab are one number.

⚠ The pool is not payable today. Under combined weights the chain split is
[65535, 0], so mecid-1 has no slice; the 20% `reserve` competition share is the
referrer carve-out held as burn inside mecid-0 until the split moves. `pool.payable`
carries that, and any surface quoting a recruiter's TAO must respect it.

Writes: /opt/iq-platform/data/live/sn89-referrer-standing.json
"""
import json, os, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, "/opt/sn89-signals")
from sn89_signals import config, replay, scoring                 # noqa: E402

OUT = Path(os.environ.get(
    "IQ_SN89_REFERRER_STANDING",
    "/opt/iq-platform/data/live/sn89-referrer-standing.json"))
MECH = Path(os.environ.get(
    "IQ_SN89_MECH_STATE",
    "/opt/iq-platform/data/live/sn89-mechanisms.json"))
NETUID = int(os.environ.get("SN89_NETUID", "89"))
NETWORK = os.environ.get("SN89_NETWORK", "finney")


def load_journal(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sig_rows = [
            {"commit_hex": ch, "hotkey": hk, "t0_unix": t0, "status": st,
             "is_copy": int(cp or 0), "plaintext": pt}
            for ch, hk, t0, st, cp, pt in con.execute(
                "SELECT commit_hex, hotkey, t0_unix, status, is_copy, plaintext "
                "FROM signals")]
        meta = {hk: {"first_seen_unix": fs, "strikes": int(sk or 0)}
                for hk, fs, sk in con.execute(
                    "SELECT hotkey, first_seen_unix, strikes FROM hotkey_meta")}
        referrals = [
            {"recruiter_hk": r, "recruit_hk": c, "commit_block": cb,
             "recruit_reg_block": rb}
            for r, c, cb, rb in con.execute(
                "SELECT recruiter_hk, recruit_hk, commit_block, recruit_reg_block "
                "FROM referrals")]
        transfers = [
            {"from_hk": f, "to_hk": t, "commit_block": cb}
            for f, t, cb in con.execute(
                "SELECT from_hk, to_hk, commit_block FROM referral_transfers")]
    finally:
        con.close()
    return sig_rows, meta, referrals, transfers


def competition_tallies(sig_rows, meta, uid_by_hotkey, now):
    """{competition: {hotkey: raw tally}} over the FULL field of each.

    Mirrors what neurons/validator.py hands replay.referrer_weights_from_journal
    on the mecid-1 path, including the Closers qualified-set gate, which is
    derived from the LF and HF vectors exactly as the validator derives it. A
    competition that cannot be computed is OMITTED (its share drops and the rest
    rescale), never zeroed — zeroing would silently reprice every recruiter on a
    transient feed error.
    """
    tallies = {"lf": replay.referrer_recruit_tallies(
        sig_rows, meta, now, set(meta))}
    if not config.referrer_multicomp_active(now):
        return tallies, []

    missing = []
    lf_vec = hf_vec = None
    try:
        lf_vec = replay.weights_from_journal(sig_rows, meta, uid_by_hotkey, now)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! LF vector unavailable (closers gate degraded): {e}",
              file=sys.stderr)
    try:
        from sn89_signals import hf_grade
        tallies["hf"] = hf_grade.mecid1_tallies(uid_by_hotkey, now)
        hf_vec = hf_grade.mecid1_weights(uid_by_hotkey, now)
    except Exception as e:                                       # noqa: BLE001
        missing.append("hf")
        print(f"  ! HF tallies unavailable: {e}", file=sys.stderr)
    try:
        from sn89_signals import closers
        hk_by_uid = {u: h for h, u in uid_by_hotkey.items()}
        qual = {hk_by_uid[u]
                for vec in (lf_vec or {}, hf_vec or {})
                for u, wt in vec.items()
                if wt > 0 and u != config.BURN_UID and u in hk_by_uid}
        tallies["closers"] = closers.closers_tallies(
            uid_by_hotkey, now, qualified_hks=qual)
    except Exception as e:                                       # noqa: BLE001
        missing.append("closers")
        print(f"  ! Closers tallies unavailable: {e}", file=sys.stderr)
    return tallies, missing


def main() -> int:
    now = time.time()
    sig_rows, meta, referrals, transfers = load_journal(config.DB_PATH)

    import bittensor as bt
    mg = bt.Subtensor(NETWORK).metagraph(NETUID)
    uid_by_hotkey = {hk: i for i, hk in enumerate(mg.hotkeys)}

    pairs = scoring.valid_referral_pairs(referrals)
    pairs = scoring.apply_referral_transfers(pairs, transfers)

    fields, missing = competition_tallies(sig_rows, meta, uid_by_hotkey, now)
    multicomp = config.referrer_multicomp_active(now)
    shares = (scoring.referrer_shares(config.comp_weights_as_of(now))
              if multicomp else {"lf": 1.0})
    shares = {k: v for k, v in shares.items() if k in fields}
    # Renormalize after dropping a competition that failed to compute, so the
    # published contributions add up to the score they are supposed to explain.
    _tot = sum(shares.values())
    shares = {k: v / _tot for k, v in shares.items()} if _tot > 0 else {}

    # Per-recruit, per-competition contribution — the "why" the page renders.
    # Same normalize-then-weight the score itself uses, kept apart so a recruit
    # can be told which competition their credit came from.
    contrib = {}
    for comp, share in shares.items():
        field = fields.get(comp) or {}
        total = sum(v for v in field.values() if v > 0)
        if total <= 0:
            continue
        for hk, v in field.items():
            if v > 0:
                contrib.setdefault(hk, {})[comp] = share * (v / total)

    tally = {hk: sum(c.values()) for hk, c in contrib.items()}
    # `tally` spans the WHOLE field (each competition is normalized over its own
    # participants, not over recruits), so the recruits-scoring count has to be
    # restricted back down to recruits — otherwise it reports every earning
    # miner on the subnet as somebody's recruit.
    recruit_hks = {c for _, c in pairs}
    scored = {hk: v for hk, v in tally.items() if v and hk in recruit_hks}
    scores = scoring.referrer_scores(pairs, tally)
    weights = scoring.referrer_weights(scores, uid_by_hotkey)

    # The whole reason this file is trustworthy: our vector must BE the
    # validator's vector. Compare against the canonical replay, not a re-read of
    # our own arithmetic.
    canon = replay.referrer_weights_from_journal(
        sig_rows, meta, uid_by_hotkey, now,
        referrals=referrals, referral_transfers=transfers,
        extra_tallies={k: v for k, v in fields.items() if k != "lf"})
    if set(canon) != set(weights) or any(
            abs(canon[u] - weights[u]) > 1e-9 for u in canon):
        print("REFUSING TO WRITE — breakdown disagrees with replay vector.\n"
              f"  replay: {canon}\n  local:  {weights}", file=sys.stderr)
        return 1

    by_recruiter = {}
    for recruiter, recruit in pairs:
        by_recruiter.setdefault(recruiter, []).append(recruit)

    try:
        mech = json.loads(MECH.read_text())
    except Exception:                                            # noqa: BLE001
        mech = {}
    pools_tao = mech.get("pools_tao_day") or {}
    pools_pct = mech.get("pools_pct") or {}
    split = mech.get("emission_split_pct") or []
    chain_slice = float(split[1]) if len(split) > 1 else 0.0
    payable = chain_slice > 0
    field_day = float(pools_tao.get("referrers_field") or 0.0)

    rows = []
    for recruiter in sorted(by_recruiter):
        uid = uid_by_hotkey.get(recruiter)
        score = float(scores.get(recruiter, 0.0))
        share = float(weights.get(uid, 0.0)) if uid is not None else 0.0
        recruits = []
        for c in sorted(by_recruiter[recruiter]):
            t = tally.get(c)
            on_chain = c in uid_by_hotkey
            recruits.append({
                "hotkey": c,
                "registered": on_chain,
                # None = this hotkey is not on chain at all, so no competition
                # can score it; 0.0 = on chain and earning nothing. The two must
                # not render the same.
                "tally": None if not on_chain else round(t or 0.0, 8),
                # Which competitions the credit came from, so a recruiter can
                # see that a recruit earned them something on HF while doing
                # nothing on LF.
                "by_competition": {k: round(v, 8)
                                   for k, v in (contrib.get(c) or {}).items()
                                   if v > 0},
                "share_of_score": round(t / score, 6) if (score and t) else 0.0,
            })
        rows.append({
            "hotkey": recruiter,
            "uid": uid,
            "registered": uid is not None,
            "score": round(score, 6),
            "pool_share": round(share, 8),
            "tao_day": round(share * field_day, 6) if payable else 0.0,
            "n_recruits": len(recruits),
            "n_scoring": sum(1 for r in recruits if r["tally"]),
            "recruits": recruits,
        })
    rows.sort(key=lambda r: (-r["pool_share"], -r["score"], -r["n_recruits"]))

    doc = {
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "netuid": NETUID,
        "mechanism": "referrers",
        "pool": {
            "pct_of_miner_emission": pools_pct.get("referrers"),
            "tao_day": pools_tao.get("referrers"),
            "field_tao_day": pools_tao.get("referrers_field"),
            "mecid": 1,
            "chain_slice_pct": chain_slice,
            "payable": payable,
            # Reserved and burning is NOT the same as absent. Say which.
            "note": ("Paying from mechanism 1." if payable else
                     "Reserved, not yet payable. Mechanism 1 holds no emission "
                     "slice, so the pool is held as burn inside mechanism 0 "
                     "until the split moves."),
        },
        "burn_share": round(float(weights.get(config.BURN_UID, 0.0)), 8),
        "score_basis": {
            "multicomp": multicomp,
            # What a recruit's performance is worth to their recruiter, per
            # competition. Before the multicomp flip this is {"lf": 1.0} and a
            # recruit's HF/Closers work is worth nothing to anyone.
            "shares": {k: round(v, 6) for k, v in shares.items()},
            "unavailable": missing,
            "multicomp_from_unix": config.REFERRER_MULTICOMP_FROM_UNIX,
        },
        "totals": {
            "recruiters": len(rows),
            "pairs": len(pairs),
            "recruits_scoring": len(scored),
            "recruiters_scoring": sum(1 for r in rows if r["score"] > 0),
        },
        "recruiters": rows,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, OUT)
    try:
        os.chmod(OUT, 0o644)
    except OSError:
        pass
    print(f"wrote {OUT}  recruiters={len(rows)} pairs={len(pairs)} "
          f"scoring={doc['totals']['recruiters_scoring']} "
          f"burn={doc['burn_share']:.3f} payable={payable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
