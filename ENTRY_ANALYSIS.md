# Entry Discriminator Analysis — Stage 2b

Read-only throughout. Branch `stage2-exit-analysis`. Not committed to `main`, not
pushed. File hashes confirmed identical before and after running
`src/entry_analysis.py` — see section 9.

**Direct answer up front (section 6 has the full reasoning): no. Nothing recorded
at entry distinguishes the two groups strongly enough to trust. The
best-separating field (bundled %) does not clear even an uncorrected significance
bar, let alone one adjusted for testing five fields.**

---

## 1. Group counts

| Split | MOVED | DEAD |
|---|---:|---:|
| At 1.5x | **31** | **33** |
| At 2.0x (secondary) | 25 | 39 |

31/33 at 1.5x matches expectation exactly — no stop condition triggered, proceeded
as instructed. The 2x split moves 6 positions from MOVED to DEAD (25 vs 39),
which section 6 returns to.

---

## 2. Step 1 — candidate discriminators (1.5x split)

Every field below is present on all 64 positions except `age_minutes` (63/64 —
one position has it recorded as null; excluded pairwise from that field's own
stats only, not from the other four).

### `pcr`

| | present | missing | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| MOVED (n=31) | 31 | 0 | 0.314 | 0.190 | 0.418 | 0.051 | 0.474 |
| DEAD (n=33) | 33 | 0 | 0.251 | 0.155 | 0.393 | 0.054 | 0.621 |

Median gap +0.063 (higher in MOVED). IQR overlap fraction **0.77** — heavy
overlap, little discriminating power. (Worth noting: DEAD's max, 0.621, exceeds
MOVED's max — the strategy's own conviction score did not even bound the losers
from above.)

### `gt_score`

| | present | missing | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| MOVED (n=31) | 31 | 0 | 2.000 | 1.000 | 2.000 | 1.000 | 2.000 |
| DEAD (n=33) | 33 | 0 | 2.000 | 1.000 | 2.000 | 1.000 | 2.000 |

Identical medians, identical IQRs. IQR overlap fraction **1.00** — no separation
at all. Every position in the sample is a 1- or 2-star call; this field carries
essentially no variance in this dataset, let alone discriminating power.

### `holders`

| | present | missing | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| MOVED (n=31) | 31 | 0 | 310.0 | 259.5 | 337.0 | 216 | 514 |
| DEAD (n=33) | 33 | 0 | 276.0 | 255.0 | 330.0 | 111 | 469 |

Median gap +34.0 (higher in MOVED). IQR overlap fraction **0.86** — heavy
overlap.

### `age_minutes`

| | present | missing | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| MOVED (n=30, 1 missing) | 30 | 1 | 7.5 | 3.5 | 18.5 | 1 | 60 |
| DEAD (n=33) | 33 | 0 | 10.0 | 3.0 | 23.0 | 1 | 60 |

Median gap −2.5 (higher in DEAD, i.e. MOVED calls were slightly fresher on
average). IQR overlap fraction **0.75** — heavy overlap.

### `bundled_pct`

| | present | missing | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| MOVED (n=31) | 31 | 0 | 9.0 | 5.0 | 14.0 | 0 | 22 |
| DEAD (n=33) | 33 | 0 | 12.0 | 7.0 | 17.0 | 1 | 28 |

Median gap −3.0 (higher in DEAD). IQR overlap fraction **0.58** — still
substantial overlap, but the **least** of the five fields, i.e. the
best-separating one in this dataset. Section 4/6 quantify what that's worth.

---

## 3. Step 2 — cut-off sweep

Nine decile-based cut-offs per field (10th–90th percentile of that field's own
observed values — grounded in the real data, not an invented grid). "Kept P&L" is
the total return of exactly the positions that pass the filter, at 2500bps
both legs + priority fee, using the identical model and constants
`exit_analysis.py` uses (imported directly, not re-derived, so this is
guaranteed consistent, not just similar). **Baseline, no filter: −9.5896 SOL.**

### `pcr` (keep if `pcr >=` cutoff)

