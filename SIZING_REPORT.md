# Sizing Report — Stage 3: Small-Wallet Sizing

Branch `stage3-small-wallet`, created from `stage2-exit-analysis`. Not committed to
`main`, not pushed. **DRY_RUN is still true in both `.env` and `.env.example`** —
confirmed explicitly in section 7.

---

## 1. Step 0 survey (posted and confirmed before any code was written)

Full detail was posted separately and confirmed. Summary:

- `MIN_LOT_SOL = 0.2` (`entry_logic.py:28`), `MAX_LOT_SOL = 0.5` (line 29),
  `MIN_BUY_SOL = 0.10` (line 30) — all hardcoded, none in `config.py` before this
  stage.
- Total lot: `pcr_to_lot_size()` linearly interpolates `MIN_LOT_SOL` → `MAX_LOT_SOL`
  by stretched PCR.
- Tranches: `split_into_tranches()` uses **fixed proportions**
  (`DCA_WEIGHTS_THREE = (0.45, 0.30, 0.25)`, `DCA_WEIGHTS_TWO = (0.60, 0.40)`),
  falling back a stage at a time if any resulting tranche would be below
  `MIN_BUY_SOL`, and to a single un-guarded buy if even the two-stage split fails
  that test.
- Smallest tranche under the *old* rules: exactly `MIN_BUY_SOL` (0.10 SOL), the
  hard floor the split logic is built around.
- Dependents found: `runner.py:1094` hardcoded an expectation of
  `MIN_BUY_SOL == 0.10` as a stale-file guard; no test asserted a specific
  numeric lot/tranche value tied to these constants.
