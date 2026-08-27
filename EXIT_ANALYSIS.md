# Exit Analysis — Stage 2, Step 1

Read-only throughout. Nothing in `logs/`, `data/`, or any source file was written to —
confirmed by MD5 hash of all four source files before and after running
`src/exit_analysis.py`; all four unchanged. Branch `stage2-exit-analysis`, created
from `stage1-safety`. Not committed to `main`, not pushed.

This report follows the brief as revised: the original Step 1 "Retrace behaviour"
subsection is cancelled (section 4 below explains why, as a plain finding rather
than an approximation); a new Step 1b (exit type analysis) replaces it; Step 3's
option C is replaced with a comparison of the actual current ladder against two
specified (not backtested) alternatives; a new Step 5 specifies the logging that
would be needed to answer the retrace question in future, for approval only.

---

## 1. Data schema survey

Full detail was posted and confirmed before this analysis began (Step 0). Summary:

- `logs/positions.json`: 64 records, all closed, 0 open. `closed_at`/`last_exit_type`
  present on only 24/64 — a clean cutover, not noise: those 40 missing them were all
  opened before 10 Aug 2026, when the field was added to the code.
- `data/calls.jsonl`: 70 records (61 `bought`, 4 `rejected_fill`, 4 `rejected_time_window`,
  1 `parse_fail`). Jupiter detail fields are null on all 70 — the Stage 2 capture code
  exists but no real buy has run since it was merged.
- `data/fills.jsonl`: 205 records (108 buy, 97 sell) across 62 of 64 contracts.
  **This is the only place sell events exist** — `positions.json`'s own embedded
  `fills` list is buy-only (confirmed: 0 of 116 embedded fill records carry sell shape).
- `data/snapshots.jsonl`: 219 records, a 5-minute heartbeat `mc` reading, covering only
  57 of 64 contracts (~3.8 points/position average) — insufficient density for what
  Step 1's original retrace question needed; see section 4.
- Peak is stored as market cap (`peak_mc`), not price — confirmed directly from
  `exit_logic.py`'s peak-tracking code, updated every 5-second monitor cycle. The
  *value* is exact; the *timestamp* of when it occurred is not recorded.
- DCA tranches: one record per **position**, not per tranche, with an embedded
  buy-only fill list (1–3 entries, matching `entry_logic.MAX_TRANCHES=3`).

---

## 2. Sample size and exclusions

**64 closed positions analysed.** Two excluded from every section that depends on
`data/fills.jsonl` (exit-type analysis, per-position hold duration where `closed_at`
was reconstructed, and the slippage-baseline fee count): **GILF** and **Ratatouille**
— both have zero `data/fills.jsonl` coverage (opened before `data_logger.py` existed).
They remain included in every section that only needs `positions.json` fields
directly (peak/exit multiple, the 1.5x split), since those fields are present on
every one of the 64.

Of the 40 positions missing `closed_at` in `positions.json`, **38** were successfully
reconstructed from the timestamp of their last `data/fills.jsonl` sell event (real
data, not invented — just not written back to the position record under the old
code). The remaining **2** (GILF, Ratatouille, the same two excluded above) have no
`closed_at` and no fills.jsonl coverage to reconstruct it from, so hold-duration is
genuinely unavailable for them specifically.

---

## 3. Step 1 — Peak vs final

n = 64 (no exclusions — every position has `entry_mc`, `peak_mc`, `last_sell_mc`).

**Peak multiple** (`peak_mc / entry_mc`):

| Bucket | Count |
|---|---:|
| <1x | 0 |
| 1–2x | 39 |
| 2–5x | 21 |
| 5–10x | 3 |
| >10x | 1 |

**Exit multiple** (`last_sell_mc / entry_mc` — the market cap at the *final* sell
only; a position with an earlier, higher-priced partial sell, e.g. initials at
1.95x, is not credited for that here):

| Bucket | Count |
|---|---:|
| <1x | 56 |
| 1–2x | 7 |
| 2–5x | 1 |
| 5–10x | 0 |
| >10x | 0 |

**Capture ratio** (exit multiple ÷ peak multiple): median **0.260**, 25th percentile
**0.188**, 75th percentile **0.299**. Typically, only about a quarter of the gap
between entry and peak survived to the final sell.

**Positions that peaked above 2x but exited below 1x: 18 of 64 (28%).**

---

## 4. Retrace behaviour — CANCELLED, reported as unanswerable

**Direct answer: this question cannot be answered from the current logs, and no
coarser proxy was substituted.**

Measuring "how deep did a coin draw down from a peak before either recovering to a
*new* high or dying" requires detecting intermediate peak→trough→peak cycles within
a single position's life. The available data cannot do this, for three compounding
reasons:

