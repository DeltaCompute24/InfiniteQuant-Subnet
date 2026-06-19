# InfiniteQuant Subnet — SN89

**Bittensor subnet 89 — trade-signal mining.** Miners submit encrypted trade
setups ("signals"); validators grade them walk-forward against market data and
set weights pro-rata to winning trades among qualified miners. No participant
can read another miner's signal until 24 hours after it is committed.

## About

### How it works

A miner builds a signal (pair + direction; TP/SL are fixed per-asset bands),
encrypts it with a [drand timelock](https://drand.love) that opens 24 h later,
uploads the ciphertext to a public bucket, and records `SHA256(signal)`
on-chain via `set_commitment`. **The block the commitment lands in is the
signal's timestamp**: validators read the commitment's exact inclusion block
from on-chain storage (millisecond-precise via the Timestamp pallet) and
anchor the entry at the open of the first 1-second market bar at or after
that block's time + 30 s — so every validator derives the identical entry
price, and the miner never states one, leaving nothing to backdate or
cherry-pick.

After 24 h the timelock opens; validators verify the plaintext hashes to the
on-chain commitment and grade it walk-forward on 1-minute candles: first touch
of TP wins, first touch of SL loses, both-in-one-candle loses, no touch within
the horizon is a wash. Emissions are shared pro-rata by wins over the trailing
8 days among qualified miners.

**A touch must be a real market price, not a glitch.** Candles are bad-tick
sanitized before grading: a one-minute spike wick more than 1 % beyond the
candle body and both neighbouring candles is treated as an uncorroborated
print and clamped — unless a second independent feed (Hyperliquid, for
crypto) traded the same level in the same minute, in which case the move was
real and stands. A single off-market trade can never trigger your SL or TP.
This matches how real exchange brackets fill (a median across feeds, never one
print), so the scored result is what a live bracket would have done.

### Payload format

Each submission blob contains one content ciphertext and two key-wraps
(`x25519-hkdf-sha256+chacha20poly1305+drand-tlock`):

| Field | What it is | Opens |
|---|---|---|
| `C` | the signal, ChaCha20-Poly1305, AAD-bound to hotkey + round | with the content key |
| `W_time` | content key, drand-timelocked | at the reveal round (24 h) |
| `W_owner` | content key, wrapped to the subnet-owner X25519 key | immediately, by the subnet owner |

Both wraps open to the same key and the plaintext must hash to the on-chain
commitment — a blob whose plaintext doesn't match its commitment is void and
strikes the hotkey. The `W_owner` wrap is a protocol requirement; submissions
without a valid owner wrap are not gradeable.

### The rules (enforced at grading — violations void the signal; resubmit)

| Rule | Value |
|---|---|
| Max signals per UTC day | 3 per hotkey |
| Min spacing between your signals | 4 h |
| Cross-miner cooldown | 15 min per (pair, direction) — if any miner committed the same pair+direction in the last 15 min, later ones are void. First commit wins. |
| TP / SL | fixed per-asset, symmetric 1:1 — see `data/signals-bands.json` |
| Horizon | 72 h max; no touch by then = WASH (not counted) |
| Overlap | one open signal per (pair, direction) per hotkey |
| Must reveal | a committed signal whose blob is never served counts as a LOSS (forfeit), 6 h past its reveal round — see below |

Voided signals don't count against your daily quota and carry no penalty —
with one exception: a blob that fails hash-verification or won't decrypt at
reveal is a strike; three strikes in 30 days zeroes the hotkey for 30 days.

**You must reveal what you commit.** The timelock hides your signal from
*others* for 24 h — it does not hide it from *you*, and the market is public,
so a "publish only the winners" option would otherwise be free. It isn't: a
commitment whose ciphertext blob is still unfetchable 6 h after its reveal
round matures is recorded as a decisive **LOSS** (`exit_reason=no_reveal`),
exactly as if the trade had hit its stop. Withholding a loser costs what
revealing it would, so there is nothing to game. A blob the validator already
fetched is pinned in its journal and grades normally even if you later delete
it; the forfeit only catches blobs that were *never served*.

### Scoring

```
decisive    = WON + LOST   (washes and voids never count)
QUALIFIED   = lifetime decisive ≥ 20  AND  trailing-8d hit rate ≥ 52 %
your weight = your trailing-8d wins / all qualified miners' trailing-8d wins
```

- **Warmup:** new hotkeys get 8 days of immunity with dust emissions — enough
  time to put ~20 trades on the board at full cadence before scoring bites.
- Random submissions sit below the 52 % gate and earn nothing after immunity.
  Volume cannot substitute for hit rate.
- If no miner qualifies, emissions burn.

### Asset board

38 assets: BTC/ETH/SOL/XRP/HYPE crypto, 29 forex pairs, XAU/XAG/XPT/XPD
metals. Bands are vol-scaled and versioned in `data/signals-bands.json`;
signals grade with the band file in force at commit time — a band update never
retroactively changes an in-flight signal.

## Testnet (netuid 496)

Rehearse a miner or validator against the live test network before touching
mainnet — same code, same protocol, free TAO, no risk. The chain target is
env-driven, so **every command below works on testnet by exporting two
variables**:

```bash
export SN89_NETWORK=test      # Bittensor test network (default: finney)
export SN89_NETUID=496        # SN89 on testnet      (default: 89)
```

### Register a testnet hotkey

Testnet TAO is free from the faucet; then register on netuid 496:

```bash
btcli wallet faucet  --wallet.name mywallet --subtensor.network test
btcli subnet register --netuid 496 --wallet.name mywallet --wallet.hotkey miner \
    --subtensor.network test
```

### Run a testnet miner

Any of the three interfaces below works unchanged — just keep the two env vars
set. The owner-hosted relay serves your blobs on testnet too (zero setup), or
use local disk for a fully self-contained soak:

```bash
export SN89_NETWORK=test SN89_NETUID=496
export SN89_BLOB_DIR=$HOME/.sn89/blobs SN89_R2_PUBLIC_BASE=http://<your-host>:8799
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit --pair BTCUSD --direction LONG
```

### Run a testnet validator

Grading is real market data even on testnet, so a Massive/Polygon key is still
required (see the validator section below):

```bash
export SN89_NETWORK=test SN89_NETUID=496 POLYGON_API_KEY=…
python neurons/validator.py --wallet.name myvali --wallet.hotkey vali
```

> **Reveal delay is protocol-level.** Signals are drand-timelocked, so a commit
> is graded ~24 h after it lands — the same on testnet as mainnet. A fresh
> testnet miner shows dust weight until its first revealed signals grade.

## Running a Miner

### Setup (once)

```bash
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate     # timelock wheel requires 3.10
pip install -r requirements.txt

# a registered hotkey on netuid 89
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner
```

A successful submit (on any interface) prints your commitment hash, the drand
reveal round, and the reveal time. Keep your clock NTP-synced. Miners do
**not** need a market data subscription — the validators price everything.

### Where your blobs are served (pick one)

Your signal is encrypted locally and served at a public URL so validators can
fetch it; the transport is untrusted (integrity is the on-chain commitment),
so it doesn't matter where it lives. In order of least setup:

1. **Owner-hosted relay (default — zero setup).** Push the already-encrypted
   blob through the IQ relay with your `/miner` feed token; we serve it for
   you. **No bucket, no server, no extra keys.** We can't read it (it's
   drand-timelocked 24 h and bound to your hotkey) and can't forge one (the
   on-chain hash only matches a blob your key produced). Active automatically
   whenever `SN89_FEED_TOKEN` / `SN89_RELAY_TOKEN` is set and no bucket creds
   are present.
