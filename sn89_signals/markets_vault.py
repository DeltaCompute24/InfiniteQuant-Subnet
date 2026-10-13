"""SN89 Markets V5: the collateral vault (Whit, 2026-10-12; SN89-PREDICTION-MARKETS-SPEC.md "V5 collateral vault").

Until V5 an entity's collateral was the stake its owner coldkey held on its own hotkey, which it could
unstake at any time, so every lock was only as strong as the emission it could withhold. From the arm,
collateral is alpha the entity has transferred to the vault coldkey (config.MARKETS_VAULT_COLDKEY), staked
on the vault hotkey (config.MARKETS_VAULT_HOTKEY). The vault is custodial: the owner can move it. What V5
guarantees is that every movement is replayable and checked.

  deposit   the entity's owner coldkey `transfer_stake`s alpha to the vault coldkey on the vault hotkey,
            netuid = config.NETUID, then files a signed `mk.deposit {amount_alpha, block, ext_index}`.
            Validators read that extrinsic: a StakeTransferred from the entity's owner (at that block) to
            the vault coldkey, same netuid both sides, on the vault hotkey, and the StakeAdded to the vault
            that carries the alpha. Credit = min(claimed, StakeAdded alpha), once per extrinsic (the first
            claim naming it in canonical order), at the claim's receipt time.
  units     balances are kept in vault-hotkey SHARES: units = alpha / share_price(block). The share
            price rises with the dividends the vault hotkey's stakers earn, so the vault's staking yield
            reaches every entity pro rata to its balance, with no separate distribution step.
  locked    the entity's net loss below its high-water mark, in alpha: max(0, hwm - rcum) from its last
            settle row closed by t (0 after an exit, see below).
  collateral at t = units before t x share price at the hour block - locked. A bet must fit it with the
            entity's open bets (markets.collateral_pass), and the V2d lock checks it at every checkpoint.
  withdraw  `mk.withdraw {amount_alpha}`: approved at the first checkpoint (MARKETS_LOCK_CHECK_S grid) at
            or after receipt, for min(amount, collateral - open bets), and debited then. The owner sends it
            with transfer_stake (vault -> the entity's owner coldkey, vault hotkey) and an operator
            (config.MARKETS_VAULT_OPERATORS) files `mk.release {withdraw_key, block, ext_index}`; validators
            verify the transfer. Unreleased past MARKETS_VAULT_RELEASE_DEADLINE_S = a recorded default.
  exit      `mk.exit`: everything available is approved as a withdrawal and the locked amount moves to the
            reserve; the entity's carry (rcum, hwm) restarts at zero for cycles after the one closed at
            the checkpoint.
  reserve   ledger entity RESERVE. At each closed cycle it pays the cycle's claims (payable + owed) pro
            rata up to its balance, as credits to the claimants' vault balances at the cycle's close
            (c + period + grace); the emission then pays what is left (V2e). See markets.settle_cycles.
  emission  (V5b) from the arm the Markets share of the weight vector goes to the vault's own UID
            (config.MARKETS_VAULT_UID_HOTKEY, owned by the vault coldkey), so the emission lands in the vault
            on chain. An operator sweeps it onto the vault hotkey (move_stake) and files `mk.sweep {block,
            ext_index}`; validators verify the StakeMoved and credit the alpha the vault hotkey received to
            the entities pro rata to the emission they are owed and not yet credited (settle.paid summed
            over armed cycles, minus earlier sweep credits). Anything above what is owed goes to the reserve.
            Credit follows what ARRIVED, not what the vector asked for: validators that set other weights
            leave part of it unpaid, and that part stays outstanding (visible) instead of being invented.
  payouts   (V5b) the vault is also the float. When a cycle closes with an entity's players net ahead
            (won_ret - lost_ret > 0, alpha), that amount is debited from the entity's vault balance at the
            close and becomes an approved `mk.payout` request: the custodian sends it to the entity's owner
            coldkey (which pays the players) and files mk.release like any withdrawal. A cycle where the
            players net lost moves nothing: the lost stakes are with the entity's custodian, which may
            deposit them. A debit is capped at the balance; the uncovered part is recorded, not owed.
  solvency  at each checkpoint the vault coldkey's on-chain stake on the vault hotkey is compared with the
            ledger (all balances + reserve + approved, unreleased withdrawals). Short by more than
            MARKETS_VAULT_SOLVENCY_TOL -> no bet is accepted until a later checkpoint is whole.

Determinism. Every value is a function of published receipts and chain state at named blocks. The ledger
advances one checkpoint at a time; a checkpoint is processed only when the settlement cycle closed by it is
settled, and settlement of cycle c waits for every checkpoint before its close, so a rebuild from scratch
writes the same rows in the same order.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

from . import config, hf

KIND_DEPOSIT, KIND_WITHDRAW, KIND_EXIT, KIND_RELEASE = "mk.deposit", "mk.withdraw", "mk.exit", "mk.release"
KIND_SWEEP, KIND_PAYOUT = "mk.sweep", "mk.payout"      # mk.payout is a request the ledger writes, never a frame
VAULT_KINDS = (KIND_DEPOSIT, KIND_WITHDRAW, KIND_EXIT, KIND_RELEASE, KIND_SWEEP)
RESERVE = "_reserve"
Z = Decimal(0)


def _m():
    from . import markets
    return markets


def schema(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS vault_claims (key TEXT PRIMARY KEY, kind TEXT, hk TEXT, amount TEXT, "
              "block INTEGER, ext_index INTEGER, ref TEXT, w INTEGER, t_recv_us INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS vault_ledger (key TEXT PRIMARY KEY, t_us INTEGER, entity TEXT, units TEXT, "
              "alpha TEXT, kind TEXT, ref TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS vault_ledger_ent ON vault_ledger(entity, t_us)")
    c.execute("CREATE TABLE IF NOT EXISTS vault_requests (key TEXT PRIMARY KEY, entity TEXT, kind TEXT, requested TEXT, "
              "cp INTEGER, approved TEXT, status TEXT, release_key TEXT, released_block INTEGER, released_alpha TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS vault_exits (entity TEXT, cp INTEGER, after_cycle INTEGER, forfeited TEXT, "
              "PRIMARY KEY (entity, cp))")
    c.execute("CREATE TABLE IF NOT EXISTS vault_checks (cp INTEGER PRIMARY KEY, chain TEXT, ledger TEXT, short INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS vault_credit (cp INTEGER PRIMARY KEY)")


VAULT_TABLES = ("vault_claims", "vault_ledger", "vault_requests", "vault_exits", "vault_checks", "vault_credit")


# ── ingest ────────────────────────────────────────────────────────────────────
def validate(payload: dict, signer_hk: str, t_unix: float) -> None:
    """Shape and authority of a vault frame. Amounts and extrinsics are checked against the chain later."""
    m = _m()
    if not config.markets_vault_open_as_of(t_unix):
        raise hf.HFRejected("markets_vault_not_open")
    kind = str(payload.get("kind", ""))
    try:
        if str(payload.get("trade_pair", "")).upper() != config.MARKETS_RATE_PAIR:
            raise m.MarketError(f"trade_pair_mismatch:expected {config.MARKETS_RATE_PAIR}")
        if kind in (KIND_DEPOSIT, KIND_WITHDRAW):
            m.parse_alpha(payload.get("amount_alpha"))
        if kind in (KIND_DEPOSIT, KIND_RELEASE, KIND_SWEEP):
            blk, idx = int(payload.get("block")), int(payload.get("ext_index"))
            if blk <= 0 or idx < 0:
                raise m.MarketError("bad_extrinsic_ref")
        if kind in (KIND_RELEASE, KIND_SWEEP):
            if signer_hk not in config.MARKETS_VAULT_OPERATORS:
                raise m.MarketError(f"{kind.split('.')[1]}_not_operator")
            if kind == KIND_RELEASE and not str(payload.get("withdraw_key") or ""):
                raise m.MarketError("release_needs_withdraw_key")
    except (TypeError, ValueError) as e:
        raise hf.HFRejected(str(e) if isinstance(e, m.MarketError) else "bad_extrinsic_ref")


def ingest(db: sqlite3.Connection, w: int, hk: str, seq: int, t_us: int, p: dict) -> None:
    kind = str(p["kind"])
    amount = str(_m().parse_alpha(p["amount_alpha"])) if kind in (KIND_DEPOSIT, KIND_WITHDRAW) else None
    blk = int(p["block"]) if kind in (KIND_DEPOSIT, KIND_RELEASE, KIND_SWEEP) else None
    idx = int(p["ext_index"]) if kind in (KIND_DEPOSIT, KIND_RELEASE, KIND_SWEEP) else None
    ref = str(p["withdraw_key"]) if kind == KIND_RELEASE else None
    db.execute("INSERT OR IGNORE INTO vault_claims VALUES (?,?,?,?,?,?,?,?,?)",
               (f"{hk}:{seq}", kind, hk, amount, blk, idx, ref, int(w), int(t_us)))


# ── ledger reads ──────────────────────────────────────────────────────────────
def units_before(db: sqlite3.Connection, entity: str, t_us: int) -> Decimal:
    """Units held by `entity` from ledger rows strictly before t_us."""
    return sum((Decimal(u) for (u,) in db.execute("SELECT units FROM vault_ledger WHERE entity=? AND t_us < ?",
                                                    (entity, int(t_us)))), Z)


def ledger_alpha_before(db: sqlite3.Connection, entity: str, t_us: int) -> Decimal:
    """Alpha posted to `entity` by ledger rows strictly before t_us, at the values they were posted at (no chain
    read, so anything priced from it replays from the ledger alone)."""
    return sum((Decimal(a) for (a,) in db.execute("SELECT alpha FROM vault_ledger WHERE entity=? AND t_us < ?",
                                                    (entity, int(t_us)))), Z)


def price_at(ch, block: int) -> Decimal | None:
    return ch.share_price(config.MARKETS_VAULT_HOTKEY, block)


def _exit_after(db: sqlite3.Connection, entity: str, t: int) -> int | None:
    row = db.execute("SELECT MAX(after_cycle) FROM vault_exits WHERE entity=? AND cp <= ?", (entity, int(t))).fetchone()
    return row[0] if row and row[0] is not None else None


def locked(db: sqlite3.Connection, entity: str, t: int) -> Decimal:
    """Net loss below the high-water mark (alpha) as of the last cycle closed by t; 0 for cycles at or
    before an exit, whose hole was forfeited to the reserve."""
    m = _m()
    closed = m._closed_by(int(t))
    row = db.execute("SELECT cycle, rcum, hwm FROM settle WHERE entity=? AND cycle <= ? ORDER BY cycle DESC LIMIT 1",
                     (entity, closed)).fetchone()
    if row is None:
        return Z
    ex = _exit_after(db, entity, t)
    if ex is not None and row[0] <= ex:
        return Z
    return max(Z, Decimal(row[2]) - Decimal(row[1]))


def collateral_at(db: sqlite3.Connection, ch, entity: str, t_us: int, block: int) -> Decimal | None:
    """Vault collateral for a bet (or a lock checkpoint) at t_us, priced at `block`. None = wait."""
    p = price_at(ch, block)
    if p is None:
        return Z if units_before(db, entity, t_us) == 0 else None
    bal = _m().CTX.multiply(units_before(db, entity, t_us), p)
    return bal - locked(db, entity, int(t_us) // 1_000_000)


def short_at(db: sqlite3.Connection, t: int) -> bool:
    """The vault failed its last solvency check before t."""
    row = db.execute("SELECT short FROM vault_checks WHERE cp < ? ORDER BY cp DESC LIMIT 1", (int(t),)).fetchone()
    return bool(row and row[0])


def cursor(db: sqlite3.Connection) -> int | None:
    row = db.execute("SELECT MAX(cp) FROM vault_checks").fetchone()
    return row[0] if row and row[0] is not None else None


def ready_for(db: sqlite3.Connection, t: int) -> bool:
    """Every deposit and release received at or before t has been applied (phase A of the checkpoint at or
    after t is done); withdrawals of earlier checkpoints are, because phase A waits for them."""
    step = config.MARKETS_LOCK_CHECK_S
    row = db.execute("SELECT MAX(cp) FROM vault_credit").fetchone()
    return bool(row and row[0] is not None and row[0] >= -(-int(t) // step) * step)


def checkpoints_done_before(db: sqlite3.Connection, t: int) -> bool:
    """Every checkpoint strictly before t is done (settlement of the cycle closing at t waits for this)."""
    if not config.MARKETS_VAULT_OPEN_UNIX:
        return True
    step = config.MARKETS_LOCK_CHECK_S
    last = (int(t) - 1) // step * step
    if last < _first_cp():
        return True
    cur = cursor(db)
    return cur is not None and cur >= last


def _first_cp() -> int:
    step = config.MARKETS_LOCK_CHECK_S
    return -(-config.MARKETS_VAULT_OPEN_UNIX // step) * step


# ── the pass ──────────────────────────────────────────────────────────────────
def _canonical(rows):
    return sorted(rows, key=lambda r: (r["w"], r["t_recv_us"], r["key"]))


def _claims(db, kind, lo_us, hi_us):
    cols = ("key", "kind", "hk", "amount", "block", "ext_index", "ref", "w", "t_recv_us")
    return _canonical([dict(zip(cols, r)) for r in db.execute(
        "SELECT key, kind, hk, amount, block, ext_index, ref, w, t_recv_us FROM vault_claims "
        "WHERE kind=? AND t_recv_us > ? AND t_recv_us <= ?", (kind, int(lo_us), int(hi_us)))])


def _post(db, key, t_us, entity, units, alpha, kind, ref):
    db.execute("INSERT OR IGNORE INTO vault_ledger VALUES (?,?,?,?,?,?,?)",
               (key, int(t_us), entity, str(units), str(alpha), kind, ref))


def _xfer_alpha(ch, block, idx, origin_ck, dest_ck) -> Decimal:
    """Alpha a transfer_stake in (block, idx) moved from origin_ck to dest_ck on the vault hotkey and
    this subnet, as received by dest_ck (its StakeAdded). 0 when the extrinsic is not that transfer."""
    evs = ch.transfer_events(int(block), int(idx))
    hk, net = config.MARKETS_VAULT_HOTKEY, int(config.NETUID)
    moved = any(e["event"] == "StakeTransferred" and e["origin_coldkey"] == origin_ck
                and e["destination_coldkey"] == dest_ck and e["hotkey"] == hk
                and int(e["origin_netuid"]) == net and int(e["destination_netuid"]) == net for e in evs)
    if not moved:
        return Z
    return sum((e["alpha"] for e in evs if e["event"] == "StakeAdded" and e["coldkey"] == dest_ck
                and e["hotkey"] == hk and int(e["netuid"]) == net), Z)


def _sweep_alpha(ch, block, idx) -> Decimal:
    """Alpha a move_stake in (block, idx) moved, for the vault coldkey, from the vault UID hotkey onto the
    vault hotkey on this subnet (the StakeAdded the vault hotkey received). 0 when it is not that move."""
    V, src, dst, net = (config.MARKETS_VAULT_COLDKEY, config.MARKETS_VAULT_UID_HOTKEY, config.MARKETS_VAULT_HOTKEY,
                        int(config.NETUID))
    evs = ch.transfer_events(int(block), int(idx))
    moved = any(e["event"] == "StakeMoved" and e["coldkey"] == V and e["origin_hotkey"] == src
                and e["destination_hotkey"] == dst and int(e["origin_netuid"]) == net
                and int(e["destination_netuid"]) == net for e in evs)
    if not src or not moved:
        return Z
    return sum((e["alpha"] for e in evs if e["event"] == "StakeAdded" and e["coldkey"] == V
                and e["hotkey"] == dst and int(e["netuid"]) == net), Z)


def emission_outstanding(db: sqlite3.Connection, through_cycle: int, before_us: int) -> dict[str, Decimal]:
    """entity -> emission it was paid (settle.paid) over armed cycles up to `through_cycle`, minus sweep
    credits posted before `before_us`. Never negative."""
    out: dict[str, Decimal] = {}
    if not config.MARKETS_VAULT_FROM_UNIX:
        return out
    for ent, pd in db.execute("SELECT entity, paid FROM settle WHERE cycle >= ? AND cycle <= ? AND paid IS NOT NULL",
                              (config.MARKETS_VAULT_FROM_UNIX, int(through_cycle))):
        out[ent] = out.get(ent, Z) + Decimal(pd)
    for ent, a in db.execute("SELECT entity, alpha FROM vault_ledger WHERE kind='emission' AND entity != ? AND t_us < ?",
                             (RESERVE, int(before_us))):
        out[ent] = out.get(ent, Z) - Decimal(a)
    return {e: v for e, v in out.items() if v >= _m().RAO}         # sub-rao rounding is not owed


def _credit_sweeps(db, ch, cp: int) -> None:
    """Phase B: sweeps received in (cp - step, cp], credited at cp pro rata to outstanding emission."""
    m = _m()
    step = config.MARKETS_LOCK_CHECK_S
    lo_us, hi_us = (cp - step) * 1_000_000, cp * 1_000_000
    for c in _claims(db, KIND_SWEEP, lo_us, hi_us):
        ref = f"{c['block']}:{c['ext_index']}"
        if db.execute("SELECT 1 FROM vault_ledger WHERE kind='emission' AND ref=?", (ref,)).fetchone():
            continue                                          # once per extrinsic
        t = c["t_recv_us"] // 1_000_000
        if not (t - config.MARKETS_VAULT_CLAIM_MAX_AGE_S <= ch.block_time(int(c["block"])) <= t):
            continue
        got = _sweep_alpha(ch, c["block"], c["ext_index"])
        p = price_at(ch, int(c["block"]))
        if got <= 0 or p is None:
            continue
        owed = emission_outstanding(db, m._closed_by(cp), hi_us)
        tot = sum(owed.values(), Z)
        scale = min(Decimal(1), m.CTX.divide(got, tot)) if tot > 0 else Z
        left = got
        for ent in sorted(owed):
            a = (owed[ent] * scale).quantize(m.RAO, rounding="ROUND_DOWN")
            if a <= 0:
                continue
            _post(db, f"em:{c['key']}:{ent}", hi_us, ent, m.CTX.divide(a, p), a, "emission", ref)
            left -= a
        if left > 0 and scale >= 1:                           # more arrived than is owed: the reserve keeps it
            _post(db, f"em:{c['key']}:{RESERVE}", hi_us, RESERVE, m.CTX.divide(left, p), left, "emission", ref)


def payout_debit(db: sqlite3.Connection, ch, cycle: int, nets: dict[str, Decimal]) -> dict[str, Decimal]:
    """V5b: debit each entity whose players finished `cycle` net ahead (nets: entity -> won_ret - lost_ret,
    alpha) at the cycle's close, capped at its balance, and open an approved mk.payout request for the
    custodian to send. Returns entity -> alpha debited."""
    if not config.markets_vault_as_of(cycle):
        return {}
    m = _m()
    tc = m._cycle_end(cycle) + config.MARKETS_SETTLE_GRACE_S
    blk = ch.block_at(tc)
    price = price_at(ch, blk) if blk is not None else None
    if price is None:
        return {}
    t_us = tc * 1_000_000
    out = {}
    for ent in sorted(nets):
        net = nets[ent].quantize(m.RAO, rounding="ROUND_DOWN")
        if net <= 0:
            continue
        bal = m.CTX.multiply(units_before(db, ent, t_us + 1), price)
        a = min(net, max(bal, Z)).quantize(m.RAO, rounding="ROUND_DOWN")
        key = f"payout:{cycle}:{ent}"
        if a > 0:
            _post(db, key, t_us, ent, -m.CTX.divide(a, price), -a, "payout", str(cycle))
        db.execute("INSERT OR IGNORE INTO vault_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (key, ent, KIND_PAYOUT, str(net), tc, str(a), "approved" if a > 0 else "uncovered",
                    None, None, None))
        out[ent] = a
    return out


def _settled_through(db) -> int | None:
    row = db.execute("SELECT MAX(cycle) FROM settle_meta").fetchone()
    return row[0] if row and row[0] is not None else None


def vault_pass(db: sqlite3.Connection, ch, upto_s: int) -> int:
    """Advance the ledger one checkpoint at a time up to `upto_s` (receipts final through it).

    Each checkpoint cp has two phases. A: deposits and releases received in (cp - step, cp], credited at
    their receipt time; bets received up to cp can then be decided. B: once every bet received at or before
    cp is decided, withdrawals and exits received in that span are approved (debited at cp), defaults are
    recorded and solvency is checked. A of the next checkpoint waits for B. Returns phases done."""
    if not config.MARKETS_VAULT_OPEN_UNIX or not config.MARKETS_VAULT_COLDKEY or not config.MARKETS_VAULT_HOTKEY:
        return 0
    step = config.MARKETS_LOCK_CHECK_S
    done = 0
    while True:
        cur = cursor(db)
        cp = _first_cp() if cur is None else cur + step
        if cp > int(upto_s):
            return done
        blk = ch.block_at(cp)
        if blk is None:
            return done
        if not db.execute("SELECT 1 FROM vault_credit WHERE cp=?", (cp,)).fetchone():
            _phase_a(db, ch, cp)
            db.execute("INSERT INTO vault_credit VALUES (?)", (cp,))
            done += 1
        if not _phase_b_ready(db, cp):
            return done
        _phase_b(db, ch, cp, blk)
        done += 1


def _phase_a(db, ch, cp: int) -> None:
    m = _m()
    step, V = config.MARKETS_LOCK_CHECK_S, config.MARKETS_VAULT_COLDKEY
    lo_us, hi_us = (cp - step) * 1_000_000, cp * 1_000_000
    for c in _claims(db, KIND_DEPOSIT, lo_us, hi_us):           # credited at receipt, once per extrinsic
        if db.execute("SELECT 1 FROM vault_ledger WHERE kind='deposit' AND ref=?",
                      (f"{c['block']}:{c['ext_index']}",)).fetchone():
            continue
        t = c["t_recv_us"] // 1_000_000
        bt = ch.block_time(int(c["block"]))
        if not (t - config.MARKETS_VAULT_CLAIM_MAX_AGE_S <= bt <= t):
            continue
        got = _xfer_alpha(ch, c["block"], c["ext_index"], ch.hotkey_owner(c["hk"], int(c["block"])), V)
        p_dep = price_at(ch, int(c["block"]))
        credit = min(Decimal(c["amount"]), got)
        if credit <= 0 or p_dep is None:
            continue
        _post(db, f"dep:{c['key']}", c["t_recv_us"], c["hk"], m.CTX.divide(credit, p_dep), credit, "deposit",
              f"{c['block']}:{c['ext_index']}")
    for c in _claims(db, KIND_RELEASE, lo_us, hi_us):           # releases of approved withdrawals
        req = db.execute("SELECT entity, cp, status FROM vault_requests WHERE key=?", (c["ref"],)).fetchone()
        if req is None or req[2] not in ("approved", "default"):
            continue
        if db.execute("SELECT 1 FROM vault_requests r JOIN vault_claims k ON k.key = r.release_key "
                      "WHERE k.block=? AND k.ext_index=?", (c["block"], c["ext_index"])).fetchone():
            continue                                         # that extrinsic already released another request
        bt = ch.block_time(int(c["block"]))
        if not (req[1] <= bt <= c["t_recv_us"] // 1_000_000):
            continue
        got = _xfer_alpha(ch, c["block"], c["ext_index"], V, ch.hotkey_owner(req[0], int(c["block"])))
        if got <= 0:
            continue
        db.execute("UPDATE vault_requests SET status='released', release_key=?, released_block=?, released_alpha=? "
                   "WHERE key=?", (c["key"], int(c["block"]), str(got), c["ref"]))


def _phase_b_ready(db, cp: int) -> bool:
    m = _m()
    need = m._closed_by(cp)
    if need >= config.MARKETS_COLLATERAL_FROM_UNIX:
        st = _settled_through(db)
        if st is None or st < need:
            return False                                     # the cycle this checkpoint reads is not settled
    und = db.execute("SELECT b.market_id FROM bets b LEFT JOIN coll c ON c.key = b.key "
                     "WHERE c.key IS NULL AND b.t_recv_us <= ?", (cp * 1_000_000,)).fetchall()
    return not any(m.is_v2(m.parse_market_id(mid)[2]) for (mid,) in und)


def _phase_b(db, ch, cp: int, blk: int) -> None:
    m = _m()
    step, V = config.MARKETS_LOCK_CHECK_S, config.MARKETS_VAULT_COLDKEY
    lo_us, hi_us = (cp - step) * 1_000_000, cp * 1_000_000
    price = price_at(ch, blk)
    _credit_sweeps(db, ch, cp)
    reqs = _canonical(_claims(db, KIND_WITHDRAW, lo_us, hi_us) + _claims(db, KIND_EXIT, lo_us, hi_us))
    for c in reqs:
        ent = c["hk"]
        if price is None:
            db.execute("INSERT OR IGNORE INTO vault_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (c["key"], ent, c["kind"], c["amount"], cp, "0", "refused", None, None, None))
            continue
        units = sum((Decimal(u) for (u,) in db.execute(
            "SELECT units FROM vault_ledger WHERE entity=? AND t_us <= ?", (ent, hi_us))), Z)
        bal = m.CTX.multiply(units, price)
        lk = min(locked(db, ent, cp), max(bal, Z))
        # open bets: received at or before cp (all decided by now), market not ended before cp
        opn = Z
        for a, mid in db.execute("SELECT c.alpha, b.market_id FROM coll c JOIN bets b ON b.key = c.key "
                                 "WHERE c.entity=? AND c.status='ok' AND c.t_recv_us <= ?", (ent, hi_us)):
            if m.parse_market_id(mid)[3] >= cp:
                opn += Decimal(a)
        avail = max(Z, bal - lk - opn)
        want = avail if c["kind"] == KIND_EXIT else Decimal(c["amount"])
        ok = min(want, avail).quantize(m.RAO, rounding="ROUND_DOWN")
        if ok > 0:
            _post(db, f"wd:{c['key']}", hi_us, ent, -m.CTX.divide(ok, price), -ok, "withdraw", c["key"])
        db.execute("INSERT OR IGNORE INTO vault_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (c["key"], ent, c["kind"], c["amount"], cp, str(ok), "approved" if ok > 0 else "refused",
                    None, None, None))
        if c["kind"] == KIND_EXIT:
            if lk > 0:
                u = m.CTX.divide(lk, price)
                _post(db, f"forfeit:{c['key']}", hi_us, ent, -u, -lk, "forfeit", c["key"])
                _post(db, f"forfeit-in:{c['key']}", hi_us, RESERVE, u, lk, "forfeit", c["key"])
            db.execute("INSERT OR IGNORE INTO vault_exits VALUES (?,?,?,?)", (ent, cp, m._closed_by(cp), str(lk)))
    # approved and unreleased past the deadline: a recorded default
    db.execute("UPDATE vault_requests SET status='default' WHERE status='approved' AND cp + ? <= ?",
               (config.MARKETS_VAULT_RELEASE_DEADLINE_S, cp))
    # solvency: the vault's stake must cover every balance, the reserve and approved unreleased withdrawals
    chain = ch.stake_of(V, config.MARKETS_VAULT_HOTKEY, blk)
    units_all = sum((Decimal(u) for (u,) in db.execute("SELECT units FROM vault_ledger WHERE t_us <= ?", (hi_us,))), Z)
    pending = sum((Decimal(a) for (a,) in db.execute(
        "SELECT approved FROM vault_requests WHERE status IN ('approved','default') AND cp <= ?", (cp,))), Z)
    ledger = (m.CTX.multiply(units_all, price) if price is not None else Z) + pending
    short = int(chain < ledger - config.MARKETS_VAULT_SOLVENCY_TOL)
    db.execute("INSERT INTO vault_checks VALUES (?,?,?,?)", (cp, str(chain), str(ledger), short))
    if short:
        print(f"  !! MARKETS VAULT SHORT at {cp}: chain {chain} < ledger {ledger}")


# ── settlement: the reserve pays first ────────────────────────────────────────
def reserve_pay(db: sqlite3.Connection, ch, cycle: int, claims: dict[str, Decimal]) -> dict[str, Decimal]:
    """Pay `claims` (entity -> alpha) from the reserve, pro rata up to its balance, as vault credits at the
    cycle's close. Returns entity -> alpha paid. A no-op before the vault arm or with an empty reserve."""
    if not config.markets_vault_as_of(cycle):
        return {}
    m = _m()
    tc = m._cycle_end(cycle) + config.MARKETS_SETTLE_GRACE_S
    blk = ch.block_at(tc)
    price = price_at(ch, blk) if blk is not None else None
    if price is None:
        return {}
    t_us = tc * 1_000_000
    have = m.CTX.multiply(units_before(db, RESERVE, t_us), price)
    tot = sum((v for v in claims.values() if v > 0), Z)
    if have <= 0 or tot <= 0:
        return {}
    scale = min(Decimal(1), m.CTX.divide(have, tot))
    out = {}
    for ent in sorted(claims):
        a = (claims[ent] * scale).quantize(m.RAO, rounding="ROUND_DOWN") if claims[ent] > 0 else Z
        if a <= 0:
            continue
        u = m.CTX.divide(a, price)
        _post(db, f"rsv:{cycle}:{ent}", t_us, RESERVE, -u, -a, "reserve_pay", str(cycle))
        _post(db, f"rsv-in:{cycle}:{ent}", t_us, ent, u, a, "reserve_pay", str(cycle))
        out[ent] = a
    return out


