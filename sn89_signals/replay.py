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
                # 4th element = journaled resolution time (exit_at_ms -> seconds).
                # qualified_wins needs it to build a CAUSAL as-of window; without
                # it every row falls back to legacy treatment. None is honest here
                # and is handled as "unknown" rather than guessed either way.
                _ex = s.get("exit_at_ms")
                decisive.append((s["t0_unix"], s["status"] == "won", bool(cp),
                                 (float(_ex) / 1000.0) if _ex else None))
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
    # The pair stays; the no-copy gate names the SIDE that is shadowing and only
    # that side's bonus is withheld (scoring.referral_pair_followers). Dropping
    # the pair outright — what this did before 2026-08-24 — made the copied-from
    # side pay for the copier.
    referral_pairs = None
    referral_suspended: set[str] = set()
    if config.REFERRAL_ENABLED and referrals:
        referral_pairs = scoring.valid_referral_pairs(referrals)
        ref_rows = _referral_pair_rows(signals, referral_pairs, now)
        for recruiter, recruit in referral_pairs:
            referral_suspended |= set(scoring.referral_pair_followers(
                ref_rows, recruiter, recruit, now))

    return scoring.compute_weights(states, now, excluded_uids=excluded_uids,
                                   referral_pairs=referral_pairs,
                                   referral_suspended=referral_suspended,
                                   # LF folds into the points scheme later; until it
                                   # does, arming points must not zero its field.
                                   use_points=False)


def referrer_withheld_recruits(signals: list[dict],
                               orig_pairs: list[tuple[str, str]],
                               remapped_pairs: list[tuple[str, str]],
                               now: float) -> set[str]:
    """Recruits whose pair pays their recruiter NOTHING on mecid-1 this cycle,
    because that recruiter is the side shadowing the pair.

    Keyed by recruit because a recruit belongs to exactly one recruiter
    (valid_referral_pairs), so the recruit alone identifies the pair.

    ONE definition, shared by referrer_weights_from_journal and
    tools/publish_referrer_standing.py — the publisher refuses to write when
    its arithmetic disagrees with the replay vector, and two copies of this
    rule is how the page and the payout drift apart.

    orig_pairs is pre-transfer, remapped_pairs post-transfer. The gate is
    judged on the ORIGINAL pair (it measures who traded on top of whom, which a
    transfer does not change) but applied only while the follower is still the
    credited recruiter — a recruiter cannot launder the penalty onto a
    destination that never traded alongside those recruits, and a destination
    does not inherit it.
    """
    ref_rows = _referral_pair_rows(signals, orig_pairs, now)
    followed = {
        recruit for recruiter, recruit in orig_pairs
        if recruiter in scoring.referral_pair_followers(
            ref_rows, recruiter, recruit, now)
    }
    orig_recruiter = {recruit: recruiter for recruiter, recruit in orig_pairs}
    return followed & {recruit for recruiter, recruit in remapped_pairs
                       if orig_recruiter.get(recruit) == recruiter}


def _referral_pair_rows(signals: list[dict], pairs: list[tuple[str, str]],
                        now: float) -> list["scoring.GradedRow"]:
    """GradedRows for every hotkey in `pairs`, inside the pair-gate window.

    Shared by the two weight paths so mecid-0 and mecid-1 can never judge the
    same pair off different evidence. Needs `plaintext` (the gate keys on
    trade_pair + direction); a journal row without it is skipped, which is the
    pre-reveal case and correctly contributes no copy evidence.
    """
    hks = {hk for pair in pairs for hk in pair}
    pcut = now - config.REFERRAL_PAIR_WINDOW_S
    rows: list[scoring.GradedRow] = []
    for s in signals:
        if (s["hotkey"] not in hks or s["status"] == "void"
                or not s.get("plaintext") or s["t0_unix"] < pcut):
            continue
        sig = Signal.from_bytes(s["plaintext"].encode())
        rows.append(scoring.GradedRow(
            hotkey=s["hotkey"], trade_pair=sig.trade_pair, direction=sig.direction,
            t0_unix=s["t0_unix"], status=s["status"],
            horizon_h=config.horizon_h_for(sig.trade_pair, s["t0_unix"])))
    return rows


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
            _ex = s.get("exit_at_ms")          # causal as-of window, see qualified_wins
            decisive_by.setdefault(hk, []).append(
                (s["t0_unix"], st == "won", bool(s.get("is_copy", 0)),
                 (float(_ex) / 1000.0) if _ex else None))

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
          → referral_pair_followers (pair no-copy gate, RECRUITER side only —
            a recruiter shadowing its own recruit forfeits that pair)
          → recruit tallies (decayed qualified wins — the recruit's own
            emission currency, rebuilt from the journal)
          → referrer_scores → referrer_weights (cap + burn)

    GLOBAL copy forensics and habitual-copier stripping are deliberately NOT
    applied to the recruit tallies here: the referrer score is a read on the
    recruit's RAW qualified performance, and re-running the §7.5 pipeline per
    mechanism would double the heaviest part of replay for a second-order
    effect. If a copier gets zeroed on mecid-0, their tally still decays to
    nothing within the decay window — the referrer's score follows with the
    same lag. The PAIR gate is different and is applied: it is scoped to the
    two hotkeys of one pair, so it costs nothing to run, and mecid-1 is the
    only place a recruiter can be charged for its own copying at all.

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
    orig = scoring.valid_referral_pairs(referrals or [])
    pairs = scoring.apply_referral_transfers(orig, referral_transfers or [])
    if not pairs:
        return {config.BURN_UID: 1.0}

    # Pair no-copy gate, recruiter side. mecid-1 is the ONLY referrer bonus that
    # still pays (the in-band 20% retired 2026-08-03), so this is where a
    # recruiter who shadows its own recruit forfeits that pair — and it forfeits
    # nothing else: the recruiter's mecid-0 emission, the recruit's emission and
    # the recruit's +10% are all untouched, and the pair resumes when the gate
    # self-clears. Judged on the ORIGINAL pair, because the gate measures two
    # accounts trading on top of each other and a transfer does not change who
    # traded; withheld only while the follower is STILL the credited recruiter,
    # so a transfer hands the destination a clean pair rather than the penalty.
    withheld = referrer_withheld_recruits(signals, orig, pairs, now)

    recruit_hks = {recruit for _, recruit in pairs}
    lf_tally = referrer_recruit_tallies(signals, meta, now, recruit_hks)

    if not config.referrer_multicomp_active(now):
        scores = scoring.referrer_scores(pairs, lf_tally, withheld_recruits=withheld)
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
    scores = scoring.referrer_scores(pairs, blended, withheld_recruits=withheld)
    return scoring.referrer_weights(scores, uid_by_hotkey)


