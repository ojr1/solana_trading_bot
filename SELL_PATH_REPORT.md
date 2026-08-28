# Sell Path Report — Stage 12

Branch `stage12-wire-sell`, from `stage11-verify`. Not merged, not pushed. VPS not touched.
`DRY_RUN` unchanged (still `true`). **No transaction was signed or submitted** — every
mocked test in this stage stubs `build_signed_transaction()`/`submit_transaction()` directly;
confirmed in detail in section 6.

---

## 1. Direction-handling choice, and why

**Chose a `direction` parameter on `execute_swap()`** (`direction="buy"` default,
`direction="sell"`), not a separate `execute_swap_sell()`.

`execute_swap()`'s signature changed from `(output_mint, amount_sol, slippage_bps=None,
priority_fee_lamports=None)` to `(mint, amount, direction="buy", decimals=None,
slippage_bps=None, priority_fee_lamports=None)`. This is **fully backward compatible**: both
existing callers (`test_swap.py`, the pre-Stage-12 tests) invoke it positionally
(`execute_swap(mint, 0.05)`), and Python binds positional arguments by position, not name, so
the rename costs nothing. Confirmed by `test_execute_swap_buy_direction_unchanged_and_labelled`.

**Why one function over two:** everything *after* the quote step — the `DRY_RUN`
short-circuit, local signature capture, `submit_transaction()`, the three-way
`confirm_transaction()` outcome, and all three exception types
(`FillNotConfirmedError`/`TransactionRevertedError`/`TransactionSubmissionError`) — is
identical machinery regardless of which direction the trade is. A separate
`execute_swap_sell()` would have to duplicate all of that, and duplicated code drifts: this
project has already been burned once by exactly that failure mode (`runner.py`'s own startup
guard comment references commit `666267d`, where a strategy fix "claimed all three files but
only entry_logic.py reached disk"). A single function with a `direction` branch only at the
quote-building step means a future fix to, say, the submission-retry policy applies to both
directions automatically, by construction, not by remembering to apply it twice.

The only place direction actually matters is choosing `get_quote()` vs. `get_quote_sell()`
(and, for sells, resolving `decimals` first) — everything downstream of "I have a quote" was
already direction-agnostic before this stage, since `build_signed_transaction()` just signs
whatever transaction Jupiter's `/swap` endpoint returns for the quote it's given.

---

## 2. Decimals caching and failure-handling decision (flagged for review)

**Caching:** `_decimals_cache`, a module-level `dict` in `trade_execution.py`, keyed by
mint address. `get_token_decimals()` checks it first; a hit skips the network call entirely.

**Not persisted to disk, and no new `.env` value added — deliberately, for one reason:** a
mint's decimals is a fixed property of its on-chain mint account, set once at creation and
**never** changed afterward. There is no staleness to guard against, so there is nothing a
TTL or an invalidation policy would protect — an in-memory cache lasting the process's own
lifetime is already exactly as correct as a persisted one, for free, with no new file to
manage and no cache-corruption failure mode to design around. A fresh process restart simply
re-populates lazily on first need, which costs one network round-trip per mint the bot
touches — not worth trading for the complexity of a second persistence mechanism. No `.env`
value was needed because there is no policy to tune (no TTL, no size cap — the practical
number of distinct mints a memecoin bot trades in one process's lifetime is small).

**Failure handling — the part I'm flagging for your review, per the brief's explicit
instruction:**

The brief is right that this matters more than it might look: a sell is how the bot gets
*out* of a losing or time-pressured position, and the decimals lookup sits directly in that
path. I considered three options:

1. **Guess a default (e.g. assume 6, since most memecoins use it).** Rejected outright — this
   is exactly the "highest-consequence bug class" this whole plan has been built around
   avoiding. A wrong guess doesn't fail loudly; it sends an amount off by one or more orders
   of magnitude to a *real* swap.
2. **Retry harder, longer, before giving up.** `_request_with_retries()` already does 3
   bounded attempts with backoff; stacking more retries on top only delays the outcome, it
   doesn't change what happens when they're all exhausted.
3. **Abort the sell attempt entirely — implemented, the conservative option.** If `decimals`
   isn't supplied and the lookup fails (`DecimalsLookupError`, or any other exception), the
   sell is abandoned for that attempt: no quote is requested, nothing is signed, the position
   stays exactly as it was. This is logged at **ERROR**, not a swallowed warning
   (`"SELL ABORTED for %s: could not resolve token decimals..."`), because a silently-skipped
   exit is itself a real failure mode worth being loud about.

**The tradeoff I want your read on:** option 3 means a position that should have exited stays
open and exposed to the market for at least another cycle, every time this specific lookup
fails — versus the alternative of ever letting a swap proceed on a guessed decimals value,
which risks a badly-sized (or outright failing, in the safer case) real transaction. I judged
continued-but-correctly-priced exposure as clearly the lesser risk of the two, but this is a
risk-tolerance call, not a purely technical one, and I'd rather you confirm it than have it
buried in a commit message. **Not wired into `runner.py`** (per hard constraint 4), so this
decision has no live effect yet — it only governs what `execute_swap(..., direction="sell")`
itself does when called, which nothing does yet outside tests.

One related design choice: **a caller can bypass the lookup (and the cache) entirely by
passing `decimals` explicitly** — useful once a future stage wires this in and already has a
trusted, freshly-parsed decimals value for the same mint (e.g. from an earlier
`parse_fill_from_transaction()` call in the same position's lifecycle) and doesn't need to
ask again. Tested by `test_execute_swap_sell_uses_supplied_decimals_without_lookup`.

---

## 3. What I found on `wrapAndUnwrapSol`

`build_signed_transaction()` sends `"wrapAndUnwrapSol": True` **unconditionally** — this was
already true before Stage 12 and is unchanged, since `build_signed_transaction()` is shared
by both directions and never branches on direction at all.

**Reasoning (not executed, see below):** per Jupiter's documented `/swap` API behaviour, this
flag governs automatic SOL↔WSOL wrapping on *whichever* side of the swap is native SOL —
symmetrically for both directions. For a buy (SOL→token), the input is native SOL and the
flag governs auto-wrapping it into WSOL before the swap. For a sell (token→SOL), the *output*
is native SOL and the flag governs auto-*un*wrapping the resulting WSOL back into native SOL
for us at the end — which is exactly the behaviour wanted: receiving spendable native SOL
directly, not a WSOL token-account balance that would need a separate close/unwrap step
later. Nothing in the documented API surface suggests this flag needs a different value, or
any other parameter needs setting, specifically for the sell direction.

**I did not verify this by executing anything.** I considered building a real *unsigned*
sell transaction via Jupiter's `/swap` endpoint (which itself signs nothing) to inspect its
actual instruction list for a WSOL-close instruction — but decided against it: doing so would
send our real wallet's real public key to Jupiter's backend tied to a specific mint and
amount, a step beyond what "quotes and read-only lookups" was clearly written to bless, for a
question reasoning from documented behaviour already answers with reasonable confidence.
Given the hard constraint's emphasis on caution here, I chose the more conservative reading
rather than stretch the permission.

**No code change was made.** My conclusion — `wrapAndUnwrapSol: True` is already correct for
both directions — is **reasoned, not verified**, and is marked UNVERIFIED accordingly (section
7). It is the single most important thing to check on the very first real sell (section 8).

---

## 4. Verbatim test output

### `pytest tests/test_safety.py -v`

```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- ...\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\projects\solana_trading_bot
collecting ... collected 72 items