def carry_floor(db: sqlite3.Connection, entity: str, cycle: int) -> int | None:
    """The first cycle whose carry an exit restarts: cycles after the one closed at the entity's last
    exit before `cycle` closes. None = no exit."""
    row = db.execute("SELECT MAX(after_cycle) FROM vault_exits WHERE entity=? AND after_cycle < ?",
                     (entity, int(cycle))).fetchone()
    if not row or row[0] is None:
        return None
    return _m()._cycle_end(row[0])


def balances(db: sqlite3.Connection, ch, t: int) -> dict[str, dict]:
    """For views: entity -> {units, alpha, locked, available-before-open-bets} at t."""
    blk = ch.block_at(int(t))
    price = price_at(ch, blk) if blk is not None else None
    out = {}
    for (ent,) in db.execute("SELECT DISTINCT entity FROM vault_ledger"):
        u = units_before(db, ent, int(t) * 1_000_000 + 1)
        a = _m().CTX.multiply(u, price) if price is not None else None
        lk = locked(db, ent, t) if ent != RESERVE else Z
        out[ent] = {"units": str(u), "alpha": str(a) if a is not None else None, "locked": str(lk),
                    "free": str(max(Z, a - lk)) if a is not None else None}
    for ent, v in emission_outstanding(db, _m()._closed_by(int(t)), int(t) * 1_000_000 + 1).items():
        out.setdefault(ent, {})["emission_outstanding"] = str(v)
    return out
