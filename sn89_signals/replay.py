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
) -> dict[int, float]:
    """Re-derive {uid: weight} from a published journal, identically to the
    validator. PURE.

    signals: each {commit_hex, hotkey, t0_unix, status, plaintext}. `status` is the
             grade (won|lost|washed|void|pending|...); `plaintext` is the revealed
             signal (public after the 24h timelock) — required for copy detection.
    meta:    {hotkey: {first_seen_unix, strikes}}. Eliminations are NOT taken from
             here — they are re-derived from the decisive history below.
    uid_by_hotkey: hotkey → metagraph uid (the auditor reads the metagraph).
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
        # qualified post-warmup wins (point-in-time gate + tier) — sizes emission.
        qwins = scoring.qualified_wins(decisive, m["first_seen_unix"], habitual)
        states.append(scoring.MinerState(
            hotkey=hk, uid=uid, first_seen_unix=m["first_seen_unix"],
            rep_wins=rep_won, rep_decisive=rep_dec, trailing_wins=tw, qwins=qwins))

    return scoring.compute_weights(states, now, excluded_uids=excluded_uids)
