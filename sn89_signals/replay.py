"""Replayable weight derivation — the trust anchor of the single-validator model.

SN89 runs ONE authoritative validator (others child-key/delegate to it), so there
is one journal and no multi-validator sync problem. Trust does not come from
redundancy — it comes from REPLAYABILITY: every signal is hash-committed on-chain,
graded from public inputs (drand + price + committed bands), so anyone can rebuild
the validator's weight vector from the published journal and confirm it was computed
honestly. This module is that rebuild, as a PURE function of the journal — no DB, no
chain, no network — so a miner or skeptic gets a byte-identical answer.

`weights_from_journal` re-derives EVERYTHING from the journal (eliminations, copy
flags, gate, tier, emission) using the same scoring functions the validator uses;
it does not trust the validator's claimed eliminations or copy marks. It must stay
in parity with `validator.maybe_set_weights` (the validator is being refactored to
call this directly; until then, mirror any change here).
"""
from __future__ import annotations

from . import config, scoring
from .schema import Signal


def weights_from_journal(
    signals: list[dict],
    meta: dict[str, dict],
    uid_by_hotkey: dict[str, int],
    now: float,
    referrals: list[dict] | None = None,
) -> dict[int, float]:
    """Re-derive {uid: weight} from a published journal, identically to the
    validator. PURE.

    signals: each {commit_hex, hotkey, t0_unix, status, plaintext}. `status` is the
             grade (won|lost|washed|void|pending|...); `plaintext` is the revealed
             signal (public after the 24h timelock) — required for copy detection.
    meta:    {hotkey: {first_seen_unix, strikes}}. Eliminations are NOT taken from
             here — they are re-derived from the decisive history below.
    uid_by_hotkey: hotkey → metagraph uid (the auditor reads the metagraph).
    referrals: journaled referral claims [{recruiter_hk, recruit_hk, commit_block,
             recruit_reg_block}] (§ referral). Validity and the pair no-copy gate
             are RE-DERIVED here (never trusted from the journal); inert unless
             config.REFERRAL_ENABLED. Default None → byte-identical to the
             pre-referral output.
    """
    # ── re-derive eliminations from the decisive history (don't trust the journal) ─
    decisive_by_hk: dict[str, list[tuple[float, bool, bool]]] = {}
    for s in signals:
        if s["status"] in ("won", "lost"):
            decisive_by_hk.setdefault(s["hotkey"], []).append(
                (s["t0_unix"], s["status"] == "won", bool(s.get("is_copy", 0))))
    eliminated_hk = {
        hk for hk, dec in decisive_by_hk.items()
        if scoring.elimination_t0([(t0, won) for t0, won, _ in dec]) is not None
    }

    # ── copy penalty / forensics (§7.5), re-derived over the copy window ──────────
    copy_rows: list[scoring.GradedRow] = []
    rows_by_commit: dict[str, scoring.GradedRow] = {}
    cutoff = now - config.COPY_WINDOW_S
    for s in signals:
        if s["status"] == "void" or not s.get("plaintext") or s["t0_unix"] < cutoff:
            continue
        sig = Signal.from_bytes(s["plaintext"].encode())
        gr = scoring.GradedRow(
            hotkey=s["hotkey"], trade_pair=sig.trade_pair, direction=sig.direction,
            t0_unix=s["t0_unix"], status=s["status"],
            horizon_h=config.horizon_h_for(sig.trade_pair, s["t0_unix"]))
        copy_rows.append(gr)
        rows_by_commit[s["commit_hex"]] = gr

    decisive_counts = {hk: len(d) for hk, d in decisive_by_hk.items()}
    both_dir = (scoring.both_direction_spammers(copy_rows)
                if config.COPY_EXCLUDE_BOTH_DIR else set())
    eligible_leaders = {
        hk for hk, n in decisive_counts.items()
        if n >= config.COPY_LEADER_MIN_DECISIVE
        and hk not in eliminated_hk and hk not in both_dir}

    scoring.mark_copies(copy_rows, eligible_leaders=eligible_leaders)  # sets gr.is_copy
    is_copy_by_commit = {c: gr.is_copy for c, gr in rows_by_commit.items()}

    reports = scoring.detect_copiers(copy_rows, now, eligible_leaders=eligible_leaders)
    # sharp 1:1 shadowing signature — now also gates the copied-win strip (§7.5)
    shadow_hk = scoring.flagged_copier_hotkeys(reports)
    flagged_hk = shadow_hk if config.COPY_ZERO_WEIGHT else set()
    excluded_uids = {uid_by_hotkey[h] for h in flagged_hk if h in uid_by_hotkey}

    # ── build MinerStates (skip struck + eliminated) and score ───────────────────
    states = []
    for hk, m in meta.items():
        uid = uid_by_hotkey.get(hk)
        if uid is None:
            continue
        if int(m.get("strikes") or 0) >= config.STRIKE_LIMIT:
            continue
        if hk in eliminated_hk:
            continue
        # decisive history with the FRESHLY re-derived is_copy (copy-window rows
        # override; outside-window rows keep their journaled is_copy).
        decisive = []
        for s in signals:
            if s["hotkey"] == hk and s["status"] in ("won", "lost"):
                cp = is_copy_by_commit.get(s["commit_hex"], bool(s.get("is_copy", 0)))
                decisive.append((s["t0_unix"], s["status"] == "won", bool(cp)))
        rep_won, rep_dec, won_all, won_orig, copies, td = scoring.score_inputs(
            decisive, m["first_seen_unix"], now)
        # copy penalty: shadow-gated, and a LOUD SHORT-LIVED warning — it bites only
        # while the last copy event is within COPY_PENALTY_TTL_S (default 2d).
        cw_copies, cw_dec, cw_last = scoring.copy_gate_inputs(decisive, now)
        habitual = (scoring.is_penalised_copier(cw_copies, cw_dec, hk in shadow_hk)
                    and cw_last is not None
                    and (now - cw_last) <= config.COPY_PENALTY_TTL_S)
        tw = won_orig if habitual else won_all
        # Full resolved history INCLUDING washes — the efficiency multiplier needs
        # what the decisive list throws away. void/pending are excluded: a void was
        # never a valid call, so counting it as a wash would penalise the miner for
        # our own rejection.
        graded = [(s["t0_unix"], s["status"] == "washed") for s in signals
                  if s["hotkey"] == hk and s["status"] in ("won", "lost", "washed")]
        # qualified post-warmup wins (point-in-time gate + tier x efficiency) — sizes emission.
        qwins = scoring.qualified_wins(decisive, m["first_seen_unix"], habitual,
                                       graded=graded)
        states.append(scoring.MinerState(
            hotkey=hk, uid=uid, first_seen_unix=m["first_seen_unix"],
            rep_wins=rep_won, rep_decisive=rep_dec, trailing_wins=tw, qwins=qwins))

    # ── referral pairs (§ referral): validity + pair no-copy, re-derived ─────────
    referral_pairs = None
    if config.REFERRAL_ENABLED and referrals:
        cand = scoring.valid_referral_pairs(referrals)
        ref_hks = {hk for pair in cand for hk in pair}
        pcut = now - config.REFERRAL_PAIR_WINDOW_S
        ref_rows: list[scoring.GradedRow] = []
        for s in signals:
            if (s["hotkey"] not in ref_hks or s["status"] == "void"
                    or not s.get("plaintext") or s["t0_unix"] < pcut):
                continue
            sig = Signal.from_bytes(s["plaintext"].encode())
            ref_rows.append(scoring.GradedRow(
                hotkey=s["hotkey"], trade_pair=sig.trade_pair, direction=sig.direction,
                t0_unix=s["t0_unix"], status=s["status"],
                horizon_h=config.horizon_h_for(sig.trade_pair, s["t0_unix"])))
        referral_pairs = [
            (recruiter, recruit) for recruiter, recruit in cand
            if scoring.referral_pair_suspended_until(
                ref_rows, recruiter, recruit, now) is None]

    return scoring.compute_weights(states, now, excluded_uids=excluded_uids,
                                   referral_pairs=referral_pairs)