def combined_weights_from_journal(
    signals: list[dict],
    meta: dict[str, dict],
    uid_by_hotkey: dict[str, int],
    now: float,
    referrals: list[dict] | None = None,
    hf_base: str | None = None,
    cache_dir: str | None = None,
    on_error=None,
) -> tuple[dict[int, float], dict]:
    """The FULL mecid-0 vector — LF + HF + Closers blended by COMP_WEIGHTS.

    `weights_from_journal` above returns the LF competition alone. Since the
    2026-08-03 combined-weights cutover that is NOT what goes on chain, so an
    auditor comparing it against the metagraph saw every uid off by a constant
    1/COMP_WEIGHTS["lf"] and HF/Closers-only earners reading replay=0.000. The
    tool reported "AUDIT FAILED — the validator's record does not match the
    chain", which to an outsider is indistinguishable from proof that we cook
    the books. The journal was honest the whole time; the comparison was not
    whole.

    This exists so there is ONE implementation of the blend. The validator, the
    auditor and any future follower must all call this — three hand-copies of a
    twenty-line blend is precisely how a consensus path drifts.

    Every input is PUBLIC and needs no validator role:
      * LF      — the published checkpoint journal (this module).
      * HF      — hf_grade.mecid1_weights, graded from the published anchored
                  windows at hf.HF_PUBLIC_BASE.
      * Closers — closers.closers_weights, same public window logs.
      * shares  — config.comp_weights_as_of, a committed consensus constant.

    A competition that cannot be computed contributes None and BURNS its share
    (competitions.combine's dead-share rule) — it is never redistributed to the
    others. That mirrors the validator exactly: a dead feed must cost its own
    share rather than silently inflate everyone else.

    Returns (weights, detail). `detail` carries the per-competition vectors, the
    shares in force, and any errors, so a caller can say WHICH competition
    diverged instead of only that the total did.
    """
    from . import closers as _closers          # local: keeps import order free
    from . import competitions as _competitions
    from . import hf_grade as _hf_grade

    lf = weights_from_journal(signals, meta, uid_by_hotkey, now,
                              referrals=referrals)
    detail: dict = {"lf": lf, "hf": None, "closers": None,
                    "shares": None, "combined": False, "errors": {}}

    if not config.combined_weights_active(now):
        # Pre-cutover replays are LF-only ON CHAIN too, so this is not a
        # degraded answer -- it is the correct vector for that instant.
        detail["shares"] = {"lf": 1.0}
        return lf, detail

    def _try(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — a dead competition burns its share
            detail["errors"][name] = f"{type(e).__name__}: {e}"
            if on_error:
                on_error(name, e)
            return None

    vectors: dict = {"lf": lf}
    vectors["hf"] = _try("hf", lambda: _hf_grade.mecid1_weights(
        uid_by_hotkey, now, base=hf_base, cache_dir=cache_dir))

    # "Qualified in HF or LF" = currently earning weight in either vector,
    # excluding burn. Built from the two vectors ABOVE and never from the blend:
    # reading the combined vector would rank a different field than the one that
    # actually got paid.
    hk_by_uid = {u: h for h, u in uid_by_hotkey.items()}
    qual = {hk_by_uid[u]
            for vec in (lf, vectors.get("hf") or {})
            for u, wt in vec.items()
            if wt > 0 and u != config.BURN_UID and u in hk_by_uid}
    vectors["closers"] = _try("closers", lambda: _closers.closers_weights(
        uid_by_hotkey, now, base=hf_base, cache_dir=cache_dir,
        qualified_hks=qual))

    shares = config.comp_weights_as_of(now)
    detail.update({"hf": vectors["hf"], "closers": vectors["closers"],
                   "shares": shares, "combined": True, "qualified_n": len(qual)})
    return _competitions.combine(vectors, shares), detail
