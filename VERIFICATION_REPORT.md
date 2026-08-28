# Verification Report — Stage 11

Branch `stage11-verify`, from `stage10-live-exec-foundation`. Not merged, not pushed. VPS
not touched. `DRY_RUN` unchanged (still `true`). **No transaction was signed or submitted at
any point in this run** — every real network call made was read-only (quotes, decimals
lookups, historical transaction/block reads); confirmed in detail in section 7.

---

## 1. What each part verified, with the real data used

### Part 1 — `get_token_decimals()` against real mints

Called against four real mints via the real Helius endpoint in `.env` (`getTokenSupply`,
read-only):

| mint | expected | returned | result |
|---|---|---:|---|
| SOL (wrapped, `So1111...1112`) | 9 | 9 | MATCH |
| USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) | 6 | 6 | MATCH |
| Penguin, a real memecoin from `data/calls.jsonl` (`3NwdurTg6...tWQpump`) | 6 | 6 | MATCH |
| DOGE, a real memecoin from `data/calls.jsonl` (`9upTLsrSmr...bA6gpump`) | 6 | 6 | MATCH |

All four matched. No lookup failed, nothing returned unexpectedly.

### Part 2 — `get_quote_sell()` against real Jupiter responses

Requested real sell quotes (token→SOL) for Penguin (100 tokens), DOGE (100 tokens), and USDC
(10 tokens), plus a real buy quote (SOL→Penguin, 0.05 SOL) for direct comparison. All four
returned successfully, routed through real AMMs (`Pump.fun Amm` for the two memecoins,
`BisonFi` for USDC). Full response for the Penguin sell:

```json
{
  "inputMint": "3NwdurTg6zyjLX5qwknDv5p8yLkY8MXVxBcn4tWQpump",
  "inAmount": "100000000",
  "outputMint": "So11111111111111111111111111111111111111112",
  "outAmount": "2418",
  "otherAmountThreshold": "1814",
  "swapMode": "ExactIn",
  "slippageBps": 2500,
  "platformFee": null,
  "priceImpactPct": "0.0469835063203357767411661172",
  "routePlan": [{"swapInfo": {"ammKey": "3KfBShm6...", "label": "Pump.fun Amm",
    "inputMint": "3NwdurTg6...", "outputMint": "So1111...", "inAmount": "100000000",
    "outAmount": "2418", "updateContextSlot": "442263611"}, "percent": 100, "bps": null}],
  "contextSlot": 442402558,
  "timeTaken": 0.000191445,
  "swapUsdValue": "0.000251086103980180092",
  "mostReliableAmmsQuoteReport": {"info": {"3KfBShm6...": "2418", "Czfq3xZZ...": "..."}},
  "longtailMarketQuoteReport": null, "useIncurredSlippageForQuoting": null,
  "useRewards": null, "otherRoutePlans": null, "loadedLongtailToken": false,
  "instructionVersion": null
}
```

`inAmount` was `"100000000"` for the 100-token sell at 6 decimals (`100 * 10**6`) — confirms
`get_quote_sell()`'s decimals-based amount conversion is correct against a real Jupiter
acceptance, not just against my own mocked math.

**Buy vs sell shape comparison:** identical top-level structure. The buy quote (SOL→Penguin)
carries exactly the same keys as the sell quote (`inputMint`/`outputMint`/`inAmount`/
`outAmount`/`otherAmountThreshold`/`swapMode`/`slippageBps`/`platformFee`/`priceImpactPct`/
`routePlan`/`contextSlot`/`timeTaken`/`swapUsdValue`/`mostReliableAmmsQuoteReport`/etc.), just
with `inputMint`/`outputMint` swapped and amounts reflecting the opposite direction.
**No direction-specific shape surprise** — `get_quote_sell()` is not receiving some
differently-shaped response the code doesn't expect.

### Part 3 — `parse_fill_from_transaction()` against 10 more real transactions