- Two conflicts surfaced and resolved by your confirmation before any code was
  written: `MIN_BUY_SOL` (0.10) was larger than the proposed `MIN_LOT_SOL`
  (0.075), and `MAX_LOT_SOL` (0.5, untouched by the brief's own value list) was
  more than 3x the new `MAX_POSITION_SOL` (0.15). Resolutions: lower
  `MIN_BUY_SOL` to 0.075 and move it to config; lower `MAX_LOT_SOL` to 0.15 and
  move it to config.

---

## 2. Every file changed

| File | Before | After | What changed and why |
|---|---:|---:|---|
| `.env` | 12 lines | 15 lines | Added `MIN_LOT_SOL`, `MAX_LOT_SOL`, `MIN_BUY_SOL`; updated `MAX_POSITION_SOL` (0.4→0.15) and `MAX_CONCURRENT_POSITIONS` (6→3) in place; re-asserted `MIN_SOL_RESERVE`, `SLIPPAGE_BPS`, `PRIORITY_FEE_LAMPORTS`, `DRY_RUN` at their existing values. Edited by exact-key-name shell substitution — contents never read or displayed at any point. |
| `.env.example` | 13 | 16 | Same nine keys, mirrored with real (non-secret) values, matching `.env` exactly per file (see section 3). |
| `src/config.py` | 209 | 234 | Added `MIN_LOT_SOL`, `MAX_LOT_SOL`, `MIN_BUY_SOL` — validated, cross-checked (`MAX_LOT_SOL >= MIN_LOT_SOL`, else `ConfigError`), and logged (masked N/A — none of these are secrets) alongside the existing sizing values. |
| `src/entry_logic.py` | 501 | 511 | `MIN_LOT_SOL`/`MAX_LOT_SOL`/`MIN_BUY_SOL` are now aliases assigned from `config.*` at import time, not independent hardcoded constants — every downstream reference (`pcr_to_lot_size`, `split_into_tranches`) is unchanged. Added `import config` and one docstring sentence noting the new dependency: this module now requires a valid `.env` to import at all, since `config.py` validates at its own import time. |
| `src/runner.py` | 1157 | 1186 | (a) Updated the stale-version guard at line ~1101 from expecting `MIN_BUY_SOL == 0.10` to `== 0.075`, with a comment explaining this now also fires on a deliberate `.env` change, not only a stale file. (b) Added a **new** position-size-cap check inside `check_dca_fills()` — see section 8, this is beyond the brief's literal text but directly serves Step 2 item 4. |
| `tests/test_safety.py` | 237 | 403 | Added the six Step 2 checks as one combined scenario test (items 1/2/3/6 are one sequence, not independent) plus two focused tests (item 4: the new DCA-time cap; item 5: PCR clamping). See section 4 for verbatim results. |

No file was deleted. `exit_logic.py`, `trade_execution.py`, `wallet.py`,
`market_data.py`, `exit_analysis.py`, `entry_analysis.py`, `EXIT_ANALYSIS.md`,
`ENTRY_ANALYSIS.md` are all confirmed byte-identical to `stage2-exit-analysis`
(`git diff --stat`, no output for any of them).

---

## 3. Full `.env.example` contents

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHANNEL=
WALLET_PRIVATE_KEY=
HELIUS_RPC_URL=
JUPITER_API_KEY=

DRY_RUN=true
MAX_POSITION_SOL=0.15
MAX_CONCURRENT_POSITIONS=3
MIN_SOL_RESERVE=0.05
SLIPPAGE_BPS=2500
PRIORITY_FEE_LAMPORTS=125000
MIN_LOT_SOL=0.075
MAX_LOT_SOL=0.15
MIN_BUY_SOL=0.075
```

---

## 4. Verbatim test output

**`pytest tests/test_safety.py -v`:**
```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- ...\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\projects\solana_trading_bot
collecting ... collected 10 items

tests/test_safety.py::test_reserve_check_blocks_when_below_floor PASSED  [ 10%]
tests/test_safety.py::test_reserve_check_allows_when_exactly_at_floor PASSED [ 20%]
tests/test_safety.py::test_reserve_check_allows_when_above_floor PASSED  [ 30%]
tests/test_safety.py::test_reserve_block_emits_warning PASSED            [ 40%]
tests/test_safety.py::test_same_ticker_lock_blocks_duplicate_concurrent_entry PASSED [ 50%]
tests/test_safety.py::test_unconfirmed_fill_is_never_treated_as_filled PASSED [ 60%]
tests/test_safety.py::test_dry_run_never_reaches_submission PASSED       [ 70%]
tests/test_safety.py::test_three_positions_at_min_lot_fit_a_0_3_sol_wallet_fourth_refused PASSED [ 80%]
tests/test_safety.py::test_dca_fill_refused_if_it_would_exceed_max_position_sol PASSED [ 90%]
tests/test_safety.py::test_no_lot_below_min_lot_sol PASSED               [100%]

============================= 10 passed in 1.17s ==============================
```

**`python tests/run_all.py`:**
```
==============================================================================
RUNNING ALL CHECKS
==============================================================================

compile - every source file
   OK

self-test - parser
   OK

self-test - entry_logic
   OK

self-test - exit_logic
   OK

self-test - trading_window
   OK

self-test - market_data
   OK

build analysis fixtures
   OK

integration - reject paths (entry guards)
   OK

integration - stage 2 field plumbing
   OK

integration - analysis chain
   OK

integration - end to end
   OK

self-test - data_logger (isolated - see module docstring)
   OK

==============================================================================
ALL CHECKS PASSED
==============================================================================
```

**No workaround fix was needed.** The brief anticipated `test_reject_paths.py`,
`test_jupiter_fields.py` and `test_end_to_end.py` might break again, since they
stub the wallet balance and raise `MAX_POSITION_SOL` to 999.0 to bypass the
Stage 1 caps. They didn't: their fixture calls happen to score at a PCR that
fully clamps `stretch_pcr()` to 1.0, so their `total_lot_sol` simply changed
value (0.5 → 0.15, tracking the new `MAX_LOT_SOL`) — still far below their
hardcoded 999.0 override, so nothing broke. Verified by actually running the
suite, not assumed.

`logs/positions.json` (64 positions) and `data/calls.jsonl` (70 records)
confirmed unchanged before and after both runs.

---

## 5. Which guard binds first at 0.3 SOL

**The concurrency cap (`MAX_CONCURRENT_POSITIONS=3`), not the reserve check.**
Proven, not assumed: `test_three_positions_at_min_lot_fit_a_0_3_sol_wallet_fourth_refused`
mocks `wallet.get_balance()` with a fixed 3-value sequence (`[0.300, 0.225,
0.150]`) — if the 4th attempt's reserve check were ever reached, the mock would
be called a 4th time and raise (it has no 4th value), failing the test outright.
It doesn't: `get_balance.call_count` stays at 3, and the 4th attempt's own
`data/calls.jsonl` record is `rejected_concurrency_cap`, not `rejected_reserve`.

This is because `on_message()` checks concurrency before `open_position()` is
ever called, and `open_position()` is the only place the reserve check runs.
At these exact numbers both guards *would* have blocked the 4th entry
independently — three 0.075 SOL positions leave 0.075 SOL remaining, and a
4th would take it to exactly 0.0, breaching the 0.05 reserve too — but
concurrency fires first in the code, so the reserve check never gets the
chance to be the one that blocks it.

---

## 6. Cost at this size: a single 0.075 SOL round trip

| Item | SOL | % of lot |
|---|---:|---:|
| Token account rent | 0.002000 | 2.67% |
| Priority fee, entry + exit (2 × 0.000125) | 0.000250 | 0.33% |
| **Total fixed drag** | **0.002250** | **3.00%** |
| Slippage, both legs, 2500 bps, on a flat (0% move) round trip¹ | 0.030000 | 40.00% |
| **Total drag on a flat round trip** | **0.032250** | **43.00%** |

¹ Same model as `exit_analysis.py`/`entry_analysis.py`: buy-side slippage
scales tokens received by `1/(1+slip)`, sell-side scales proceeds by
`(1-slip)`; combined, a flat-price round trip returns
`(1-0.25)/(1+0.25) = 0.6` of the nominal amount — a 40% loss with **no price
move at all**.

**Break-even move required: the market cap must rise to ≈1.717x entry — a
+71.7% gain — before a 0.075 SOL lot returns any profit,** once rent, both
priority fees and 2500bps round-trip slippage are all accounted for. Full
arithmetic:

```
proceeds = lot × multiple × (1-slip)/(1+slip)
breakeven: proceeds = lot + entry_fee + exit_fee + rent
multiple  = (0.075 + 0.00025 + 0.002) / (0.075 × 0.6)
          = 0.07725 / 0.045
          = 1.71667x  (+71.67%)
```

This assumes token account rent is **not** recovered (a conservative,
worst-case assumption, stated plainly — in practice closing the account may
reclaim some or all of it; this analysis doesn't assume that reclaim happens).

---

## 7. DRY_RUN confirmation

**Confirmed true in both files.**
- `.env`: verified with `grep -q "^DRY_RUN=true$" .env` (exit 0) — the file's
  contents were never read or displayed to reach this confirmation.
- `.env.example`: `DRY_RUN=true`, visible directly in section 3 above.

Nothing in this stage set, read, or referenced `DRY_RUN=false` anywhere.

---

## 8. Changed beyond this brief

**Added a position-size cap inside `check_dca_fills()` (`runner.py`) that did
not exist before.** This goes beyond the brief's literal text but was needed
to make Step 2 item 4 ("a second tranche taking one coin past MAX_POSITION_SOL
is refused") actually true, rather than assumed. Before this stage, the
aggregate cap was enforced **once**, upfront, in `on_message()`, against the
*planned* total (`decision["total_lot_sol"]`) — since real tranches are just
fixed fractions of that already-approved total, they could never sum past it
under normal operation, so no second check existed. But a position **opened
under the old, larger sizing regime** could still have a pending tranche on
disk sized for that old regime; once `MAX_POSITION_SOL` drops (0.4 → 0.15,
this very stage), filling that legacy tranche could push the position over
the new, smaller cap with nothing to stop it. The new guard catches this
specific case: it compares `sol_invested + next_tranche["sol"]` against
`config.MAX_POSITION_SOL` before filling, and — since nothing about wallet
state could ever make an already-too-large tranche fit under a fixed cap —
abandons (pops) the tranche rather than leaving it to retry forever. Tested
directly in `test_dca_fill_refused_if_it_would_exceed_max_position_sol`.

This is squarely a sizing change (not exit logic, not the entry filter, not
the conviction score), consistent with constraint 4's scope, but I'm flagging
it here explicitly since it's genuinely new logic, not just a renumbered
constant.

Also beyond the brief's literal text but implied by "consistent with how
stage 1 handled every other setting" (Step 1): `entry_logic.py`'s module
docstring gained one sentence noting it now requires a valid `.env` to import.

---

## 9. Anything not completed

Nothing was left undone.

---

## 10. Conflicts with existing code, stated plainly

**DCA can no longer fire at all, at any conviction level, at this wallet
size.** Confirmed directly from `entry_logic.py`'s own self-test output:

```
three-stage needs a lot of at least 0.300 SOL
two-stage   needs a lot of at least 0.187 SOL
```

Both thresholds exceed the new `MAX_LOT_SOL` (0.15) — the absolute maximum any
call can ever size to. Every position, regardless of PCR, now becomes a
**single, unstaged buy** somewhere in [0.075, 0.15] SOL. `DCA_WEIGHTS_THREE`,
`DCA_WEIGHTS_TWO`, `DCA_DROP_STEP_PCT`, and `split_into_tranches()`'s whole
staged-entry mechanism remain fully coded and untouched (per constraint 4/5 —
I did not touch them), but they are now dead code in practice at this sizing:
no real call can ever reach the lot size their split logic requires. This
wasn't explicitly discussed in the Step 0 confirmation exchange (that covered
the `MIN_BUY_SOL` floor and the `MAX_LOT_SOL` ceiling, not this consequence of
combining both), so flagging it now rather than leaving it implicit.

**Historical re-sizing, as requested:** recomputing all 64 closed positions'
lot size under the new [0.075, 0.15] range (using each position's actual
recorded `pcr` and the unchanged `PCR_STRETCH_LO`/`PCR_STRETCH_HI`, 0.10/0.60)
against what they actually received under the old [0.2, 0.5] range:

| | |
|---|---:|
| Positions affected | **64 of 64 (all of them)** |
| New/old lot ratio | min 0.300x, median 0.331x, max 0.375x |
| Total lot committed, old range | 19.9746 SOL |
| Total lot committed, new range | 6.5935 SOL |
| **Aggregate reduction** | **67.0%** |

Every single historical position would have been sized smaller under the new
range — expected, since the new range (0.075–0.15) is uniformly a subset,
scaled to roughly a third, of the old (0.2–0.5). The ratio compresses slightly
with conviction (a PCR=0 call now sizes at `0.075/0.2 = 0.375x` its old lot; a
PCR=1 call at `0.15/0.5 = 0.300x`) because the new range is proportionally
tighter at the top than the bottom.

**No daily loss cap exists.** Per the brief's own "What this stage does NOT
do" — noted here explicitly, not implemented, not implied to exist.

---

## 11. Exact command to run locally in dry run

```
cd C:\projects\solana_trading_bot
venv\Scripts\python.exe src\runner.py
```

`DRY_RUN=true` in `.env` (confirmed section 7) — no transaction will be signed
or submitted regardless of what else runs.