tests/test_safety.py::test_reserve_check_blocks_when_below_floor PASSED  [  1%]
tests/test_safety.py::test_reserve_check_allows_when_exactly_at_floor PASSED [  2%]
tests/test_safety.py::test_reserve_check_allows_when_above_floor PASSED  [  4%]
tests/test_safety.py::test_reserve_block_emits_warning PASSED            [  5%]
tests/test_safety.py::test_same_ticker_lock_blocks_duplicate_concurrent_entry PASSED [  6%]
tests/test_safety.py::test_unconfirmed_fill_is_never_treated_as_filled PASSED [  8%]
tests/test_safety.py::test_reverted_fill_raises_transaction_reverted_not_treated_as_filled PASSED [  9%]
tests/test_safety.py::test_confirmed_fill_returns_success PASSED         [ 11%]
tests/test_safety.py::test_submission_failure_carries_local_signature_forward PASSED [ 12%]
tests/test_safety.py::test_confirm_transaction_null_err_is_success PASSED [ 13%]
tests/test_safety.py::test_confirm_transaction_absent_err_is_success PASSED [ 15%]
tests/test_safety.py::test_confirm_transaction_non_null_err_is_failed PASSED [ 16%]
tests/test_safety.py::test_confirm_transaction_never_resolves_is_timeout PASSED [ 18%]
tests/test_safety.py::test_dry_run_never_reaches_submission PASSED       [ 19%]
tests/test_safety.py::test_ten_positions_at_min_lot_fit_a_2_5_sol_wallet_eleventh_refused PASSED [ 20%]
tests/test_safety.py::test_dca_fill_refused_if_it_would_exceed_max_position_sol PASSED [ 22%]
tests/test_safety.py::test_no_lot_below_min_lot_sol PASSED               [ 23%]
tests/test_safety.py::test_config_loads_with_dry_run_true_and_no_wallet_key PASSED [ 25%]
tests/test_safety.py::test_config_raises_when_dry_run_false_and_no_wallet_key PASSED [ 26%]
tests/test_safety.py::test_importing_wallet_does_not_construct_a_keypair PASSED [ 27%]
tests/test_safety.py::test_reserve_check_allows_entry_when_no_wallet_configured PASSED [ 29%]
tests/test_safety.py::test_price_history_written_for_each_open_position_each_cycle PASSED [ 30%]
tests/test_safety.py::test_price_history_skips_positions_before_initials PASSED [ 31%]
tests/test_safety.py::test_price_history_not_written_when_no_positions_open PASSED [ 33%]
tests/test_safety.py::test_price_history_record_contains_every_specified_field PASSED [ 34%]
tests/test_safety.py::test_price_history_write_failure_does_not_propagate PASSED [ 36%]
tests/test_safety.py::test_initials_sells_33_percent_not_50 PASSED       [ 37%]
tests/test_safety.py::test_daily_loss_cap_blocks_new_entries_when_breached PASSED [ 38%]
tests/test_safety.py::test_daily_loss_cap_allows_when_under_the_cap PASSED [ 40%]
tests/test_safety.py::test_daily_loss_cap_boundary_just_before_and_after_utc_midnight PASSED [ 41%]
tests/test_safety.py::test_daily_loss_cap_ignores_positions_without_closed_at PASSED [ 43%]
tests/test_safety.py::test_alert_logs_at_error_with_distinctive_prefix PASSED [ 44%]
tests/test_safety.py::test_plan_sell_does_not_mutate_apply_sell_does PASSED [ 45%]
tests/test_safety.py::test_plan_dca_fill_does_not_mutate_apply_dca_fill_does PASSED [ 47%]
tests/test_safety.py::test_monitor_once_skips_evaluation_for_in_flight_position_but_updates_peak PASSED [ 48%]
tests/test_safety.py::test_monitor_once_evaluates_normally_once_in_flight_clears PASSED [ 50%]
tests/test_safety.py::test_recovery_no_signature_clears_and_does_not_apply PASSED [ 51%]
tests/test_safety.py::test_recovery_confirmed_sell_applies_missed_update PASSED [ 52%]
tests/test_safety.py::test_recovery_confirmed_partial_sell_leaves_position_open PASSED [ 54%]
tests/test_safety.py::test_recovery_confirmed_buy_applies_missed_update PASSED [ 55%]
tests/test_safety.py::test_recovery_reverted_clears_without_applying PASSED [ 56%]
tests/test_safety.py::test_recovery_timeout_leaves_flag_and_alerts PASSED [ 58%]
tests/test_safety.py::test_recovery_no_in_flight_trades_is_a_no_op PASSED [ 59%]
tests/test_safety.py::test_reserved_sol_sums_only_in_flight_buys PASSED  [ 61%]
tests/test_safety.py::test_reserve_check_blocks_when_in_flight_buys_would_breach_reserve PASSED [ 62%]
tests/test_safety.py::test_reserve_check_allows_when_in_flight_buys_still_leave_room PASSED [ 63%]
tests/test_safety.py::test_parse_fill_buy_reports_tokens_received_and_sol_spent PASSED [ 65%]
tests/test_safety.py::test_parse_fill_sell_reports_tokens_sent_and_sol_received PASSED [ 66%]
tests/test_safety.py::test_parse_fill_uses_real_decimals_not_a_hardcoded_default PASSED [ 68%]
tests/test_safety.py::test_parse_fill_finds_owner_via_address_lookup_table PASSED [ 69%]
tests/test_safety.py::test_parse_fill_raises_on_reverted_transaction PASSED [ 70%]
tests/test_safety.py::test_parse_fill_raises_when_transaction_not_found PASSED [ 72%]
tests/test_safety.py::test_parse_fill_raises_when_no_matching_token_balance PASSED [ 73%]
tests/test_safety.py::test_get_token_decimals_reads_from_get_token_supply PASSED [ 75%]
tests/test_safety.py::test_get_token_decimals_raises_when_mint_not_found PASSED [ 76%]
tests/test_safety.py::test_get_quote_sell_sends_correct_direction_and_amount PASSED [ 77%]
tests/test_safety.py::test_get_quote_sell_wrong_decimals_produce_order_of_magnitude_error PASSED [ 79%]
tests/test_safety.py::test_get_quote_sell_defaults_slippage_from_config PASSED [ 80%]
tests/test_safety.py::test_parse_fill_real_transaction_no_lookup_table PASSED [ 81%]
tests/test_safety.py::test_parse_fill_real_transaction_owner_not_fee_payer PASSED [ 83%]
tests/test_safety.py::test_execute_swap_sell_dry_run_never_reaches_submission PASSED [ 84%]
tests/test_safety.py::test_execute_swap_sell_successful_chains_into_parse_fill PASSED [ 86%]
tests/test_safety.py::test_execute_swap_sell_reverted_raises_transaction_reverted PASSED [ 87%]
tests/test_safety.py::test_execute_swap_sell_timeout_raises_fill_not_confirmed PASSED [ 88%]
tests/test_safety.py::test_execute_swap_sell_submission_failure_carries_local_signature PASSED [ 90%]
tests/test_safety.py::test_execute_swap_sell_decimals_lookup_failure_aborts_before_any_quote PASSED [ 91%]
tests/test_safety.py::test_execute_swap_sell_quote_failure_raises_before_any_signing PASSED [ 93%]
tests/test_safety.py::test_execute_swap_sell_uses_supplied_decimals_without_lookup PASSED [ 94%]
tests/test_safety.py::test_execute_swap_invalid_direction_raises_value_error PASSED [ 95%]
tests/test_safety.py::test_execute_swap_buy_direction_unchanged_and_labelled PASSED [ 97%]
tests/test_safety.py::test_get_token_decimals_cache_avoids_second_network_call PASSED [ 98%]
tests/test_safety.py::test_get_token_decimals_network_failure_raises_decimals_lookup_error PASSED [100%]