| cutoff | dead removed | movers lost | kept P&L |
|---:|---:|---:|---:|
| 0.092 | 4 | 3 | −8.7682 |
| 0.147 | 7 | 6 | −8.0377 |
| 0.178 | 12 | 7 | −7.0535 |
| 0.232 | 13 | 12 | −6.4740 |
| 0.268 | 19 | 12 | −5.2205 |
| 0.319 | 21 | 16 | −4.5542 |
| 0.378 | 22 | 21 | −3.9987 |
| 0.410 | 26 | 23 | −2.9549 |
| 0.447 | 28 | 27 | −1.8485 |

Best ratio in this sweep: 0.178 (12 dead removed for 7 movers lost) — roughly
1.7:1, not close to a clean filter.

### `gt_score` (keep if `gt_score <=` cutoff)

| cutoff | dead removed | movers lost | kept P&L |
|---:|---:|---:|---:|
| 1.1 – 1.9 (all nine decile points) | 17 | 17 | 20 | −3.7802 |

**Degenerate sweep, reported honestly rather than padded to look like nine data
points:** `gt_score` only takes the values 1 and 2 in this dataset, so every
decile cut-off between 1 and 2 produces the *identical* filter (keep only
1-star calls). There is exactly one real cut-point here, not nine, and it removes
17 of 33 dead alongside 20 of 31 movers — worse than proportional.

### `holders` (keep if `holders >=` cutoff)

| cutoff | dead removed | movers lost | kept P&L |
|---:|---:|---:|---:|
| 239.7 | 5 | 1 | −8.4898 |
| 254.4 | 8 | 5 | −7.3095 |
| 264.1 | 9 | 10 | −7.1676 |
| 271.8 | 14 | 11 | −6.0543 |
| 297.5 | 20 | 14 | −4.4643 |
| 311.4 | 23 | 17 | −3.6972 |
| 323.7 | 24 | 22 | −2.9021 |
| 385.8 | 27 | 25 | −2.3796 |
| 406.0 | 30 | 28 | −0.9413 |

Best ratio: 239.7 (5 dead removed for 1 mover lost), but at very small scale —
barely touches the DEAD pile.

### `age_minutes` (keep if `age_minutes <=` cutoff; n=63, 1 missing excluded)

| cutoff | dead removed | movers lost | kept P&L |
|---:|---:|---:|---:|
| 3.4 | 23 | 23 | −3.2277 |
| 5.8 | 20 | 20 | −4.4927 |
| 8.2 | 19 | 15 | −5.3490 |
| 11.2 | 16 | 13 | −6.1484 |
| 14.0 | 11 | 9 | −7.3551 |
| 20.4 | 9 | 8 | −7.7222 |
| 28.6 | 8 | 6 | −7.6432 |
| 33.6 | 5 | 5 | −8.1917 |
| 49.6 | 4 | 3 | −8.5361 |

No cut-off separates cleanly — dead removed and movers lost move together
almost 1:1 throughout.

### `bundled_pct` (keep if `bundled_pct <=` cutoff)

| cutoff | dead removed | movers lost | kept P&L |
|---:|---:|---:|---:|
| 2.4 | 32 | 27 | −0.4254 |
| 5.4 | 29 | 20 | −1.7158 |
| 7.6 | 24 | 17 | −3.0551 |
| 9.8 | 20 | 15 | −3.8869 |
| 12.0 | 15 | 10 | −5.5157 |
| 14.2 | 13 | 8 | −6.0125 |
| **16.4** | **10** | **3** | **−6.9720** |
| 18.6 | 5 | 3 | −8.1164 |
| 21.8 | 3 | 1 | −8.8025 |

**This is the single most favourable-looking point in the entire sweep across
all five fields: `bundled_pct <= 16.4` removes 10 of 33 dead for 3 of 31 movers
lost** (a ~3.3:1 ratio), improving kept P&L from −9.59 to −6.97 SOL. It is the
same field that showed the strongest (still weak) separation in Step 1 — this is
one observation confirming itself, not independent corroboration. **No cut-off on
any field approaches the brief's own illustrative bar of "20 dead removed for 3
movers lost."**

