# Resize Report — Stage 4: 2-3 SOL Wallet

Branch `stage4-resize`, created from `stage3-small-wallet`. Not committed to
`main`, not pushed. **DRY_RUN confirmed true in both `.env` and
`.env.example`** — see section 8.

---

## 1. Every file changed

| File | Before | After | What changed and why |
|---|---:|---:|---|
| `.env` | 12 lines | 15 lines | `MIN_LOT_SOL`, `MIN_BUY_SOL` re-asserted at 0.075 (unchanged); `MAX_LOT_SOL`, `MAX_POSITION_SOL` updated to 0.25; `MAX_CONCURRENT_POSITIONS` updated to 10; `MIN_SOL_RESERVE`/`SLIPPAGE_BPS`/`PRIORITY_FEE_LAMPORTS`/`DRY_RUN` re-asserted unchanged. Edited by exact-key shell substitution — never read. |
| `.env.example` | 16 | 16 | Same nine values, mirrored. |
| `tests/test_safety.py` | 403 | 409 | The three Stage 3 sizing tests **updated in place** (not duplicated) for the new numbers — 2.5 SOL wallet, 10 concurrent positions, `MAX_POSITION_SOL=0.25`, `MAX_LOT_SOL=0.25`. The seven Stage 1 tests above them are untouched. |

**No source file needed changing.** `src/config.py`, `src/entry_logic.py` and
`src/runner.py` are all confirmed byte-identical to `stage3-small-wallet`
(`git diff --stat`, no output for any of the three) — every value this stage
touches already had validated plumbing from Stage 3, exactly as the brief
expected ("this should be values only, no new plumbing"). `exit_logic.py`,
`trade_execution.py`, `wallet.py`, `market_data.py`, `exit_analysis.py` and
`entry_analysis.py` are likewise untouched.

**Conflict flagged, not silently assumed correct:** the brief asked me to
verify (not assume) that Stage 3's `runner.py` stale-version guard, hardcoded
to expect `MIN_BUY_SOL == 0.075`, is still correct now that `MIN_BUY_SOL`
stays at 0.075 this stage too. Verified two ways: read the guard directly
(`runner.py:1123`, still `!= 0.075`), and confirmed end-to-end by importing
`config`/`entry_logic` fresh against the new `.env` — `entry_logic.MIN_BUY_SOL
== 0.075 == True`. The guard is correct; no code change was needed or made.

---

## 2. Full `.env.example` contents

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHANNEL=
WALLET_PRIVATE_KEY=
HELIUS_RPC_URL=
JUPITER_API_KEY=

DRY_RUN=true
MAX_POSITION_SOL=0.25
MAX_CONCURRENT_POSITIONS=10
MIN_SOL_RESERVE=0.05
SLIPPAGE_BPS=2500
PRIORITY_FEE_LAMPORTS=125000
MIN_LOT_SOL=0.075
MAX_LOT_SOL=0.25
MIN_BUY_SOL=0.075
```

---

## 3. Verbatim test output

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
tests/test_safety.py::test_ten_positions_at_min_lot_fit_a_2_5_sol_wallet_eleventh_refused PASSED [ 80%]
tests/test_safety.py::test_dca_fill_refused_if_it_would_exceed_max_position_sol PASSED [ 90%]
tests/test_safety.py::test_no_lot_below_min_lot_sol PASSED               [100%]

============================= 10 passed in 1.36s ==============================
```

**`python tests/run_all.py`:**
```
==============================================================================
RUNNING ALL CHECKS
==============================================================================
compile - every source file          OK
self-test - parser                   OK
self-test - entry_logic              OK
self-test - exit_logic               OK
self-test - trading_window           OK
self-test - market_data              OK
build analysis fixtures              OK
integration - reject paths (entry guards)      OK
integration - stage 2 field plumbing           OK
integration - analysis chain                   OK
integration - end to end                       OK
self-test - data_logger (isolated)             OK
==============================================================================
ALL CHECKS PASSED
==============================================================================
```

**No workaround fix was needed** — `test_reject_paths.py`, `test_jupiter_fields.py`
and `test_end_to_end.py` still override `MAX_POSITION_SOL` to 999.0 from Stage
1/3, comfortably above the new 0.25, so nothing broke. Verified by actually
running the suite.

One real bug caught by actually running the tests, not assumed away: my first
draft of the new 10-position test used letter suffixes `A`-`J` for synthetic
contract addresses, not noticing `I` is *also* excluded from base58 (alongside
`0`, `O`, `l`) — the same class of mistake Stage 3's tests hit with a trailing
`0`. `parser.py` correctly refused to classify the resulting text as a call,
and entry 9 of 10 failed with a clear assertion error rather than silently
mis-passing. Fixed by skipping `I` (`ABCDEFGHJK`).

