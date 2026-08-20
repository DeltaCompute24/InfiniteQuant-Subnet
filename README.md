# InfiniteQuant Subnet — SN89

Bittensor subnet 89. Miners commit encrypted directional trade calls. Validators grade them against real market data. Emissions go to miners with statistically proven edge.

> **Requires Python 3.10.** The `timelock` dependency publishes wheels only for 3.7–3.10. On 3.11/3.12: `apt install python3.10 python3.10-venv` (or pyenv) and build the venv with `python3.10`.

## Protocol

1. **COMMIT** — pick a pair + direction. The signal is encrypted with a 2-hour [drand](https://drand.love) timelock; `SHA256(signal)` goes on-chain via `set_commitment`. The commit block is the signal's timestamp. Entry price = open of the first 1-second bar at or after that block's time — every validator derives the same entry.
2. **REVEAL** — after 2 hours the timelock opens. Validators verify the plaintext against the on-chain hash, then grade on 1-minute candles: TP touched first = **WON** · SL touched first = **LOST** · neither by the horizon = **WASH**.
3. **EARN** — emissions ∝ trailing-7-day qualified wins (capped at 20) × hit-rate tier.

- A committed blob that never reveals is a decisive **LOSS**. Committing costs what revealing costs.
- Candles are bad-tick sanitized (uncorroborated wicks clamped; crypto cross-checked against Hyperliquid). Forex/metals rollover bars [20:55–21:20 UTC] are dropped from grading.

## Rules

| Rule | Value |
|---|---|
| Signals per UTC day | **3** per hotkey (from 2026-07-18T00:00:00Z; 6 before) |
| Min gap between calls | **1 h** (from 2026-07-18T00:00:00Z; none before) |
| TP / SL | per-asset, symmetric 1:1 — `data/signals-bands.json` |
| Grade window | crypto 8 h · forex/metals 12 h · equities 48 h |
| Same-pair repeats | allowed — including long-then-short on one pair |
| Unrevealed blob | LOSS 6 h past reveal round |
| Bad blob (hash/decrypt fail) | strike — 3 strikes in 30 d zeroes the hotkey 30 d |

Limits are as-of versioned (`config.SUBMISSION_RULES_HISTORY`): each signal is judged by the rules in force at its own commit time. A rules change never retroactively voids older signals. The miner tooling enforces the limits locally, so an over-limit call is refused with a reason instead of silently voiding.

## Scoring

```
decisive    = WON + LOST   (washes and voids never count)
reputation  = decisive trades in the last ~60 days, capped at the most recent ~100
QUALIFIED   = window decisive ≥ 8  AND  Wilson lower bound on true hit-rate ≥ 50%
              (~90% confidence you beat a coin flip — luck does not qualify)
tier        = on a SHRUNK hit-rate (thin samples pulled toward 50%):
              QUALIFIED ≥ 55% → 1.0×  |  SHARP ≥ 60% → 1.2×  |  WOLF ≥ 70% → 2.0×
your weight ∝ (your qualified wins × tier, each decaying linearly to 0 over 7 d, count-capped 20) / Σ same over all qualified
```

- Reputation decays (~60 d / ~100 trades): old wins can't mask bad recent trading, and a bad early stretch ages out.
- Wins decay linearly over 7 days and are capped at 20 — conviction outweighs volume.
- **Warmup:** first 8 days = dust weight. Warmup trades build the record; warmup wins never pay. Post fresh wins after warmup to earn.
- If no qualified miner has recent wins, emissions burn.

## Copy penalty

- The first eligible hotkey on a `(pair, direction)` is the original; a different hotkey opening the same trade while it is live lands second.
- Landing second on ≥ 50% of your decisive trades (min 5) + a sharp 1:1 shadowing signature = habitual copier. Copied wins stop counting toward hit-rate.
- The first mover is always safe. Occasional overlap on a crowded trade is fine — the penalty targets systematic following.
- Leader eligibility (≥ 10 decisive, not eliminated, not holding both directions of a pair) blocks throwaway hotkeys from manufacturing copy-flags.

## Referral

- An existing miner can claim a **new** hotkey before it registers: `python neurons/miner.py refer <new_hotkey_ss58>` — commit the claim, wait for it to appear in the public checkpoint (~5 min), **then** register the recruit.
- While both hotkeys are earning: recruit +10% emissions, recruiter +20% of each recruit's score (capped at 100% of own score).
- A hotkey that registered before the claim can never be claimed. Adding a second miner? Claim it first.
- Strict pair no-copy: shadowing inside the pair suspends the bonus 30 days.

## Assets

| Class | Pairs |
|---|---|
| Crypto | BTCUSD · ETHUSD · SOLUSD · XRPUSD · TAOUSD · HYPEUSD |
| Metals | XAUUSD · XAGUSD |
| Forex | AUDUSD · USDCHF · USDCAD |

Bands are per-asset, volatility-scaled, symmetric 1:1. A signal is graded against the band in force **at its commit block** (`data/signals-bands.json` + `data/signals-bands-history.json`) — a band update never changes an in-flight signal.

**Forex narrows to AUDUSD · USDCHF · USDCAD at 2026-08-13T00:00:00Z** (`fxmacro3-20260813`). EURUSD, GBPUSD, USDJPY, NZDUSD, NZDCHF, AUDNZD, GBPCAD and NZDJPY leave the board. The three that stay each track a hard asset — the commodity complex, the safe-haven bid and crude — so the forex board carries the same kind of view as the metals and crypto rows. Bands do not change: AUDUSD stays 28 bps, USDCHF 26, USDCAD 20. A call committed before that instant on any of the eight grades normally, on the board in force at its own commit block. On the high-frequency board the same change lists AUDUSD only.

The table above is the **low-frequency** board (mechanism 0). The **high-frequency** board (mechanism 1) is a smaller set — see *Mine — High-Frequency* below.

---

## Mine

```bash
# 1. install
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. register your hotkey (~0.05 TAO burn)
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner

# 3. get a token — DM  /token  to @Iqsignals2026_bot
export SN89_FEED_TOKEN=<token>

# 4. run (pick one mode)
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner follow   # mirror your IQ Signals bot/extension calls
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner serve --port 8089   # REST intake
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner submit --pair BTCUSD --direction LONG   # one-shot
```

- No market-data subscription needed — validators handle all pricing.
- The token scopes the feed to your own calls and authorizes the blob relay. Non-custodial: keys never leave your box.
- **One feed = one hotkey.** Mirroring the same feed from a second hotkey flags the duplicate as a copier.
- **Second miner? Claim it before registering** — see Referral above.
- Prefer auto-update (validators must track releases; miners should too):

```bash
bash run.sh miner follow --wallet.name mywallet --wallet.hotkey miner
```

### Mine — High-Frequency (mechanism 1)

SN89 runs **two incentive mechanisms** under one netuid. Mechanism 0 is the
low-frequency Signals program above. Mechanism 1 is **high-frequency**: shorter
holding times, tighter bands sized to what a pair actually moves in that window, and
up to **30 calls/day**. Same hotkey — **no separate registration**. The owner sets
the emission share between the two on chain.

HF does not commit each call on chain (a 12-second block cannot carry HF cadence).
Instead you open a WebSocket to the ingest, send a **signed frame**, and get back a
**countersigned receipt** — the receipt is your proof we accepted the call. Every
window of receipts is Merkle-rooted on chain and the full log is published, so
grading is replayable: anyone can re-derive the weights from the public logs.

```bash
# one HF call (same wallet/hotkey as your LF miner)
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit-hf --pair XAUUSD --direction LONG
```

The command signs the frame with your hotkey, sends it to `wss://hf.infinitequant.app`,
verifies the receipt against the published ingest key, and appends it to
`~/.sn89/hf_receipts_<hotkey>.jsonl` (keep these — they are your fraud-proof). A
strictly-increasing sequence per hotkey is tracked in `~/.sn89/hf_seq_<hotkey>.json`.

**Two gates decide whether an HF win earns anything.** Both are computed from the
published windows, so you can check either one yourself.

*Participation.* 50 accepted submissions across 8 distinct UTC days. Wins before you
clear it are warmup and pay nothing. Wins after it pay normally.

*Direction diversity.* You must take both sides of the market some of the time, and
how often depends on how many pairs you trade. Over a trailing 30 days we take the
smaller of your LONG and SHORT counts **on each pair separately**, sum those, and
divide by your total calls. That share must be at least:

| distinct pairs | minimum minority-direction share |
|---|---|
| 2 or fewer | 20% |
| 3 to 4 | 12% |
| 5 to 6 | 6% |
| 7 or more | 3% |

Below 40 calls in the window the rule does not apply.

Before 2026-08-19 we counted LONG and SHORT across your whole book at once. A miner
could be 100% long on the pairs they actually traded and still meet the minimum with
a small mostly-short book on a pair they barely touched. Counting per pair removes
that path. Seven shorts on a pair you called seven times count for seven. They do not
offset three hundred one-way calls on another pair.

A hotkey that fails it earns zero for as long as it fails. This is reversible and it
is not an elimination: start taking the other side and your weight returns on its
own, with no re-registration. A miner covering the whole board is allowed to be
lopsided, because a house view across eight instruments is still eight separate
decisions. A hotkey on two pairs that has never once gone the other way has made one
decision and repeated it, and the mechanism pays for forecasts.

### Closers — vote on the network's open positions

The third competition pays for exit timing: the network publishes its OPEN
positions (`SN89_CLOSERS_POSITIONS_URL`, JSON `{positions: [{id, trade_pair,
direction, ...}]}`), and you submit **HOLD** (it will keep improving) or
**CLOSE** (it will give back) on any position, whenever you choose:

**1. List what is open.** Positions are keyed by an id you pass to the vote, so
start here:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner positions
```

```jsonc
{"generated_at_ms": 1785..., "positions": [
  {"id": "f8973be2-58ec-467d-a718-0a644df9229b",
   "trade_pair": "BTCUSD", "venue_pair": "BTCUSDC",
   "direction": "LONG", "opened_ms": 1785..., "miner": "iq"}
]}
```

`trade_pair` is what your vote is GRADED on; `venue_pair` is the instrument we
actually hold (a USDC perp grades against its USD spot twin). A position only
appears once it has moved ±0.10% in position P&L — before that it is not votable,
so an empty list is normal, not an error.

**2. Vote.** `HOLD` = it keeps improving, `CLOSE` = it gives back:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit-closers f8973be2-58ec-467d-a718-0a644df9229b CLOSE
```

Prints the countersigned receipt on success; a refusal prints `REFUSED: <reason>`
and exits non-zero, so it scripts cleanly.

**3. Push feed, for an algo.** Holds an SSE stream open and prints one JSON line
per change — new position, closed position — instead of polling:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    watch-positions | your_algo
```

**4. Rest a vote until a price.** Fires locally when the trigger hits, so your
keys never leave your box:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    limit --kind closers --position-id <id> --action CLOSE --trigger 63500
```

`limit` also takes `--kind hf|lf` with `--pair` and `--direction` for the other
two competitions.

Same transport and countersigned receipt as HF — one more payload kind on the
same ingest. Grading: the vol-normalized move over the horizon after your
call, + for HOLD if the position improved, + for CLOSE if it deteriorated,
winsorized at ±3σ; the sum over the rolling window ranks you on the Closers
board. Capped per UTC day; entry requires being a qualified LF or HF miner.
USDC-quoted (Hyperliquid) positions grade against their USD spot twin's tick
series — the alias table is committed in
`scripts/closers_positions_publisher.py`.

**HF board** (bands fixed by the board; you cannot choose them):

| Pair | TP / SL | Horizon |
|---|---|---|
| XAUUSD | ±10 bps | 30 min |
| BTCUSD | ±19 bps | 30 min |
| ETHUSD / SOLUSD / XRPUSD | ±24 bps | 30 min |
| AUDUSD | ±8.4 bps | 120 min |
| TAOUSD | ±53.1 bps | 120 min |
| HYPEUSD | ±62.6 bps | 120 min |

Fewer pairs than LF by design: a pair is listed only if its band clears ≈8× the
typical spread at the HF horizon — below that the outcome is microstructure, not
signal. Live board + wash times: <https://partner.infinitequant.app/sn89/mechanisms>.

**Rules.** Up to 30/day per hotkey, ≥250 ms apart. A pair you trade on one mechanism
is **locked on the other for 24 h** (same view can't earn twice). Qualification is the
same gate as LF — 8 decisive results, hit-rate beating a coin flip — computed over your
**HF-only** record, so your LF standing gives no head start: a first HF win earns nothing.

**Verify us.** Every window's receipts + ticks + anchor are public at
`<HF_PUBLIC_BASE>/<window>/` (index at `.../index.json`). Fetch them, recompute the
Merkle roots, check them against the on-chain anchor, re-grade off the published
ticks. Full design: <https://infinitequant.app/signals-hf-spec>.

### Blob hosting

Signals are encrypted locally and served at a public URL for validators. The transport is untrusted — integrity is the on-chain commitment. Default is the **owner relay** (zero setup, activated by `SN89_FEED_TOKEN`; blobs are pinned at submit, so a hosting outage can never cause a forfeit). Alternatives:

```bash
# your own S3/R2 bucket
export SN89_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
export SN89_R2_BUCKET=<bucket>
export SN89_R2_ACCESS_KEY_ID=… SN89_R2_SECRET_ACCESS_KEY=…
export SN89_R2_PUBLIC_BASE=https://<public-bucket-host>

# local disk + static server (testnet)
export SN89_BLOB_DIR=$HOME/.sn89/blobs
export SN89_R2_PUBLIC_BASE=http://<your-host>:8799
```

### Link your X handle

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner register-x --handle @yourname
```

Shows your handle on the public leaderboard. Signed locally; keys never leave the box.

### Emissions & elimination

No collateral, no deposit — weight is earned on track record:

| State | Result |
|---|---|
| Immunity (first 8 days) | dust weight |
| Below the confidence gate | no emissions |
| Qualified (≥ 8 decisive, Wilson LB ≥ 50%) | emissions ∝ capped trailing-7d wins × tier |
| Lifetime hit-rate confidently < 45% (≥ 40 decisive) | **eliminated** — hotkey zeroed permanently |

Elimination is a lifetime confidence test, separate from the (reversible) qualify gate: a cold streak drops you to zero emissions until you recover; only a durably sub-floor record is removed.

### Testnet (netuid 496)

Same code, free TAO:

```bash
export SN89_NETWORK=test SN89_NETUID=496
btcli wallet faucet --wallet.name mywallet --subtensor.network test
btcli subnet register --netuid 496 --wallet.name mywallet --wallet.hotkey miner --subtensor.network test
```

Reveals still take 2 h; grading follows each call's horizon.

---

## Validate

Requirements:

1. **Validator permit** — the hotkey must be staked into the validator set, or weight commits never reveal.
2. **No market-data subscription.** Nothing else is needed.

```bash
bash run.sh validator --wallet.name myvali --wallet.hotkey vali    # auto-updater (recommended)
```

**You do not need a market-data key.** This section used to require a paid Massive
(Polygon) plan and `export POLYGON_API_KEY=…`, and that instruction was stale from
2026-07-25, when the grading rule became `touch_ticks`. Grading now reads the
Merkle-anchored tick series published at `SN89_HF_PUBLIC_BASE` over plain HTTPS with
no authentication — `grader._grade_touch_ticks` fetches it, and the anchor on chain
is what makes it trustworthy rather than the vendor it came from. Every validator
therefore grades the *same* prices, which is the point: a private feed would make
two honest validators disagree.

`POLYGON_API_KEY` is now optional and affects one thing only: replaying calls whose
t0 predates 2026-07-25, which grade under the older `close_1m` substrate and do need
1-minute aggregates. A validator running today's board never reaches that path.

There has never been a Taostats key in this package — it appears nowhere outside the
vendored `bittensor_cli`, which only uses it as a block-explorer URL.

State: `~/.sn89/validator.db`. Grading is deterministic — same chain + same anchored
ticks ⇒ same weights. **Run the current release**: grading code is consensus; a stale validator diverges and loses VTRUST.

### Timelock version compatibility — READ THIS IF VTRUST IS FALLING

Two mutually-unreadable drand-tlock ciphertext formats exist:

| `timelock_wasm_wrapper` | `W_time` | status |
|---|---|---|
| **0.3.0** (vendored here) | **245 B** | **consensus — what the live fleet seals** |
| 0.0.2 (PyPI's newest) | 261 B | legacy — some self-hosted miners |

`pip install -r requirements.txt` installs the consensus version from
`vendor/timelock/`. Check any install:

```bash
python tools/check_timelock.py
```

LF is the only competition gated on opening a reveal, and a validator that
cannot open the consensus format **fails silently**: reveals never open, no
miner reaches `QUALIFY_MIN_DECISIVE` inside the 7-day `EMISSION_DECAY_S`
window, the LF earner set empties, and `combine()`'s dead-share rule burns
LF's entire share. The only symptom is falling vtrust. If yours is falling,
run the command above first.

To also open **legacy** reveals (needed to replay history), add the sidecar:

```bash
python3.10 -m venv .venv-tl001 && .venv-tl001/bin/pip install "timelock==0.0.1.dev0"
export SN89_TLD_FALLBACK_PYTHON=$PWD/.venv-tl001/bin/python
```

A blob that opens under **either** version is graded normally; only one that
fails both is voided.

### Seeding a new or damaged validator from the checkpoint

A validator that starts today sees only what happens from the moment it turns on.
There is no live catch-up by design (see `docs/single-validator-model.md`), so it
pays LF miners nothing until each re-earns `QUALIFY_MIN_DECISIVE` from scratch —
on observed rates 6 to 28 days, losing VTRUST throughout. The same applies to a
validator whose journal was damaged.

This is a DELIBERATE, ONE-TIME step. `run.sh`'s auto-updater will never do it —
it pulls code, reinstalls dependencies when `requirements.txt` changes, and
restarts. It does not touch your journal, and should not: seeding a database is
not something that should happen behind an operator's back.

```bash
sudo systemctl stop sn89-validator          # never import into a live DB
cp ~/.sn89/validator.db ~/.sn89/validator.db.bak.$(date -u +%Y%m%dT%H%M%SZ)

python3 scripts/import_checkpoint.py \
    https://partner.infinitequant.app/sn89/checkpoint.json \
    --db ~/.sn89/validator.db --anchors 50 --dry-run   # inspect first

python3 scripts/import_checkpoint.py \
    https://partner.infinitequant.app/sn89/checkpoint.json \
    --db ~/.sn89/validator.db --anchors 50
sudo systemctl start sn89-validator
```

Take the backup. The import updates rows in place and preserves every field it
does not own, but an earlier version used `INSERT OR REPLACE` and nulled
`t0_ms`, `blob_json`, `entry_price`, `outcome_bps` and `exit_reason` on every
row it touched — caught only because it was tested against a populated journal
rather than an empty one. A copy costs seconds.

`--dry-run` verifies and reports without writing, so run it first and read the
counts. Then watch the next weight commit: `earners: lf=` in the validator log
should jump, because the miners whose calls were unreadable now carry their
decayed win tallies again.

What it checks before writing anything:

| tier | what it proves | how |
|---|---|---|
| no altered signals | the plaintext is what the miner committed | `sha256(plaintext)` vs the on-chain `commit_hex` — the commitment carries no salt, so this is checkable offline by anyone. A row that fails is refused. |
| no fabricated signals | the commitment is really on chain | `--anchors N` reads each back at its `commit_block`. Needs an **archive node**; a pruned node returns nothing, which is reported as *unreadable*, never as a mismatch. |
| grades | **not verified** | outcomes are imported as published. Re-grade against your own price feed before treating them as yours. |

Measured 2026-08-20: 5290 of 5290 published plaintexts verify, and a journal
imported from the public URL into an empty database reproduced the authoritative
validator's committed vector across 71 uids with 2 differing by more than 1e-3
(worst 0.0016, both HF participants).

`first_seen_unix` — the 8-day immunity clock — is imported deliberately. It is
observational, the instant a validator first saw a hotkey, and deriving it
locally would restart every miner's warmup and defeat the point.

### Delegate via child-key (recommended)

Skip the market-data plan — delegate to the authoritative validator:

```bash
btcli stake child set --netuid 89 \
    --wallet.name <your-wallet> --wallet.hotkey <your-validator-hotkey> \
    --children <AUTHORITATIVE_HOTKEY_SS58> --proportion 1.0
```

### Audit the validator

The authoritative validator publishes its journal as a public checkpoint. Anyone can replay it:

```bash
python3 scripts/audit_journal.py <CHECKPOINT_URL> --chain --anchors --referral-anchors
```

Re-derives the weight vector from the journal and confirms it matches on-chain. See `docs/single-validator-model.md`.

---

## FAQ

**Why can't I set my own TP/SL?** Symmetric fixed bands make hit-rate a clean skill metric; vol-scaling keeps the bar equivalent across assets.

**What if my blob is unreachable when validators poll?** They retry every 30 s through reveal + 6 h grace, and a fetched blob stays pinned. Use the relay and this risk is zero.

**When do emissions arrive?** Weights update every tempo (~72 min). Your first non-dust weight lands once the gate is cleared — a handful of trades for a clear edge.

## License

MIT