2. **Your own S3/R2 bucket.** Set the creds and the miner uploads there:
   ```bash
   export SN89_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
   export SN89_R2_BUCKET=<bucket>
   export SN89_R2_ACCESS_KEY_ID=… SN89_R2_SECRET_ACCESS_KEY=…
   export SN89_R2_PUBLIC_BASE=https://<public-bucket-host>
   ```
3. **Local disk + any static server** (testnet soaks): `export SN89_BLOB_DIR=…`
   and serve it at `SN89_R2_PUBLIC_BASE`.

### Interface 1 — Telegram Signals Bot / Chrome extension (`follow` mode)

Already trading in the IQ Signals program? Keep submitting exactly as you do
today — through the Telegram bot or the IQ Copilot Chrome extension — and
mirror your own calls onto SN89 automatically. DM `/miner` to the Signals Bot
for a feed token, then run:

```bash
export SN89_FEED_TOKEN=<token from /miner>
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner follow
```

That one token is all you need — it scopes the feed to your own calls **and**
authorizes the blob relay, so there's no bucket to set up. The follower
long-polls a feed of **your own submissions only** and commits each one with
your local hotkey the moment it appears. Non-custodial by construction: the
platform never sees your keys, and the token can't read anyone else's calls —
so the subnet's no-copy-before-reveal property holds.
The program's submission rules (3 calls/day, ≥ 4 h apart) match the subnet's
consensus rules exactly, so every bot call is SN89-legal.