`logs/positions.json` (64 positions) and `data/calls.jsonl` (70 records)
confirmed unchanged before and after every run this stage.

---

## 4. Step 2: DCA behaviour at the new sizing

From `entry_logic.py`'s own self-test (not hand-copied):

```
three-stage needs a lot of at least 0.300 SOL
two-stage   needs a lot of at least 0.187 SOL
```

`MAX_LOT_SOL` is now 0.25. **Three-stage DCA remains unreachable** — 0.300 SOL
exceeds the ceiling at any PCR, exactly as at Stage 3. **Two-stage DCA is
reachable again** — 0.1875 SOL sits below the new 0.25 ceiling, unlike Stage 3
where the whole 0.075–0.15 range sat below it. Solving for the PCR at which a
call first clears the two-stage threshold: a lot of 0.1875 SOL requires
`stretch_pcr = (0.1875-0.075)/(0.25-0.075) = 0.6429`, i.e. **PCR ≈ 0.421 or
higher** clears it (using the unchanged `PCR_STRETCH_LO/HI` of 0.10/0.60).

**Data availability note on the 18-position figure:** the brief asks how many
of "the 18 positions in the 27 Aug 18:00-23:23 VPS log" would get one tranche
vs two. I do not have a local copy of that log, and connecting to the VPS to
fetch it is against every brief's standing constraint (confirmed explicitly
declined twice already this session) — so I have not invented per-position
data to answer this precisely. **However, the question is answerable exactly
from the stated PCR range alone (0.055 to 0.308), without needing the
individual 18 values**, because lot size is a monotonic function of PCR: the
*highest* PCR in the range determines the *largest* possible lot, and if even
that falls short of the two-stage threshold, every position in the range does
too.

```
lot(pcr=0.308) = 0.075 + stretch(0.308) x (0.25-0.075)
               = 0.075 + 0.416 x 0.175
               = 0.1478 SOL
```

0.1478 SOL is below the 0.1875 SOL two-stage threshold. **All 18 positions in
the stated range would receive exactly one tranche. Zero would get two. Zero
would get three.** This holds regardless of where within 0.055–0.308 each
individual call's PCR actually falls — I don't need the per-call breakdown to
know this, only the range, which the brief itself provided.

**Three-stage DCA is not reachable at this sizing, at any PCR from 0 to 1.**

---

## 5. Which guard binds first at 2.5 SOL

**The concurrency cap (`MAX_CONCURRENT_POSITIONS=10`), not the reserve
check** — same shape of result as Stage 3, proven the same way: the mocked
`get_balance()` sequence has exactly 10 values; if the 11th attempt's reserve
check were ever reached, the mock would be called an 11th time and raise. It
doesn't — `get_balance.call_count` stays at 10, and the 11th attempt's
`data/calls.jsonl` record is `rejected_concurrency_cap`.

At MIN_LOT_SOL-sized (0.075) positions on a 2.5 SOL wallet this isn't close: 10
positions cost 0.75 SOL, leaving 1.75 SOL — nowhere near the 0.05 reserve. The
reserve check would only start to matter at much larger lot sizes; see section
6's 2 SOL/3 SOL comparison for where it actually does.

---

## 6. Cost table at the new sizing

Same model as Stage 3 (`SIZING_REPORT.md` §6): buy-side slippage scales tokens
received by `1/(1+slip)`, sell-side scales proceeds by `(1-slip)`; combined, a
flat-price round trip returns `(1-0.25)/(1+0.25) = 0.6` of nominal, regardless
of lot size — slippage drag is proportional, fixed drag (rent + fees) is not.

| | 0.075 SOL lot | 0.25 SOL lot |
|---|---:|---:|
| Rent (0.002 SOL) | 2.6667% | 0.8000% |
| Priority fees, entry+exit (0.00025 SOL) | 0.3333% | 0.1000% |
| **Fixed drag** | **3.0000%** | **0.9000%** |
| Slippage, both legs, flat round trip | 40.0000% | 40.0000% |
| **Total drag on a flat round trip** | **43.0000%** | **40.9000%** |
| **Break-even multiple** | **1.71667x (+71.67%)** | **1.68167x (+68.17%)** |