Widened well beyond "at least five more": fetched real signatures two ways — recent activity
against Penguin's live AMM pool (`getSignaturesForAddress`, all Jupiter-routed, all with
lookup tables), and recent activity directly against the pump.fun bonding-curve program
(mixed — some with lookup tables, some without). Ran the real `parse_fill_from_transaction()`
function (not a reimplementation) against each, using `_fetch_transaction` monkeypatched to
return the genuinely-fetched JSON (so parsing logic is real; only the network round-trip is
skipped to avoid a second live call per case):

| case | lookup table? | direction | result |
|---|---|---|---|
| penguin_pool_1 | yes | SELL | parsed correctly |
| penguin_pool_2 | yes | flat (0 delta, different mint leg) | parsed correctly |
| penguin_pool_3 | yes | SELL | parsed correctly |
| penguin_pool_4 | yes | SELL | parsed correctly |
| penguin_pool_5 | yes | SELL | parsed correctly |
| penguin_pool_6 | yes | BUY | parsed correctly |
| pumpfun_direct_1 | **no** | BUY | parsed correctly |
| pumpfun_direct_2 | yes | SELL | parsed correctly |
| pumpfun_direct_3 | yes | SELL | parsed correctly |
| pumpfun_direct_4 | yes | — | **correctly raised `FillParseError`** (genuinely reverted, `err={'InstructionError': [3, {'Custom': 3}]}`) |

**9 of 10 parsed correctly with real numbers; the 10th was a real reverted transaction and
the function correctly refused to parse it rather than returning nonsense.** Includes at
least one no-lookup-table case (`pumpfun_direct_1`) and multiple real sells
(`penguin_pool_1/3/4/5`, `pumpfun_direct_2/3`), satisfying both explicit requirements.

**Section 9's low-confidence item — wallet not the fee payer — found and tested directly.**
Two real transactions (`pumpfun_direct_2`, `pumpfun_direct_3`) track an owner
(`Hjg3mFh289u8Gcqt9wBcvPx7THKzcLRH31psyjspHckN`) who is genuinely **not** the fee payer:

```
tx2: fee_payer(index0)=B2X1KVw78Lb...  owner=Hjg3mFh289u8...  owner_index=5  is_fee_payer=False
tx3: fee_payer(index0)=2HkXSdszic...   owner=Hjg3mFh289u8...  owner_index=6  is_fee_payer=False
```

`parse_fill_from_transaction()` correctly resolved this owner's account at its real
(non-zero, non-fee-payer) index in both cases and reported `real_sol_delta` of exactly
`+1.3` and `+1.0` SOL — clean round numbers with no fee-sized remainder, which is itself
positive evidence the fee was correctly *not* attributed to this owner (the fee payer's own
account absorbed it, not this one). **This closes the low-confidence item from Stage 10's
report with real, positive evidence**, not just a synthetic test. Both cases are now
permanent regression tests (section on new tests below) — `pumpfun_direct_1` incidentally
covers the same not-fee-payer case too (owner is buyer at index 9, not 0), so the no-ALT
case and the not-fee-payer case are covered by real data simultaneously.

**Bonus, not required by this brief but directly relevant to Stage 10's still-open item:**
while hunting for transactions, `getSignaturesForAddress` against the pump.fun program
surfaced several *genuinely reverted* real transactions (`err` non-null) alongside the
successful ones — e.g. `{'InstructionError': [3, {'Custom': 3}]}` and
`{'InstructionError': [4, {'Custom': 7}]}`. This is the first real (not mocked) confirmation
of what a revert's `err` field actually looks like from a live RPC node, which Stage 10's
report flagged as needing a real trade specifically to observe. It does **not** close that
UNVERIFIED item (this brief doesn't touch `confirm_transaction()`, and the shape of `err`
via `getTransaction` isn't guaranteed identical to `getSignatureStatuses`'s `err` field,
even though both come from the same underlying execution result) — but it is one real
data point in favour of the assumption already made in Part 1's fix.

---

## 2. Anything that did NOT match the mocked assumptions

