# SN89 — How it works, why it's fair, and how to start

This document answers two kinds of questions:

- **"Are the subnet owners manipulating this to their own advantage?"** — the trust model,
  in detail, with the specific things the owner *cannot* do and the (weak, detectable) things
  they can. See **Part 2**.
- **"I want to mine or validate — how do I start?"** — see **Part 3**. Full commands live in
  the [README](../README.md); this is the orientation.

For the deep consensus / late-validator design, see [`checkpoint-sync-spec.md`](checkpoint-sync-spec.md).

---

## Part 1 — What SN89 is (in one minute)

Miners publish **encrypted directional trade calls**; validators grade them on real market
data and pay emissions to miners who prove **consistent, statistically-real edge**.

1. **Commit.** A miner picks a pair + direction, encrypts the call under a [drand](https://drand.love)
   2-hour timelock, and writes `SHA256(signal)` on-chain via `set_commitment`. The block it
   lands in **is** the timestamp; entry is anchored to the first 1-second bar at/after that
   block time, so every validator derives the identical entry price.
2. **Reveal.** After 2 h the timelock opens; validators verify the plaintext matches the
   on-chain hash, then grade on candles — first touch of TP wins, first touch of SL loses, no
   touch by the horizon is a wash.
3. **Earn.** Emissions go to qualified miners, sized by recent wins × a hit-rate tier.

No collateral, no deposit. Weight is earned purely on a published, gradeable track record.

---

## Part 2 — Is the owner manipulating the network?

The short answer: **the design is built so that the owner — and any single validator — cannot
forge, alter, front-run, or selectively grade signals.** Every grade is a deterministic
function of public inputs that anyone can recompute. Here is exactly what that means.

### What the owner CANNOT do

- **Cannot forge a signal.** Every graded signal must correspond to an on-chain `commit_hex`
  set by the **miner's** hotkey. The owner doesn't hold miners' keys, so cannot manufacture
  commitments or inject signals that didn't happen.
- **Cannot edit a signal's content.** The encrypted blob is bound to the on-chain hash
  `C = SHA256(plaintext‖salt)`. Change one byte of the plaintext and the hash no longer
  matches `commit_hex` — every validator rejects it. The trade you committed is the trade
  that gets graded.
- **Cannot forge a grade or a weight.** Validators do **not** accept anyone's stated outcome.
  Each validator independently re-grades from the on-chain entry block, the public price feed,
  and the on-chain bands-in-force — and independently computes weights. The owner's opinion of
  who won is worth nothing.
- **Cannot read your signal early.** The call is sealed under a 2-hour drand timelock and
  AEAD-bound to your hotkey. The owner relay (default blob transport) is **untrusted transport
  only** — it cannot decrypt or forge a blob, and a relay-hosted blob is **pinned at submit**,
  so a hosting outage can never cost you a forfeit.
- **Cannot retroactively re-grade.** A signal is graded against the band that was in force **at
  its commit block** (`data/signals-bands-history.json`, append-only with effective dates). A
  later band change cannot reach back and void or flip an in-flight signal.

### What the owner CAN do — and why it's weak

The one residual power is **withholding a blob** (declining to serve it). It is deliberately
weak, and it is *not* a clean delete:

- **"Delete" = withhold a blob — and it's weak, not a clean erase.**
  - The on-chain `commit_hex` still exists. Every validator scanning the chain sees that a
    signal was committed at that T0 for that hotkey. You can't make it disappear from the record.
  - Withholding only converts it to a **uniform forfeit `LOST`** — the same verdict for every
    validator. It doesn't vanish; it downgrades.
  - It **can't help anyone.** Withhold your own *loser* → it's still `LOST` (that is exactly
    what the forfeit rule is for). Withhold your own *winner* → you forfeit your own win.
  - The only thing it can do is **grief someone else's winner** — turn a WON into a forfeit-LOST.
    And even that is **detectable** (the commitment-with-no-blob is visible on-chain) and
    **defeatable**: any validator that fetched the blob live keeps it pinned forever, and any
    mirror that holds it can re-serve it, overriding the withholding.

So the worst-case owner abuse is a single, detectable, defeatable griefing vector that cannot
forge, cannot help the owner's own miners, and cannot erase the on-chain fact. Contrast a
trusted-checkpoint subnet (e.g. SN8/Vanta's "mothership"), where the distributor can forge
wins, erase losses, and edit grades outright with no on-chain anchor to check against.

### No privileged validator

Grading is **deterministic**: same chain + same market data ⇒ same weights, so validators
converge with no coordinator and no trusted party. There is no "mothership" whose word other
validators must take. A late-joining validator rebuilds the record by **replaying public
state and verifying every entry against the chain** — not by trusting us (see Part 4 and
[`checkpoint-sync-spec.md`](checkpoint-sync-spec.md)).

### Anti-gaming, by construction

- **No hiding losers.** A committed signal whose blob is never gradeable is a decisive `LOST`,
  not a free pass — so committing and revealing only winners costs exactly the same as
  revealing everything.
- **Confidence, not luck.** Qualification is a **statistical lower bound** on your true
  hit-rate (we must be ~90% confident you beat a coin flip), and the per-win tier is a
  shrunk estimate — a thin lucky streak doesn't qualify or get a high tier. Elimination is a
  **lifetime** confidence bound, so a genuinely-good trader is never zeroed by a cold fortnight,
  while a never-real trader is reliably removed.
- **Copy resistance.** Systematic landing-second into other miners' live trades strips your
  copied wins; the timelock keeps calls sealed while fresh, and systematic following is penalized, and a throwaway hotkey
  can't manufacture copy-flags against honest miners.
- **Real prices only.** Candles are bad-tick sanitized (an uncorroborated wick is clamped
  unless a second feed confirms it); forex/metals rollover bars are dropped from grading.
- **Symmetric fixed bands.** Every outcome is ±1R, so hit-rate is a clean skill metric and
  there's no expectancy surface to game.

---

## Part 3 — Getting started

> Full commands, flags, and the auto-updater are in the [README](../README.md). Requires
> **Python 3.10** (the `timelock` dep has no 3.11/3.12 wheels).

### Miners

You don't need a market-data subscription — validators handle all pricing. Register, then pick
a submission mode:

```bash
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner
```

Three ways to submit (all commit with **your** local hotkey — keys never leave your box):

- **Follow mode** — mirror your IQ Signals Telegram-bot / Chrome-extension calls onto SN89:
  `python neurons/miner.py … follow` (DM `/token` to the Signals Bot for a feed token).
- **REST API** — `python neurons/miner.py … serve --port 8089` then POST to `/submit`.
- **CLI one-shot** — `python neurons/miner.py … submit --pair BTCUSD --direction LONG`.

**Blob hosting:** the default **owner relay** (`SN89_RELAY_TOKEN`) is zero-setup, can't read or
forge your blob, and pins it at submit (no forfeit risk). Or host your own S3/R2 bucket. New
hotkeys get 8 days of warmup (dust weight) to build a record before emissions start.

### Referral / recruiter incentive

An existing miner (the **recruiter**) can vouch for a **new** hotkey (the **recruit**) and both
earn a bonus once the recruit earns:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner refer <recruit_ss58>
# … wait until the referral shows in the public checkpoint (~5 min), THEN register the recruit
```

Rules (all enforced consensus-side; `REFERRAL_*` in `sn89_signals/config.py`):

- **Commit BEFORE registration.** The `sn89ref:1:<recruit_ss58>` commitment must land at least
  `REFERRAL_MIN_LEAD_BLOCKS` (~2 min) before the recruit's registration block, or the claim is
  permanently invalid. One recruit belongs to the earliest claimant. A recruiter may refer any
  number of recruits (no per-recruiter breadth cap); its total referral bonus is still bounded
  by `REFERRAL_MAX_X` × its own tally.
- **Both must be earning.** While both hotkeys have a positive qualified-win tally, the recruit's
  emission tally is boosted `+10%` and the recruiter gains `+20%` of each recruit's tally (total
  referral bonus capped at `100%` of the recruiter's own tally). If either side stops earning,
  both bonuses lapse and resume on re-earn. Bonuses redistribute inside the miner pool — nothing
  new is minted.
- **Strict no-copy inside the pair.** Shadowing between the two hotkeys (a stricter, pair-scoped
  version of the §7.5 detector: 4 sharp episodes or 8 live-overlap episodes in 30 days) suspends
  the bonus of the side that FOLLOWED until 30 days after its last copy event; the other side
  keeps its bonus, and base emission is untouched on both. A call counts as live from its commit
  until its journaled close (or the board horizon if it is still open) — entering the same pair
  and direction after the other side's call has already resolved is not an overlap.
- **Sequencing.** `CommitmentOf` is one latest-wins slot per hotkey: don't commit a referral
  within ~90s of your last signal (either could go unobserved), hold your next signal ~90s, and
  register the recruit only after the referral appears in the checkpoint. The `refer` subcommand
  enforces the first guard and refuses already-registered recruits.

### Validators

Two prerequisites: a paid **Massive/Polygon** plan (Currencies + Crypto, 1-second + 1-minute
aggregates — the free tier cannot grade) and a **validator permit** (staked into the validator
set). Then:

```bash
export POLYGON_API_KEY=…
bash run.sh validator --wallet.name myvali --wallet.hotkey vali   # auto-updater (recommended)
```

State lives in `~/.sn89/validator.db`. **Run the current release** — grading code is consensus;
a stale validator diverges and loses VTRUST. `run.sh` tracks `origin/master` and restarts on new
releases. Testnet works identically with `SN89_NETWORK=test` and the testnet netuid (free TAO).

---

## Part 4 — Consensus & catching up (for validators and deep reviewers)

A validator's standings are a pure function of its **journal** (per-hotkey graded history).
Two validators with the same journal compute identical weights, so the question is whether any
validator can rebuild the same journal trustlessly.

- **The index is on-chain.** Every signal is a `set_commitment` hash with a consensus-exact
  inclusion block (T0). The full commitment set is readable from chain — it is the ground truth
  of what must be accounted for, and no one can hide or fake an entry in it.
- **The grading inputs are public/permanent.** drand signatures (public beacon), the price
  feed (historical bars), and the bands-in-force (committed, append-only) — so re-grading a past
  signal yields the same outcome.
- **The only off-chain piece is the encrypted blob** (the signal content). It is verified
  against its on-chain hash before use, so it cannot be forged — at most it can be *unavailable*,
  which forfeits to a uniform `LOST` for everyone.

To let validators that join **days later** converge to the identical record, SN89 is adding an
**on-chain-anchored checkpoint** (design: [`checkpoint-sync-spec.md`](checkpoint-sync-spec.md)):
an owner/mirror-served archive of blobs that a late validator **verifies entry-by-entry against
the chain and re-grades** — Vanta-style catch-up, but trust-minimized rather than trusted.

### The one honest dependency

Grading reads an external **price feed**, which the checkpoint does not replace (validators
re-fetch prices). All validators must use the same provider with the same query semantics, and
grade only finalized bars; on liquid pairs this makes grades reproducible, but it is the single
place two correct, fully-synced validators could still diverge. We name it openly because a
fairness claim that hides its assumptions isn't one.

---

*Maintainers: keep the scoring summary here in sync with `sn89_signals/config.py` and
`scoring.py` (confidence gate / shrunk tier / lifetime elimination / win cap). The legacy
README scoring section is being updated to match.*