The larger lot dilutes the *fixed* costs (rent, fees) across more capital, so
its break-even is slightly easier — but slippage, which dominates both
figures, is scale-invariant: it costs the same 40% of the lot whichever size
is used. Sizing up does not meaningfully change the fundamental economics at
2500 bps; it only shaves about 3.5 percentage points off the break-even
requirement.

**Total maximum exposure: 10 × 0.25 = 2.5 SOL.**

| Wallet | Positions before reserve check would bind (worst case, all at `MAX_LOT_SOL`) | Which guard binds first |
|---|---:|---|
| 2.0 SOL | 7 | **Reserve check** — blocks the 8th attempt, well before the concurrency cap (10) is ever reached |
| 3.0 SOL | 10 | **Concurrency cap** — all 10 fit with 0.5 SOL to spare; reserve would only start to bind at an 11th (already blocked by concurrency) |

Worst-case assumption stated plainly: this table assumes every position sizes
at the maximum (PCR=1 for all ten) — the most capital-hungry case. Real PCR
varies, so in practice more than 7 positions would typically fit on a 2 SOL
wallet before the reserve binds; this is the conservative bound, not a
typical-case estimate.

A 2.0 SOL wallet **cannot** open all 10 positions at maximum size even in
principle (10 × 0.25 = 2.5 > 2.0) — the reserve check is doing real work there,
not just a formality. A 3.0 SOL wallet comfortably covers the full 10-position,
max-size exposure with headroom to spare.

---

## 7. Step 6: reserve adequacy at 10 positions

`MIN_SOL_RESERVE` stays at 0.05 (not changed — informational only, per
instruction).

| | SOL |
|---|---:|
| Token account rent × 10 positions (0.002 each) | 0.02000 |
| Priority fees × 10 exits (0.000125 each) | 0.00125 |
| **Total claim against the reserve** | **0.02125** |
| Reserve floor | 0.05000 |
| **Headroom remaining** | **0.02875 (57.5%)** |

**0.05 is adequate at 10 positions**, though with a smaller margin than the
original basis: the reserve was first sized (Stage 1) against 6 positions,
where the equivalent claim was 0.01275 SOL — 25.5% of the floor, 74.5%
headroom. At 10 positions the claim rises to 0.02125 SOL — 42.5% of the floor,
57.5% headroom. Utilization nearly doubled Stage 1's estimate but the floor
still comfortably covers it; "tight" would mean utilization approaching 100%,
which this is not. Not a change to make, per instruction — reported as
information only.

---

## 8. DRY_RUN confirmation

**Confirmed true in both files.**
- `.env`: `grep -q "^DRY_RUN=true$" .env` — exit 0. Contents never read or
  displayed.
- `.env.example`: `DRY_RUN=true`, visible directly in section 2.

Nothing in this stage set, read, or referenced `DRY_RUN=false`.

---

## 9. Changed beyond this brief

Nothing beyond the letter suffix fix noted in section 3 (a test-fixture bug
caught while running the suite, not a behavioural change). No source file
needed touching this stage — genuinely values-only, as the brief expected.

---

## 10. Anything not completed

The individual per-call breakdown for the 18-position VPS-log sample (Step 2)
was not producible, because I don't have that data and won't fetch it from
the VPS. The question itself **was** answered exactly, from the stated range
alone — see section 4 for why the range is sufficient and no per-position data
was actually needed to give a definitive answer.

---

## 11. Conflicts with existing code or with stage 3's decisions

**Stage 3's "DCA cannot fire at all" finding is now partially reversed.**
`SIZING_REPORT.md` reported that at `MAX_LOT_SOL=0.15`, neither a two-stage
nor three-stage split could ever fire — DCA was completely dead at any
conviction level. At this stage's `MAX_LOT_SOL=0.25`, **two-stage DCA is
reachable again**, for calls scoring PCR ≥ ~0.421 (section 4). Three-stage
remains unreachable regardless (needs 0.30 SOL, still above the 0.25 ceiling).
This isn't a contradiction of Stage 3's report — it was correct for the sizing
in force at the time — but it is a real, stated reversal of that specific
finding under the new numbers, flagged explicitly as instructed rather than
left for you to notice by cross-referencing two documents.

**The stale-version guard verification (section 1)** is the other item the
brief specifically asked not to be assumed — verified, confirmed correct, no
conflict found.

No other conflicts found with `stage3-small-wallet`'s decisions or with the
existing code.

---

## 12. Exact command to run locally in dry run

```
cd C:\projects\solana_trading_bot
venv\Scripts\python.exe src\runner.py
```

`DRY_RUN=true` in `.env` (confirmed section 8) — no transaction will be signed
or submitted regardless of what else runs.