1. `peak_mc` is a running maximum overwritten in place — it can only ever report the
   single highest value ever reached, with no timestamp and no memory of any
   intermediate peak that was later exceeded. A coin that went straight to its peak
   is indistinguishable, in this field, from one that peaked, fell 50%, recovered,
   and then made a new higher peak.
2. The only *timestamped* price observations are (a) fill events in
   `data/fills.jsonl`, which by construction only exist at the moment a DCA or exit
   threshold **under the current rules** was crossed, and (b) 5-minute snapshot
   heartbeats, covering 57 of 64 positions at ~3.8 points average.
3. Point 2(a) is the sharper problem: a retrace that stayed *inside* the current 60%
   trailing stop and then recovered fires no sell at all and leaves zero trace
   anywhere. Testing whether a *tighter* stop would help requires exactly the
   retraces the current rules didn't react to — which are the ones this data
   structurally cannot show.

No interpolation, no "assume linear decay from peak to exit," and no proxy metric
was used in place of this. Section 8 (the new Step 5) specifies what would need to
be logged to make this answerable going forward.

---

## 5. Step 1b — Exit type analysis (new)

n = 97 sell events, `data/fills.jsonl`, across 62 of 64 positions (GILF and
Ratatouille excluded — see section 2).

| exit_type | count | median multiple¹ | total SOL realised | median hold² |
|---|---:|---:|---:|---:|
| stop_loss | 27 | 0.389x | 2.9901 | 7.0 min |
| trailing_stop | 21 | 0.768x | 1.8799 | 26.0 min |
| initials | 20 | 2.086x | 4.6632 | 3.6 min |
| ladder_clip | 15 | 3.484x | 0.9243 | 7.9 min |
| absolute_floor | 14 | 0.288x | 1.1335 | 13.0 min |

¹ `mc_at_fill / entry_mc` for that specific sell event.
² time from `opened_at` to that sell event's own timestamp — not necessarily the
position's final close, for partial sells (initials, ladder_clip).

**`stop_loss` + `absolute_floor` (41 sells, the coins that died before initials):
how far did they get first?** Distribution of `peak_mc / entry_mc` at the time of
that sell:

| min | 25th pct | median | 75th pct | max |
|---:|---:|---:|---:|---:|
| 1.000x | 1.177x | 1.386x | 1.463x | 2.550x |

Every one of these had *some* green (minimum ratio is exactly 1.000x, i.e. never
went red before dying) — the median coin that eventually stopped or floored out had
still only reached 1.39x before doing so. None of these 41 came close to 2x.

**Split by whether a position ever reached 1.5x peak:**

| Group | n | Total P&L | Win rate |
|---|---:|---:|---:|
| Ever ≥ 1.5x | 31 | **+1.7819 SOL** | 71% |
| Never ≥ 1.5x | 33 | **−6.2244 SOL** | **0%** |

Every position that never reached 1.5x lost money — zero exceptions in 33. All of
the strategy's profit came from the 31 that did, and even among those, 29% still
lost. This is a plain reading of the numbers, not a recommendation.

---

## 6. Step 2 — Slippage-aware baseline

**As recorded: the logs reflect 0% slippage and $0 priority fee — not the 100bps
the brief assumed.** `trade_execution.py` had 100bps hardcoded, but `runner.py`'s
dry-run fills never call `trade_execution.py` at all: confirmed by reading
`open_position()`/`check_dca_fills()`, every simulated fill in these 64 positions
used the live market cap Jupiter returned directly, with nothing deducted.

| | SOL |
|---|---:|
| Total invested | 17.2436 |
| Total realised | 12.8011 |
| **Total return, as recorded** | **−4.4425** |

**At 2500 bps both legs + 0.000125 SOL/transaction priority fee:**

Assumption stated plainly: the full stated slippage tolerance is modelled as
consumed on *every* fill, symmetrically, on both legs — a conservative,
worst-realistic-case model, not a simulation of what would actually have been
realised (real execution would land somewhere between 0 and this). Buy-side
slippage reduces tokens received per SOL spent (so `sol_invested` is unchanged —
you still spend what you declared); sell-side slippage reduces SOL received per
token sold. Under the further assumption that slippage changes *how much* a
trade nets but not *when* a threshold fires or what fraction is sold (i.e., exit
timing and sizing are unaffected), this collapses to a single scalar:
`realised_sol × (1 − 0.25) / (1 + 0.25) = realised_sol × 0.6000`.