**Part 2's mocked tests (Stage 10) never modelled the response body at all** — they stubbed
`{"outAmount": "999"}` and asserted only on the outgoing request parameters
(`inputMint`/`outputMint`/`amount`/`slippageBps`). Every other field in a real Jupiter quote
response — `inAmount`, `otherAmountThreshold`, `swapMode`, `platformFee`, `priceImpactPct`,
`routePlan` (with nested `swapInfo`), `contextSlot`, `timeTaken`, `swapUsdValue`,
`mostReliableAmmsQuoteReport`, `longtailMarketQuoteReport`, `useIncurredSlippageForQuoting`,
`useRewards`, `otherRoutePlans`, `loadedLongtailToken`, `instructionVersion` — is present in
reality and absent from the mocks. **This is not a bug**: `get_quote_sell()` (like
`get_quote()`) returns Jupiter's response verbatim, unprocessed, and nothing downstream reads
any of these fields except `execute_swap()`'s `"outAmount" not in quote` check, which real
responses satisfy. Flagged as requested, not fixed, since there is nothing to fix.

No case of the reverse (something the mocks assumed that reality lacks) was found — the
mocks never asserted the *presence* of anything beyond what `outAmount` needed.

Part 3's mocked tests (Stage 10) matched reality closely, including the address-lookup-table
concatenation logic verified in the original report. No discrepancy found this round either.

## 3. Any new gap found (style of Gaps 7–10)

**None.** Every real call in every part behaved exactly as the existing code already
assumed. No new gap to name.

## 4. What remains UNVERIFIED, and why

- **Part 1's original revert test** (a real reverted transaction, checked through
  `confirm_transaction()`/`getSignatureStatuses` specifically): still open. This brief's Part
  3 incidentally surfaced real reverted transactions via `getTransaction`'s `meta.err`
  (section 1's bonus finding), which is encouraging but not equivalent —
  `confirm_transaction()` calls `getSignatureStatuses`, a different RPC method, and this
  brief's hard constraints don't cover re-touching that code path. **Still needs a real
  trade deliberately constructed to revert**, submitted and then polled via
  `getSignatureStatuses` specifically, per Stage 10's original report.
- **A real fill from this bot's own wallet**, buy or sell. Everything verified this run used
  *other* real wallets' real transactions (Penguin's pool activity, pump.fun program
  activity) — genuine mainnet data, but not a trade this bot itself placed. **Still needs one
  real, tiny, human-confirmed swap** from the configured wallet to close completely.

## 5. Would I now trust `get_quote_sell()` and `parse_fill_from_transaction()` with real money?

**`get_quote_sell()`: yes, for the quote step itself.** It returned correct, real, immediately
usable quotes for three different real mints at realistic sizes, with a response shape
identical to the already-trusted buy path. What would change my mind: any future Jupiter API
version change to the quote schema (nothing in this code defends against that — it trusts the
response shape implicitly, same as `get_quote()` always has), or evidence that decimals
handling breaks for a mint using something other than the standard SPL Token program (e.g. a
Token-2022 mint with transfer fees or other extensions, which none of the six real mints
tested here happened to use).

