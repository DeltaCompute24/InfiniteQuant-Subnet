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

Voided signals don't count against your daily quota and carry no penalty —
with one exception: a blob that fails hash-verification or won't decrypt at
reveal is a strike; three strikes in 30 days zeroes the hotkey for 30 days.

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

## Running a Miner

```bash
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate     # timelock wheel requires 3.10
pip install -r requirements.txt

# 1. a registered hotkey on netuid 89
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner

# 2. a public bucket for your ciphertext blobs (any S3-compatible host)
export SN89_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
export SN89_R2_BUCKET=<bucket>
export SN89_R2_ACCESS_KEY_ID=… SN89_R2_SECRET_ACCESS_KEY=…
export SN89_R2_PUBLIC_BASE=https://<public-bucket-host>

# 3. submit
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit --pair BTCUSD --direction LONG
```

Or run the REST intake and POST from your own stack (TradingView webhook,
your bot, anything):

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner serve --port 8089
curl -s localhost:8089/submit -d '{"trade_pair":"XAUUSD","direction":"SHORT"}'
```

A successful submit prints your commitment hash, the drand reveal round, and
the reveal time. Keep your clock NTP-synced. Miners do **not** need a market
data subscription — the validators price everything.

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
until reveal + grading. No blob by reveal ⇒ void.

**When do emissions arrive?** Weights update every tempo (~72 min); your first
non-dust weight lands after your 20th decisive trade, assuming ≥ 52 %.

**Where did the crypto design come from?** The envelope construction follows
the drand-timelock payload pattern proven on Bittensor by MANTIS (SN123), MIT
licensed.

## License

MIT