Fee-bearing transactions: 116 buys (all 64 positions, from `positions.json`) + 97
sells (`data/fills.jsonl`) = 213. GILF and Ratatouille's unlogged sells are not
counted, so this slightly *underestimates* total fees, by at most 0.000250 SOL —
negligible next to the totals below.

| | SOL |
|---|---:|
| Total invested (unchanged) | 17.2436 |
| Total realised, adjusted | 7.6806 |
| Total priority fees | −0.0266 |
| **Total return, adjusted** | **−9.5896** |

**Difference: −5.1471 SOL (−115.9% relative to the as-recorded return).** The
historical −4.44 SOL result was measured under conditions roughly twice as
forgiving as what the bot would actually pay today.

---

## 7. Step 3 — Rule comparison

**Current thresholds, read directly from `exit_logic.py`'s constants by importing
the module (not hand-copied, cannot drift out of sync):**

| Constant | Value |
|---|---|
| `STOP_LOSS_DRAWDOWN` | 55% (before initials, vs average entry) |
| `INITIALS_TRIGGER_GAIN` | 95% (sells 50% of position) |
| `TRAILING_STOP_DRAWDOWN` | 60% (after initials, vs running peak) |
| `ABSOLUTE_FLOOR_MC` | $9,000 |
| `LADDER_STEP` | $50,000, up to $500,000 |
| `LADDER_STEP_LARGE` | $100,000, above $500,000 |
| `LADDER_CLIP_FRACTION` | 15% of remaining position, per level |
| `MIN_GAP_BETWEEN_SELLS` | 10% above the previous sell |

**Documentation bug found in passing, not fixed (out of scope, `exit_logic.py` is
off-limits this stage):** the module docstring at the top of the file says the
trailing stop fires "70% below its peak." The actual constant, confirmed above, is
60%. The constant is what runs; the docstring is stale.

### Rule A (current flat 60% trailing stop) and the current ladder

Not backtested — this is what actually happened. Full numbers in sections 5–6
above. For reference: `initials` fired on 20 of 64 positions; `ladder_clip` fired
15 times in total (across however many positions ran far enough to reach a level).

### Rule B (stepped: 60% below 3x, 45% above 5x, 35% above 10x), and the two
alternative ladder level sets: **not honestly simulable from current logs**

Same root cause as section 4. Simulating a *different* rule requires knowing what
the market cap was at the moments that different rule's thresholds would have been
checked — which needs the same continuous price path the retrace question needed
and which does not exist. Building a return figure by assuming a path between the
known points (entry, peak, sparse fills/snapshots) would produce a number that
looks authoritative and isn't, which is explicitly the outcome to avoid.

**What *is* honestly supportable — directional bounds, not return figures:**

- For any position whose `peak_mc / entry_mc < 3x` (39 of 64, the entire 1–2x
  bucket), Rule B is **identical** to Rule A — both use 60%. No difference,
  exactly, for these 39.
- For the 25 positions that peaked at 2x+ (some crossing into B's tighter 3x/5x/10x
  bands), Rule B's stop is strictly tighter than A's at every point above 3x, so B
  would have exited **at or before** the market cap A's actual 60% stop exited at —
  never later, never at a worse price. The exact price and SOL amount is
  undetermined without the path.
- Symmetrically, a *denser* ladder (see Alt 1 below) fires strictly more often than
  the current one for any position reaching a given market cap, so it would have
  banked **at least as much**, and generally more, partial profit on the way up
  than the current ladder did, for the same reason.

**Two alternative ladder level sets, specified for the record (not backtested):**

| | Current | Alt 1 — tighter | Alt 2 — wider |
|---|---|---|---|
| `LADDER_STEP` | $50,000 | $25,000 | $100,000 |
| `LADDER_WIDEN_ABOVE` | $500,000 | $500,000 | $1,000,000 |
| `LADDER_STEP_LARGE` | $100,000 | $50,000 | $250,000 |
| `LADDER_CLIP_FRACTION` | 15% | 15% (unchanged) | 15% (unchanged) |

*Rationale for Alt 1:* section 5 found a median capture ratio of only 0.260 and 18
of 64 positions peaking above 2x but exiting below 1x. A denser ladder banks
partial profit sooner and more often on the way up, aimed directly at that capture
gap.

*Rationale for Alt 2:* tests the opposite hypothesis — that the current ladder
over-trims coins that go on to run much further, banking on genuine outliers
rather than clipping every $50K move.

Worked example, entry at $30,000 (a representative real call size), levels up to
$600,000:

| Scheme | Levels fired by $600K |
|---|---|
| Current | $100K, $150K, $200K, $250K, $300K, $350K, $400K, $450K, $500K, $600K → 10 clips |
| Alt 1 | $75K, $100K, $125K, ... $500K (17 levels), then $550K, $600K → 19 clips |
| Alt 2 | $100K, $200K, ..., $600K → 6 clips (widen point not reached) |