---

## 4. Step 3 — sample size honesty

MOVED n=31, DEAD n=33, 64 positions total, spanning 7–25 Aug 2026 (~18 days).
**This is a small sample by any standard for detecting a moderate effect.**

Best-separating field by IQR overlap: **`bundled_pct`** (overlap 0.58, median
gap −3.0). A two-sample permutation test on the median gap (10,000 reshuffles of
the MOVED/DEAD labels, fixed seed 20260828, fully reproducible on re-run):

- observed |median gap| = 3.000
- **p = 0.2766** — over a quarter of random relabellings of these same 64
  positions produce a gap at least this large by chance alone.
- Five fields were examined, so p<0.05 unadjusted is not the right bar (same
  multiple-comparison logic `pcr_analysis.py` already applies elsewhere in this
  project). Adjusted: p<0.010. **0.2766 does not come close to clearing it.**

**In plain terms: this cannot be distinguished from noise at the current sample
size.** A formal test was run because n=31/33 is large enough for a
distribution-free permutation test to be meaningful (not so small that a formal
test is inappropriate on its face), but the result itself says the same thing the
informal read of the overlapping IQRs already suggested.

**How many more positions would help:** a rough guide — the observed gap (3
percentage points of `bundled_pct`, against interquartile spreads of roughly
9–10 points in each group) is a small effect relative to the spread. Detecting an
effect that size with reasonable confidence typically needs several hundred
observations per group under a two-sample test, not the 31/33 available.
Concretely: at the current effect size, doubling or even quadrupling the sample
(to roughly 120–250 positions) would likely still leave this underpowered: this
is a rough order-of-magnitude statement, not a formal power calculation, stated
in that spirit deliberately rather than dressed up as more precise than it is.

---

## 5. Step 4 — call source findings

`data/calls.jsonl`: 61 of 64 positions have a matching `bought` record (3 don't —
GILF, Ratatouille, RODRI, all pre-`data_logger.py`, excluded from this section
only). MOVED (covered) n=28, DEAD (covered) n=33.

| Field | MOVED median | MOVED p25/p75 | DEAD median | DEAD p25/p75 |
|---|---:|---|---:|---|
| `call_mc` | $36,500 | 35,850 / 37,600 | $35,700 | 34,700 / 36,600 |
| hour of day (UTC) | 7.5 | 2.0 / 13.5 | 8.0 | 5.0 / 12.0 |
| day of week (0=Mon) | 0.0 | 0.0 / 6.0 | 0.0 | 0.0 / 6.0 |

None of these show any meaningful separation — `call_mc` medians are $800 apart
on a ~$35–37K base, hour-of-day medians are half an hour apart with heavily
overlapping quartile ranges, and day-of-week is identical at the median for both
groups. Jupiter detail fields (`top_holders_pct`, `organic_score`,
`dev_migrations`, `dev_mints`, `liquidity`, `launchpad`, `live_holder_count`) are
null on all 70 `calls.jsonl` records and were not treated as data, per
instruction.

---

## 6. Does anything recorded at entry distinguish the two groups?

**No.** Every one of the five candidate fields, plus every call-record field
checked in Step 4, shows either no separation (`gt_score`, day-of-week) or
separation weak enough to overlap heavily between groups (`pcr`, `holders`,
`age_minutes`, `call_mc`, hour-of-day). The one field with the least overlap,
`bundled_pct`, still fails a permutation test by a wide margin (p=0.28 against a
0.01 bar), and its own best cut-off in the Step 2 sweep — the most favourable
point found anywhere in this entire analysis — removes only 10 of 33 dead
positions at a cost of 3 of 31 movers, nowhere near the brief's own illustrative
bar for "a filter worth having."

The 2x secondary split (section 1) doesn't change this conclusion — moving 6
positions from MOVED to DEAD shifts group sizes but there is no reason from
anything above to expect the entry fields would separate any better at that
line; re-running the same tests at 2x was not requested and was not done, to
avoid multiplying comparisons further without a stated reason.