def referrer_recruit_tallies(
    signals: list[dict],
    meta: dict[str, dict],
    now: float,
    hotkeys,
) -> dict[str, float]:
    """{hotkey: LF decayed qualified-win tally} for the given hotkeys.

    Split out of referrer_weights_from_journal so the LF leg of the referrer
    score has ONE definition — the publisher that shows a recruiter WHY they
    score needs the same per-recruit numbers, and a second copy of this loop is
    how the page and the payout drift apart.
    """
    # Bucket ONCE. The original inline version rescanned the whole journal twice
    # per hotkey, which was tolerable for a handful of recruits and is not when
    # the multicomp path needs the tally for the entire field every cycle.
    want = set(hotkeys)
    decisive_by: dict[str, list] = {}
    graded_by: dict[str, list] = {}
    for s in signals:
        hk = s["hotkey"]
        if hk not in want:
            continue
        st = s["status"]
        if st not in ("won", "lost", "washed"):
            continue
        graded_by.setdefault(hk, []).append((s["t0_unix"], st == "washed"))
        if st != "washed":
            decisive_by.setdefault(hk, []).append(
                (s["t0_unix"], st == "won", bool(s.get("is_copy", 0))))

    out: dict[str, float] = {}
    for hk, decisive in decisive_by.items():
        m = meta.get(hk)
        if m is None:
            continue
        qwins = scoring.qualified_wins(decisive, m["first_seen_unix"],
                                       habitual=False,
                                       graded=graded_by.get(hk))
        out[hk] = scoring.decayed_qwin_tally(qwins, now)
    return out