**`parse_fill_from_transaction()`: yes, with more confidence than before this run, but I
would still want a real fill from our own wallet before fully trusting it unattended.** 9 of
10 real transactions parsed correctly, including the two specific cases (no lookup table,
owner not fee payer) the original report was least confident about — both now confirmed with
real data and locked in as regression tests. What would change my mind either way: a real
trade from our own wallet parsing incorrectly (would lower confidence sharply — everything
tested here is *other* wallets' activity, not proof our own trade's shape is identical), or a
transaction using a Token-2022 mint (extension-bearing tokens can carry additional balance
fields this function doesn't know to look for) — none of the ten real transactions sampled
this run happened to be Token-2022.

## 6. Verbatim test output

### `pytest tests/test_safety.py -v`

```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- ...\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\projects\solana_trading_bot
collecting ... collected 60 items

tests/test_safety.py::test_reserve_check_blocks_when_below_floor PASSED  [  1%]
tests/test_safety.py::test_reserve_check_allows_when_exactly_at_floor PASSED [  3%]
tests/test_safety.py::test_reserve_check_allows_when_above_floor PASSED  [  5%]
tests/test_safety.py::test_reserve_block_emits_warning PASSED            [  6%]
tests/test_safety.py::test_same_ticker_lock_blocks_duplicate_concurrent_entry PASSED [  8%]
tests/test_safety.py::test_unconfirmed_fill_is_never_treated_as_filled PASSED [ 10%]
tests/test_safety.py::test_reverted_fill_raises_transaction_reverted_not_treated_as_filled PASSED [ 11%]
tests/test_safety.py::test_confirmed_fill_returns_success PASSED         [ 13%]
tests/test_safety.py::test_submission_failure_carries_local_signature_forward PASSED [ 15%]
tests/test_safety.py::test_confirm_transaction_null_err_is_success PASSED [ 16%]
tests/test_safety.py::test_confirm_transaction_absent_err_is_success PASSED [ 18%]
tests/test_safety.py::test_confirm_transaction_non_null_err_is_failed PASSED [ 20%]
tests/test_safety.py::test_confirm_transaction_never_resolves_is_timeout PASSED [ 21%]
tests/test_safety.py::test_dry_run_never_reaches_submission PASSED       [ 23%]
tests/test_safety.py::test_ten_positions_at_min_lot_fit_a_2_5_sol_wallet_eleventh_refused PASSED [ 25%]
tests/test_safety.py::test_dca_fill_refused_if_it_would_exceed_max_position_sol PASSED [ 26%]
tests/test_safety.py::test_no_lot_below_min_lot_sol PASSED               [ 28%]
tests/test_safety.py::test_config_loads_with_dry_run_true_and_no_wallet_key PASSED [ 30%]
tests/test_safety.py::test_config_raises_when_dry_run_false_and_no_wallet_key PASSED [ 31%]
tests/test_safety.py::test_importing_wallet_does_not_construct_a_keypair PASSED [ 33%]
tests/test_safety.py::test_reserve_check_allows_entry_when_no_wallet_configured PASSED [ 35%]
tests/test_safety.py::test_price_history_written_for_each_open_position_each_cycle PASSED [ 36%]
tests/test_safety.py::test_price_history_skips_positions_before_initials PASSED [ 38%]
tests/test_safety.py::test_price_history_not_written_when_no_positions_open PASSED [ 40%]
tests/test_safety.py::test_price_history_record_contains_every_specified_field PASSED [ 41%]
tests/test_safety.py::test_price_history_write_failure_does_not_propagate PASSED [ 43%]
tests/test_safety.py::test_initials_sells_33_percent_not_50 PASSED       [ 45%]
tests/test_safety.py::test_daily_loss_cap_blocks_new_entries_when_breached PASSED [ 46%]
tests/test_safety.py::test_daily_loss_cap_allows_when_under_the_cap PASSED [ 48%]
tests/test_safety.py::test_daily_loss_cap_boundary_just_before_and_after_utc_midnight PASSED [ 50%]
tests/test_safety.py::test_daily_loss_cap_ignores_positions_without_closed_at PASSED [ 51%]
tests/test_safety.py::test_alert_logs_at_error_with_distinctive_prefix PASSED [ 53%]
tests/test_safety.py::test_plan_sell_does_not_mutate_apply_sell_does PASSED [ 55%]
tests/test_safety.py::test_plan_dca_fill_does_not_mutate_apply_dca_fill_does PASSED [ 56%]
tests/test_safety.py::test_monitor_once_skips_evaluation_for_in_flight_position_but_updates_peak PASSED [ 58%]
tests/test_safety.py::test_monitor_once_evaluates_normally_once_in_flight_clears PASSED [ 60%]
tests/test_safety.py::test_recovery_no_signature_clears_and_does_not_apply PASSED [ 61%]
tests/test_safety.py::test_recovery_confirmed_sell_applies_missed_update PASSED [ 63%]
tests/test_safety.py::test_recovery_confirmed_partial_sell_leaves_position_open PASSED [ 65%]
tests/test_safety.py::test_recovery_confirmed_buy_applies_missed_update PASSED [ 66%]
tests/test_safety.py::test_recovery_reverted_clears_without_applying PASSED [ 68%]
tests/test_safety.py::test_recovery_timeout_leaves_flag_and_alerts PASSED [ 70%]
tests/test_safety.py::test_recovery_no_in_flight_trades_is_a_no_op PASSED [ 71%]
tests/test_safety.py::test_reserved_sol_sums_only_in_flight_buys PASSED  [ 73%]
tests/test_safety.py::test_reserve_check_blocks_when_in_flight_buys_would_breach_reserve PASSED [ 75%]
tests/test_safety.py::test_reserve_check_allows_when_in_flight_buys_still_leave_room PASSED [ 76%]
tests/test_safety.py::test_parse_fill_buy_reports_tokens_received_and_sol_spent PASSED [ 78%]
tests/test_safety.py::test_parse_fill_sell_reports_tokens_sent_and_sol_received PASSED [ 80%]
tests/test_safety.py::test_parse_fill_uses_real_decimals_not_a_hardcoded_default PASSED [ 81%]
tests/test_safety.py::test_parse_fill_finds_owner_via_address_lookup_table PASSED [ 83%]
tests/test_safety.py::test_parse_fill_raises_on_reverted_transaction PASSED [ 85%]
tests/test_safety.py::test_parse_fill_raises_when_transaction_not_found PASSED [ 86%]
tests/test_safety.py::test_parse_fill_raises_when_no_matching_token_balance PASSED [ 88%]
tests/test_safety.py::test_get_token_decimals_reads_from_get_token_supply PASSED [ 90%]
tests/test_safety.py::test_get_token_decimals_raises_when_mint_not_found PASSED [ 91%]
tests/test_safety.py::test_get_quote_sell_sends_correct_direction_and_amount PASSED [ 93%]
tests/test_safety.py::test_get_quote_sell_wrong_decimals_produce_order_of_magnitude_error PASSED [ 95%]
tests/test_safety.py::test_get_quote_sell_defaults_slippage_from_config PASSED [ 96%]
tests/test_safety.py::test_parse_fill_real_transaction_no_lookup_table PASSED [ 98%]
tests/test_safety.py::test_parse_fill_real_transaction_owner_not_fee_payer PASSED [100%]

============================= 60 passed in 7.30s ==============================
```

(58 tests carried over unchanged from Stage 10, plus 2 new regression tests added this
stage, built from real captured mainnet data rather than synthetic fixtures — see section
below.)

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

## 7. Confirmation no transaction was signed or submitted

No code path in this run called `build_signed_transaction()`, `submit_transaction()`, or
`execute_swap()`'s live branch. Every real network call made this run was one of:

- `trade_execution.get_token_decimals()` — Helius `getTokenSupply`, read-only.
- `trade_execution.get_quote()` / `get_quote_sell()` — Jupiter `/quote`, read-only (a quote
  moves no funds and requires no signature).
- Public Solana RPC `getSlot`, `getBlock`, `getSignaturesForAddress`, `getTransaction` — all
  read-only historical/state lookups, none of which can sign, submit, or spend anything.

`.env`/`.env.example`: `DRY_RUN=true`, unchanged. No file outside `tests/test_safety.py` and
this report was modified — `src/trade_execution.py` and every other source file are
byte-identical to the `stage10-live-exec-foundation` branch point (confirmed via `git diff`,
empty for every file except the test file).

---

## Bug fixes

**None.** No bug was found in Parts 1–3. The two new tests added (real-data regressions for
the no-lookup-table and not-the-fee-payer cases) exist to lock in verified-correct behaviour
against future changes, not to fix anything broken.