---

## 7. What would need to be captured at entry to make this answerable

Named field by field, since the current five don't discriminate:

- **Liquidity depth at call time.** Not currently captured at all — the Stage 2
  Jupiter field-capture code (`market_data.fetch_token_details`) already defines
  a `liquidity` column, but it is null on every record because no live buy has
  run since that code was merged. This is the single most likely candidate:
  thin liquidity is a standard mechanical reason a coin can't sustain a move
  regardless of call quality, and it isn't represented by any of the five fields
  tested here.
- **Top-holder concentration at call time** (`top_holders_pct`) — same
  situation: defined, captured in code, populated with nulls only because no
  real buy has occurred since the merge.
- **Organic score / launchpad** — same: defined, not yet populated.
- **Something distinguishing WHERE the call sits in its own trajectory at the
  moment of the call** (e.g. seconds since last discernible price change, or
  whether the market cap was still rising vs already rolling over) — not
  captured by any field today, and not addressed by the Jupiter fields either.
  This would need a genuinely new field, not just waiting for the existing
  null columns to populate.
- **A larger sample, full stop.** Even a perfect new field cannot be evaluated
  with confidence at n≈32/32 per group. Section 4's answer holds regardless of
  which fields eventually get logged, until the sample grows substantially.

---

## 8. Assumptions

1. MOVED/DEAD labels use `peak_mc / entry_mc` from `positions.json` directly —
   no reconstruction needed, these fields are exact on all 64 positions.
2. Step 2's sweep direction per field (`>=` or `<=`) was chosen to favour
   MOVED's observed median — i.e., each field was tested in the direction Step
   1 already showed it leaning, not both directions. This is stated as a
   methodology choice, not hidden: a field could in principle separate better
   in the opposite direction, but nothing in Step 1's results suggested that for
   any of the five.
3. Cut-offs swept are the 10th–90th percentile of each field's own observed
   values (deciles) — grounded in real data, not an arbitrary invented grid.
   For `gt_score`, this produces a degenerate sweep (reported as such in
   section 3) because the field only takes two values in this dataset.
4. Step 2's P&L model is imported directly from `exit_analysis.py`
   (`build_sample`, the same 2500bps/priority-fee slippage constants) rather
   than reimplemented, so it is guaranteed identical, not just similarly
   written. The same caveat `exit_analysis.py` stated applies here too: GILF
   and Ratatouille have zero `data/fills.jsonl` coverage, so their exact
   sell-transaction fee count is not counted in any "kept P&L" figure that
   includes them — a small, quantified underestimate of total fees, as before.
5. The permutation test (section 4) used a fixed random seed (20260828) so the
   reported p-value is exactly reproducible on re-run, not a fresh random draw
   each time.
6. No field was imputed for the one missing `age_minutes` value or the three
   missing `calls.jsonl` records — both were excluded pairwise from the
   specific comparisons that needed them, per instruction.

---

## 9. File hashes — confirming read-only

**Before:**
```
logs\positions.json: 2b7dd4920cc7e389c38d7a983f3395de
data\calls.jsonl:    caebcfe8451f8c0969b88597a91fbe82
data\fills.jsonl:    5fb59cbfecc59b37076a3b56c8b39956
data\snapshots.jsonl: 81af1cf8f6345d470bc8b86065c60f6b
```

**After (identical):**
```
logs\positions.json: 2b7dd4920cc7e389c38d7a983f3395de  [OK]
data\calls.jsonl:    caebcfe8451f8c0969b88597a91fbe82  [OK]
data\fills.jsonl:    5fb59cbfecc59b37076a3b56c8b39956  [OK]
data\snapshots.jsonl: 81af1cf8f6345d470bc8b86065c60f6b  [OK]
```

---

## 10. Exact command to re-run

```
cd C:\projects\solana_trading_bot
venv\Scripts\python.exe src\entry_analysis.py
```

Read-only and deterministic (fixed permutation-test seed) — re-running produces
identical output unless `logs/positions.json` or `data/calls.jsonl` have changed.