def referrer_weights_from_journal(
    signals: list[dict],
    meta: dict[str, dict],
    uid_by_hotkey: dict[str, int],
    now: float,
    referrals: list[dict] | None = None,
    referral_transfers: list[dict] | None = None,
    extra_tallies: dict[str, dict[str, float]] | None = None,
) -> dict[int, float]:
    """§ referrer mechanism (mecid 1) — PURE rebuild, the auditor's mirror of
    the validator's referrer vector. Pipeline:

        valid_referral_pairs (same gate as the in-band bonus era)
          → apply_referral_transfers (one-time sn89refx remaps)
          → recruit tallies (decayed qualified wins — the recruit's own
            emission currency, rebuilt from the journal)
          → referrer_scores → referrer_weights (cap + burn)

    Copy forensics and habitual-copier stripping are deliberately NOT applied
    to the recruit tallies here: the referrer score is a read on the recruit's
    RAW qualified performance, and re-running the copy pipeline per mechanism
    would double the heaviest part of replay for a second-order effect. If a
    copier gets zeroed on mecid-0, their tally still decays to nothing within
    the decay window — the referrer's score follows with the same lag.

    extra_tallies: {competition_key: {hotkey: raw tally}} for the competitions
    this module cannot rebuild from the signals journal — HF and Closers grade
    off the public HF base, not off `signals`. Required once
    config.referrer_multicomp_active(now); ignored before it, so a replay of a
    pre-flip block is byte-identical whether or not they are supplied.

    ⚠ An auditor replaying the multicomp era from the journal ALONE cannot
    reproduce this vector — they need the public HF/Closers logs too. That is
    the cost of paying recruiters for what their recruits actually earn rather
    than for the third of it that lives in this file. The inputs are public;
    the rebuild is just wider.
    """
    pairs = scoring.valid_referral_pairs(referrals or [])
    pairs = scoring.apply_referral_transfers(pairs, referral_transfers or [])
    if not pairs:
        return {config.BURN_UID: 1.0}

    recruit_hks = {recruit for _, recruit in pairs}
    lf_tally = referrer_recruit_tallies(signals, meta, now, recruit_hks)

    if not config.referrer_multicomp_active(now):
        scores = scoring.referrer_scores(pairs, lf_tally)
        return scoring.referrer_weights(scores, uid_by_hotkey)

    # Multi-competition era. Each competition is normalized over its OWN FULL
    # field, not over the recruits — a recruit's credit has to mean "this much
    # of that competition", so the denominator is every participant in it.
    # The LF field is therefore rebuilt for all hotkeys with journal history,
    # not only for recruits.
    lf_field = referrer_recruit_tallies(signals, meta, now, set(meta))
    tallies = {"lf": lf_field}
    for comp, field in (extra_tallies or {}).items():
        tallies[comp] = field
    shares = scoring.referrer_shares(config.comp_weights_as_of(now))
    blended = scoring.blended_recruit_tallies(tallies, shares)
    scores = scoring.referrer_scores(pairs, blended)
    return scoring.referrer_weights(scores, uid_by_hotkey)