### Rule D (combination of B and the current ladder)

The original brief defined D as "combination of B and C." Since C has been
redefined per your instruction, I've interpreted D as **Rule B's stepped trailing
stop applied on top of the current ladder mechanism** (not the two alternatives —
tripling this comparison wasn't asked for). This is a judgement call, stated here
rather than made silently. Like B itself, D is not honestly simulable for the same
reason: the ladder-clip portion is already exactly what happened (no simulation
needed), but the trailing-stop portion inherits B's path-dependency problem in
full.

---

## 8. Assumptions used in this analysis, listed plainly

1. "Exit multiple" (section 3) uses `last_sell_mc` — the market cap of the position's
   *final* sell only, not a size-weighted blend across partial sells.
2. `closed_at` was reconstructed, for 38 positions, from the timestamp of the last
   `data/fills.jsonl` sell event where the field itself was absent — real recorded
   data, cross-referenced, not invented.
3. Slippage (section 6) is modelled as the full stated tolerance consumed
   symmetrically on both legs of every fill, and as changing *how much* SOL a
   trade nets without changing *when* it fires or what fraction it sells. Both are
   conservative simplifications, not a simulation of actual realised slippage.
4. The priority-fee transaction count (213) omits GILF's and Ratatouille's unlogged
   sells — a known, quantified, negligible undercount (≤ 0.00025 SOL).
5. Rule D (section 7) is interpreted as B's trailing stop combined with the
   *current* ladder specifically, not either alternative — a judgement call, not
   specified by the revised brief.
6. No price path was interpolated anywhere in this document. Where the data
   couldn't support a number, none was produced.

---

## 9. What the data cannot answer, and Step 5 — logging specification (for approval only, not implemented)

**Cannot currently answer:** the retrace question (section 4), and any honest
return/capture/win-rate backtest of a *different* exit rule than the one actually
run (section 7, Rule B and the ladder alternatives).

**Root cause, both times:** no continuous or sufficiently dense price series is
recorded per position. The closest thing that exists, `data/snapshots.jsonl`, is
throttled to a 5-minute heartbeat specifically to bound file volume (its own
purpose in `runner.py` is a liveness heartbeat, not a price history), and it only
fires for positions still open when the check runs, missing anything that opened
and closed inside one window — 7 of 64 positions in the current data.

**Proposed logging (specification, not implemented — for your approval):**

- **New file:** `data/price_history.jsonl` (parallel to the existing
  `calls.jsonl`/`fills.jsonl`/`snapshots.jsonl`, same append-only JSONL pattern).
- **New field, one line per observation:** `ts`, `run_id`, `schema_version`,
  `contract_address`, `ticker`, `mc` (the current market cap), `peak_mc` (the
  running maximum as of this observation — cheap to include, saves a join later).
- **Sampling interval: every monitor cycle a position is open** — i.e.
  `POLL_INTERVAL_SECONDS`, currently 5 seconds. This reuses a market-cap value
  `_monitor_once()` already fetches for every open position every cycle; it adds
  no new network calls, only one JSONL append per open position per cycle.
- **Where in the code:** a new `data_logger.log_price_point(position, current_mc)`
  function, written the same way as the existing `log_snapshot()`. Called from
  `runner.py`'s `_monitor_once()`, inside the `for contract, position in
  open_positions.items():` loop, immediately after `current_mc =
  market_caps.get(contract)` is resolved — the same point `check_dca_fills()` and
  `exit_logic.check_exit_conditions()` are already called from.

**Cost, stated plainly:** roughly 60x the row volume of the current 5-minute
snapshot heartbeat for the same open duration (a position open 30 minutes would
produce ~360 rows instead of ~6). This is the same volume tradeoff
`log_snapshot()`'s own docstring already weighs, just resolved the other way
because the question this is meant to answer specifically needs that density —
your call whether the volume is acceptable.

**Important limit, stated plainly: this fixes the problem prospectively, not
retroactively.** The 64 positions already in `positions.json` cannot be
backfilled — the price path they actually experienced genuinely was never
recorded at this density and cannot be recovered. Only positions opened *after*
this logging exists would support the retrace analysis or an honest rule backtest.

---

## 10. Exact command to re-run this analysis

```
cd C:\projects\solana_trading_bot
venv\Scripts\python.exe src\exit_analysis.py
```

Read-only; safe to re-run at any time, including against a live/growing
`positions.json` — every number above will simply update to reflect however many
closed positions exist at run time.
