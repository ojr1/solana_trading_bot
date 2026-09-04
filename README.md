# solana_trading_bot

A signal-driven Solana memecoin trading system in Python. It listens to a Telegram calls
channel, scores each call against a conviction model, sizes a position, and manages the exit
through a layered set of profit-taking and loss-limiting rules.

**Currently runs in dry run only.** Every fill below is simulated at the market cap observed
at that moment. The execution layer is built and tested, but is deliberately not wired into
the live pipeline — see [Execution status](#execution-status).

---

## Why it is built this way

A memecoin call is a low-information event. The channel supplies a handful of fields — a star
rating, holder count, token age, bundled supply percentage, market cap — and the coin either
runs within minutes or dies. Two design consequences follow, and most of this repository is
downstream of them.

**Sizing has to carry the strategy, not selection.** The conviction model accepts most calls;
it earns its keep by committing meaningfully more capital to the small number of calls that
score well than to the ones that do not. Position size is the lever, not a binary buy/reject.

**Exits have to be mechanical.** A coin can gap through several price levels in one second, so
every exit rule is evaluated on a five-second timer against a batched price feed, with no
discretion anywhere in the path.

---

## Architecture

```
Telegram message
   |
   +-- parser.py ............ classify and extract structured fields
   |
   +-- trading_window.py .... entry time gate (entries only, never exits)
   |
   +-- entry_logic.py ....... PCR conviction score -> lot size -> DCA tranches
   |
   +-- runner.py ............ opens position, enforces safety rails
   |
   +-- market_data.py ....... batched market cap polling, every 5s
   |
   +-- exit_logic.py ........ stop-loss, initials, trailing stop, ladder clips
   |
   +-- data_logger.py ....... JSONL event log for later analysis
```

`runner.py` runs two things concurrently under `asyncio`: the Telegram listener, which reacts
to messages as they arrive, and the position monitor, which polls on a timer. Neither blocks
the other.

---

## The entry model — Proprietary Conviction Rating

Each call is scored 0–1 across three weighted factors, then scaled by a market cap multiplier.

```
base = 0.40 x GTscore + 0.40 x holder velocity + 0.20 x bundled supply
PCR  = base x market cap multiplier
lot  = MIN_LOT + PCR_stretched x (MAX_LOT - MIN_LOT)
```

| Factor | Weight | Normalisation |
| :-- | :-- | :-- |
| GTscore (star rating) | 0.40 | Mapped by **rarity**, not linear position: `{1: 0.00, 2: 0.45, 3: 0.77, 4: 0.92, 5: 1.00}`. Roughly three quarters of observed calls are 1–2 stars, so a linear scale would spend the factor's weight suppressing the whole population rather than discriminating within it. |
| Holder velocity | 0.40 | Holders per minute since launch, full score at 60/min. |
| Bundled supply % | 0.20 | Scores 0.0 at 40% or above, with a gamma curve (0.65) shifting the steep section earlier so that *typical* bundling (~14%) sits mid-scale rather than near the top. |
| Market cap | multiplier | Full score at or below $15K; tapers from $50K; **rejected outright at $75K**. Softening factor 0.3 caps how punitive a poor market cap can be. |

**PCR range stretching.** The raw PCR clusters rather than spanning 0–1, because a call rarely
scores well on every factor simultaneously. Mapping that clustered range directly onto the lot
range put roughly 80% of calls within 0.12 SOL of each other. The band 0.10–0.60 is therefore
stretched across the full lot range.

These stretch bounds are calibrated against a simulated distribution, not against logged calls,
and are flagged in the source for recalibration once enough live observations accumulate.

### Multi-tranche entry

A lot is split across up to three buys, each triggered by a 10% fall measured from the
*previous fill* rather than from the first buy.

| Split | Weights |
| :-- | :-- |
| Three tranches | 0.45 / 0.30 / 0.25 |
| Two tranches | 0.60 / 0.40 |
| Single buy | below the two-tranche minimum |

The front-loading is a revision, not the original design. The first shape put the largest
commitment on buy 2, reasoning that a dip with the thesis intact is the better price. Logged
results contradicted it: coins that ran never dipped 10%, so six of twelve winners filled
tranche 1 only and were taken at roughly a third of intended size, while nearly every loser
filled all three tranches on the way down. Buy 1 is now largest because it is the only tranche
a winner is guaranteed to fill.

Removing DCA entirely tested *worse* — the dip fills lowered average entry enough for several
positions to reach their profit trigger at all — so the later tranches were kept and shrunk
rather than deleted.

---

## The exit model

Four mechanisms, which never overlap. Two apply before initials are taken, two after.

| # | Rule | Trigger | Action |
| :-- | :-- | :-- | :-- |
| 0 | **Absolute floor** | Market cap below $9,000 | Sell all |
| 1 | Stop-loss | 55% below average entry | Sell all |
| 2 | Initials | 95% above average entry | Sell 33% |
| 3 | Trailing stop | 60% below peak since entry | Sell all |
| 4 | Ladder clips | Each level crossed | Sell 15% of remainder |

**The absolute floor exists because the stop-loss is measured against average entry, and
average entry falls with every DCA fill.** A coin DCA'd down to a $15K average entry has its
55% stop sitting near $6,750, so the position could keep bleeding well past the point it is
realistically dead. The floor overrides that calculation entirely and is checked first,
regardless of position state.

**Ladder spacing** runs in $50K steps above the initials level, widening to $100K above $500K.
A clip only fires if market cap has risen at least 10% above the price of the *previous sell*,
not above the previous nominal level — if initials fill on a gap at $98K, clipping again at
$100K would sell twice at effectively the same price and waste a rung.

**Spike confirmation.** On crossing a take-profit threshold the bot waits 3 seconds and
re-checks, continuing to wait while the price is still climbing (>2% between checks) up to a
15-second cap, and firing immediately if it pulls back 3% from the high seen while waiting.
Loss exits are exempt: on the way down, waiting costs money.

---

## Safety rails

| Rail | Default | Enforced in |
| :-- | :-- | :-- |
| `DRY_RUN` | `true` | `config.py`, re-checked in `trade_execution.py` at the point closest to signing |
| Max position per coin | 0.25 SOL aggregate across all tranches | `runner.py` |
| Max concurrent positions | 10 | `runner.py` |
| Minimum SOL reserve | 0.05 SOL | `runner.py`, checked live against on-chain balance before every buy |
| Slippage tolerance | 2500 bps | `trade_execution.py` |
| Priority fee | 125,000 lamports | `trade_execution.py` |
| Ticker cooldown | per-ticker | `runner.py` — prevents re-entering the same call |
| Duplicate-entry lock | keyed by contract address, survives process death | `runner.py` |
| Stale call rejection | by message age | `runner.py` |
| Suspension detection | wall-clock gap between monitor cycles | `runner.py` |

All configuration is validated at import time in `config.py` — every value cast, range-checked
and failed loudly at startup rather than surfacing later as a `KeyError` deep in the run. A
blank `SLIPPAGE_BPS` read as `0` would silently mean "accept any price"; this is the layer that
stops that.

`config.py` also logs the resolved configuration at startup with every secret masked, so a
session's log records exactly which settings it ran under.

### Entry time gate

Entries are permitted only inside a fixed window (Europe/London, DST-aware):

```
Saturday, Sunday    open all day
Monday              00:00-09:00, 18:00-24:00
Tuesday-Friday      00:00-06:00, 18:00-24:00
```

The gate applies to **entries only**. The process keeps running 24/7 so open positions stay
monitored — shutting down outside the window previously produced 81–94% overshoot losses on
three positions that went unmanaged.

---

## Execution status

`trade_execution.py` implements the full swap path against Jupiter: quote, build, sign locally,
submit via Helius RPC, and poll until finalised on-chain.

**Nothing in the live pipeline calls it.** `runner.py` simulates every fill. While `DRY_RUN` is
true, `execute_swap()` additionally short-circuits at the point closest to signing, logging
exactly what would have been submitted — mint, amount, resolved slippage, priority fee and the
expected output from a real quote — and returns without building or submitting anything.

That is two independent guards, not one: the call site doesn't call it, and the function
refuses anyway.

---

## Analysis tooling

The bot writes structured JSONL alongside its human-readable log, so decisions can be
re-examined after the fact:

```
data/calls.jsonl            every call seen, and what was decided
data/fills.jsonl            every buy and every sell
data/snapshots.jsonl        periodic price observations on open positions
data/price_history.jsonl    per-cycle observations, post-initials only
```

Four read-only analysis scripts run over that history. None of them writes anywhere.

| Script | Question it answers |
| :-- | :-- |
| `exit_analysis.py` | Peak vs final outcome, exit-type breakdown, slippage-aware baseline, and a comparison of alternative trailing-stop rules |
| `entry_analysis.py` | Does anything recorded at entry separate positions that reached 1.5x from ones that never did? |
| `pcr_analysis.py` | Does any individual entry input actually track outcome? |
| `time_of_day_analysis.py` | Performance by hour, day of week, and window membership |

**On method.** These deliberately do *not* fit a regression. With a few dozen trades against
fifteen candidate inputs, a regression produces impressive-looking coefficients that are mostly
noise. Each input is instead measured against outcome one at a time, with explicit sample-size
honesty sections stating how much of each finding could be luck, and questions the data cannot
answer are reported as unanswerable rather than approximated.

`exit_analysis.py` reads `exit_logic.py`'s live constants directly rather than restating them,
so the "current thresholds" in any report can never drift out of sync with the running code.
`entry_analysis.py` imports the other's P&L model for the same reason.

Written findings live in [`EXIT_ANALYSIS.md`](EXIT_ANALYSIS.md) and
[`ENTRY_ANALYSIS.md`](ENTRY_ANALYSIS.md).

---

## Tests

```bash
python tests/run_all.py
```

Success marker: `ALL CHECKS PASSED` and a zero exit code.

| File | Covers |
| :-- | :-- |
| `test_safety.py` | Reserve floor, same-ticker locking, unconfirmed fills never treated as filled, dry-run never reaching submission, position caps, minimum lot sizing |
| `test_end_to_end.py` | Full pipeline, message through to simulated exit |
| `test_reject_paths.py` | Every rejection route |
| `test_jupiter_fields.py` | Jupiter response field handling |
| `test_analysis_chain.py` | Analysis scripts against fixture data |

The suite exists because work was twice committed that had never reached disk, and the bot then
ran a full overnight session on rules nobody had verified. One command before every commit makes
that state loud rather than silent.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ojr1/solana_trading_bot.git
cd solana_trading_bot
python -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # Mac / Linux
pip install -r requirements.txt
```

### 2. Configure

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Mac / Linux
```

| Variable | Required | Notes |
| :-- | :-- | :-- |
| `TELEGRAM_API_ID` | yes | From my.telegram.org |
| `TELEGRAM_API_HASH` | yes | |
| `TELEGRAM_CHANNEL` | yes | Channel to listen to |
| `HELIUS_RPC_URL` | yes | Solana RPC endpoint |
| `WALLET_PRIVATE_KEY` | only when `DRY_RUN=false` | See security note below |
| `JUPITER_API_KEY` | optional | Falls back to the unkeyed endpoint with a warning |

Remaining variables carry defaults in `.env.example` and are all range-validated at startup.

Verify the configuration before running anything:

```bash
python src/config.py
```

Success marker: `Config loaded and validated OK.` followed by the resolved settings with every
secret masked.

### 3. Run

```bash
python src/runner.py
```

---

## Security

- **No credentials are in this repository, and none ever have been.** `.env`, `*.session` and
  `*.session-journal` have been gitignored since the first commit. `.env.example` contains
  variable names with empty values.
- `WALLET_PRIVATE_KEY` is read as a string in `config.py`, never logged except masked, and
  converted to a `Keypair` in exactly one place — `wallet.py`.
- The keypair is constructed **lazily**, on first use. Importing `wallet.py` does not require a
  private key at all, so a dry run needs no wallet configured.
- `generate_wallet.py` prints a new private key to the terminal. Run it once, copy the key into
  `.env`, then clear the terminal.

---

## Repository structure

```
solana_trading_bot/
├── src/
│   ├── config.py               validated settings layer
│   ├── listener.py             Telegram connection
│   ├── parser.py               message -> structured fields
│   ├── entry_logic.py          PCR, sizing, tranche splitting
│   ├── exit_logic.py           the four exit mechanisms
│   ├── runner.py               orchestration, safety rails, monitor loop
│   ├── market_data.py          batched Jupiter price feed
│   ├── trade_execution.py      quote / sign / submit / confirm
│   ├── wallet.py               key handling, balance checks
│   ├── trading_window.py       entry time gate
│   ├── data_logger.py          JSONL event log
│   ├── data_loader.py          shared loader for analysis scripts
│   ├── exit_analysis.py        \
│   ├── entry_analysis.py        }  read-only analysis
│   ├── pcr_analysis.py          }
│   └── time_of_day_analysis.py /
├── tests/
├── generate_wallet.py
├── .env.example
└── requirements.txt
```

Created at runtime and gitignored: `logs/`, `data/`, `venv/`.

---

## Disclaimer

A personal research project in systematic trading and API integration. Not financial advice,
and no guarantee of profitability. Memecoin trading carries a high risk of total loss.