============================= 72 passed in 5.20s ==============================
```

(60 tests carried over unchanged from Stage 11, plus 12 new tests this stage: Part 4's
required coverage — successful sell, reverted sell, timed-out sell, decimals lookup failure,
quote failure, DRY_RUN short-circuit — plus submission-failure, invalid-direction, buy-path
regression, supplied-decimals bypass, and two decimals-cache tests.)

### `python tests/run_all.py`

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

---

## 5. Confirmation `runner.py` is unchanged, with `git diff` evidence

```
$ git diff stage11-verify..HEAD -- src/runner.py
(empty)

$ git status --short
 M src/trade_execution.py
 M tests/test_safety.py
```

`runner.py` is byte-identical to the `stage11-verify` branch point. Only
`src/trade_execution.py` (the sell-path implementation) and `tests/test_safety.py` (the new
tests) were changed. No point in this stage's implementation needed a `runner.py` change —
the brief's hard constraint 4 asked me to stop and report if I believed otherwise, and I
don't: `execute_swap()` staying callable-but-unwired is exactly the same shape the buy path
has had since Stage 1, and wiring either direction into the monitor loop is explicitly later,
out-of-scope work per `LIVE_EXECUTION_PLAN.md`'s staged rollout.

## 6. Confirmation no transaction was signed or submitted

No test in this stage's new coverage calls the real `build_signed_transaction()` or
`submit_transaction()` — every test that reaches past the `DRY_RUN` short-circuit mocks both
directly (`AsyncMock(return_value=...)`), matching the existing buy-side test pattern exactly.
`.env`/`.env.example`: `DRY_RUN=true`, unchanged. No network call was made this stage at all
(Stage 11 already verified `get_quote_sell()`/`get_token_decimals()` against real endpoints;
this stage's testing is offline/mocked only, per Part 4's own framing — "without executing
anything").

---

## 7. Everything still UNVERIFIED, and what real test each needs

Carried over from Stage 11 (unchanged by this stage, since nothing here touched
`confirm_transaction()` or executed a fill):

- **A genuinely reverted transaction through `confirm_transaction()`/`getSignatureStatuses`
  specifically.** Needs a real trade deliberately constructed to revert.
- **A real fill from this bot's own wallet**, buy or sell, run through
  `parse_fill_from_transaction()`. Needs one real, tiny, human-confirmed swap.

New this stage:

- **The entire sell path against real execution** — `execute_swap(..., direction="sell")`
  has never been called with `DRY_RUN=false` against real Jupiter/Helius endpoints. Needs a
  real, tiny sell (section 8's proposal).
- **`wrapAndUnwrapSol` on the sell side** (section 3) — reasoned from documented behaviour,
  not executed. Needs a real sell whose resulting SOL balance is inspected directly (native
  SOL received, not a lingering WSOL token-account balance).
- **The decimals-lookup-failure abort path, under real conditions** (a real Helius outage or
  timeout at the moment of a real sell attempt) — the mocked test proves the code path
  triggers correctly, but a real network failure's exact shape/timing hasn't been observed.
  Lower priority: the conservative behaviour (abort, don't guess) doesn't depend on the exact
  failure shape to be safe.

---

## 8. Proposal: what the very first real sell should look like

**This is a proposal for your approval — nothing here has been executed.**

- **Mint:** the same mint used in `test_swap.py`'s original real-trade verification (BONK,
  `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`) is deep-liquidity and well-understood, but
  it isn't actually held in the wallet from that test (that was a buy verification only, and
  its result was never persisted). **Recommend instead:** first run a small, real,
  human-confirmed **buy** of a fixed small amount of a liquid, well-known token (BONK is a
  reasonable choice again, precisely because Stage 11 already round-tripped a real quote for
  it successfully) — this gives you an actual token balance to sell, sidesteps needing to
  correctly guess what's already in the wallet, and re-uses infrastructure already proven
  once. Then sell that exact position back.
- **Size:** the same `0.02 SOL` `test_swap.py` already used for the original buy
  verification — small enough that a worst-case sizing error (e.g. if `wrapAndUnwrapSol` or
  decimals handling has a real-world surprise this report's reasoning missed) costs at most
  that amount, and it's a number already proven safe once for a swap of this shape.
- **What to check immediately afterward, in order:**
  1. **The wallet's native SOL balance increased** by roughly the quoted amount minus fees —
     not a WSOL token-account balance sitting unspent. This is the one thing section 3
     couldn't verify without executing something, and it's the single most important check.
  2. **The signature's `getTransaction` result**, run through the real (not mocked)
     `parse_fill_from_transaction()`, and its output hand-checked against Solscan for that
     signature — this is what would finally verify Section 7's "real fill from this bot's own
     wallet" item, for both this stage's sell path and Stage 10/11's parsing work together.
  3. **The signature's `confirm_transaction()` result** specifically returned `"confirmed"`,
     not `"failed"` or `"timeout"` — confirming the three-way return works correctly for a
     sell in practice, not just in the mocked tests.
  4. If anything about steps 1–3 doesn't match expectations, **stop before attempting a
     second real sell** and treat the mismatch as a new finding to investigate, the same
     discipline used throughout this project's verification stages so far.

This deliberately does **not** propose wiring anything into `runner.py` yet — that is a
separate decision, for a later stage, once this one real sell (and, separately, a real
revert test) have both been observed to behave as expected.