### Interface 2 — REST API (`serve` mode)

Run the intake and POST from your own stack (TradingView webhook, your bot,
anything):

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner serve --port 8089
curl -s localhost:8089/submit -d '{"trade_pair":"XAUUSD","direction":"SHORT"}'
```

To expose it beyond localhost, set a bearer token and bind the interface
(the server refuses a public bind without one):

```bash
export SN89_INTAKE_TOKEN=<long random string>
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    serve --host 0.0.0.0 --port 8089
curl -s https://<your-host>:8089/submit \
    -H "Authorization: Bearer $SN89_INTAKE_TOKEN" \
    -d '{"trade_pair":"XAUUSD","direction":"SHORT"}'
```

### Interface 3 — CLI one-shot

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit --pair BTCUSD --direction LONG
```

## Running a Validator

> **⚠️ Market-data subscription required.** Running/scoring requires a **paid
> [Massive](https://massive.com) (formerly Polygon.io) subscription** covering
> **both the Currencies feed (forex + metals) and the Crypto feed**, with
> intraday aggregates — the validator pulls **1-second bars** for entry
> anchoring and **1-minute bars** for TP/SL touch grading, across the full
> 38-asset board on every poll cycle. The free tier (5 req/min, end-of-day
> data) cannot grade signals; without a sufficient plan your validator will
> leave signals stuck `pending` and your weights will diverge from consensus.

```bash
export POLYGON_API_KEY=…           # Massive (formerly Polygon) API key — see above
python neurons/validator.py --wallet.name myvali --wallet.hotkey vali
```

State lives in `~/.sn89/validator.db` (SQLite). Grading is deterministic —
same chain + same market data ⇒ same weights — so validators converge without
coordination. Crypto bad-tick corroboration additionally queries Hyperliquid's
public candle API (no key needed). Entry timing design rationale:
`docs/entry-timing.md`.

## FAQ

**Why can't I set my own TP/SL?** Symmetric fixed bands are what make hit rate
a meaningful skill metric (and the 52 % gate meaningful). Vol-scaled bands
keep the bar equivalent across assets.

**What if my blob is unreachable when validators poll?** They retry every 30 s
through reveal + a 6 h grace. A blob they manage to fetch in that window grades
normally (and stays pinned even if you later remove it). A blob that is *never*
served by reveal + 6 h is a forfeit **LOSS**, not a free void — keep your
hosting up, or use the zero-setup owner relay (the relay captures your blob at
submit, so relay-hosted signals can never be lost to a hosting outage).

**When do emissions arrive?** Weights update every tempo (~72 min); your first
non-dust weight lands after your 20th decisive trade, assuming ≥ 52 %.

## License

MIT
