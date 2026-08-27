# Stage 1 — Safety Layer and Config: Change Report

Branch: `stage1-safety` (created from `main` at commit `46ecb5c`, not committed to
`main`, not pushed).

What was done, not what was intended.

---

## 1. Every file created, modified or deleted, with line counts

| File | Status | Before | After |
|---|---|---:|---:|
| `.env` | modified | 6 lines | 12 lines (values never displayed anywhere in this process) |
| `.env.example` | modified | 5 | 13 |
| `.gitignore` | modified | 16 | 17 |
| `requirements.txt` | modified | 22 | 28 |
| `src/config.py` | **created** | — | 209 |
| `src/runner.py` | modified | 1,029 | 1,157 |
| `src/wallet.py` | modified | 62 | 88 |
| `src/trade_execution.py` | modified | 189 | 328 |
| `tests/test_safety.py` | **created** | — | 237 |
| `tests/test_reject_paths.py` | modified | 326 | 337 |
| `tests/test_jupiter_fields.py` | modified | 340 | 354 |
| `tests/test_end_to_end.py` | modified | 258 | 273 |

Nothing was deleted. `src/entry_logic.py` and `src/exit_logic.py` are byte-identical
to `main` — confirmed with `git diff --stat`, no output for either file.
`src/listener.py`, `src/parser.py`, `src/data_logger.py`, `src/data_loader.py`,
`src/market_data.py`, `src/pcr_analysis.py`, `src/trading_window.py`,
`src/time_of_day_analysis.py`, `src/test_swap.py`, `generate_wallet.py` are
also untouched.

---

## 2. What changed in each modified file, and why

**`.env` / `.env.example`** — both already existed (see section 9). Added six new
keys to each: `DRY_RUN`, `MAX_POSITION_SOL`, `MAX_CONCURRENT_POSITIONS`,
`MIN_SOL_RESERVE`, `SLIPPAGE_BPS`, `PRIORITY_FEE_LAMPORTS`. The six existing
credential keys were left untouched — added to `.env` via a shell append that
checks each key name with `grep` and only appends if absent, so the file's
existing content was never read, displayed, or risked being overwritten.

**`.gitignore`** — added `.pytest_cache/`, the directory pytest creates on every
run. `.env` and `*.session` were already present (constraint 5 was already
satisfied; verified, not fixed).

**`requirements.txt`** — added `pytest==9.1.1` and its five transitive
dependencies (`colorama`, `iniconfig`, `packaging`, `pluggy`, `Pygments`),
pinned to the versions actually installed, matching the file's existing style.

**`src/config.py` (new)** — the configuration layer Step 1 asks for. Loads
`.env`, casts every value to its correct type, validates ranges, and raises a
specific `ConfigError` naming the exact key and problem if anything is missing,
unparseable, or out of range. Exposes `log_resolved_config()`, which logs every
setting with every credential masked (see section 5 for the full list).

**`src/runner.py`** —
- Now imports `config` and `wallet` (see SENSITIVE, section 6).
- `DRY_RUN`, `API_ID`, `API_HASH`, `CHANNEL` are now read from `config.*`
  instead of `os.getenv()` directly. The now-redundant runtime credential
  check inside `main()` was removed, since `import config` already fails
  loudly, earlier, with a more specific message.
- Startup now logs the resolved config (masked) via `config.log_resolved_config()`.
- New `check_reserve_ok(trade_size_sol)`: calls `wallet.get_balance()` and
  refuses if `balance - trade_size_sol < config.MIN_SOL_RESERVE`. Fails
  *closed* if the balance can't be fetched at all — an unknown balance is
  treated as an insufficient one, not as permission to proceed.
- `check_reserve_ok()` is called at the top of `open_position()` (before the
  price fetch — no reason to spend a Jupiter call if the reserve was already
  going to block the fill) and inside `check_dca_fills()` before every DCA
  tranche fill, so "before any buy" applies to every buy, not just the first.
  `check_dca_fills()` is now `async def` as a result; its one call site in
  `_monitor_once()` was updated to `await` it.
- New concurrency cap in `on_message()`: refuses a new entry if
  `MAX_CONCURRENT_POSITIONS` positions are already open.
- New position-size cap in `on_message()`: refuses if `decision["total_lot_sol"]`
  (the aggregate across every planned DCA tranche) exceeds `MAX_POSITION_SOL`.
- Two now-inaccurate log lines were corrected: the module docstring and the
  "DRY RUN - no wallet loaded" startup line both used to claim the wallet is
  never loaded, which stopped being true the moment `runner.py` started
  importing `wallet` for the reserve check (see SENSITIVE, section 6).

**`src/wallet.py`** — `get_balance()` now has an explicit 10s timeout and up to
3 attempts with exponential backoff (1s, 2s, 4s) before giving up and raising,
matching `trade_execution.py`'s policy. Previously this was the one Helius call
in the whole codebase with no timeout at all — and it is now called on every
buy, not just when this file is run standalone. Credential loading moved from
its own `os.getenv()` calls to `config.WALLET_PRIVATE_KEY` / (indirectly)
`config.HELIUS_RPC_URL`.

**`src/trade_execution.py`** —
- `SLIPPAGE_BPS` and `PRIORITY_FEE_LAMPORTS` now default from `config.py`
  (2500 bps, 125,000 lamports) instead of a hardcoded 100 bps and no priority
  fee handling at all — there was none before this change.
  `prioritizationFeeLamports` is now sent in the Jupiter `/swap` request body.
- Every network call goes through `_request_with_retries()`: a 10s timeout,
  up to 3 attempts with backoff (1s, 2s, 4s), then abandoned and logged.
  `confirm_transaction()`'s own polling loop keeps its existing "poll every 2s
  until an outer deadline" shape (a different pattern to a one-shot
  request retry, and a better fit for "wait for eventual state") but every
  individual poll now has its own timeout and a failed poll is logged and
  retried on the next tick rather than crashing the wait.
- New `FillNotConfirmedError`. `execute_swap()` now raises it if
  `confirm_transaction()` returns `False`, instead of returning a soft
  `"confirmed": False` field a caller could fail to check. This is 2b.
- `execute_swap()` now checks `config.DRY_RUN` itself, immediately after
  fetching a real (read-only) quote and before ever calling
  `build_signed_transaction()` or `submit_transaction()`. While `DRY_RUN` is
  true it logs the mint, amount, resolved slippage, resolved priority fee and
  the real expected output, then returns — this is Step 3's dry-run logging
  requirement, and it is enforced *inside* the function itself now, not left
  to whatever eventually calls it to remember.

**`tests/test_reject_paths.py`, `tests/test_jupiter_fields.py`,
`tests/test_end_to_end.py`** — see section 4/7. All three needed a small fix
after the safety guards above were added: none of them mocked
`wallet.get_balance()`, so they started hitting the real (small) wallet
balance and failing for reasons unrelated to what they actually test. Each now
stubs `get_balance()` to a large fake balance and raises `MAX_POSITION_SOL` for
the duration of the file, with a comment explaining why, pointing at
`tests/test_safety.py` as where those two guards are actually tested.

---

## 3. Full contents of `.env.example`

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHANNEL=
WALLET_PRIVATE_KEY=
HELIUS_RPC_URL=
JUPITER_API_KEY=

DRY_RUN=true
MAX_POSITION_SOL=0.4
MAX_CONCURRENT_POSITIONS=6
MIN_SOL_RESERVE=0.05
SLIPPAGE_BPS=2500
PRIORITY_FEE_LAMPORTS=125000
```

---

## 4. Exact pytest output, verbatim

```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- C:\projects\solana_trading_bot\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\projects\solana_trading_bot
collecting ... collected 7 items

tests/test_safety.py::test_reserve_check_blocks_when_below_floor PASSED  [ 14%]
tests/test_safety.py::test_reserve_check_allows_when_exactly_at_floor PASSED [ 28%]
tests/test_safety.py::test_reserve_check_allows_when_above_floor PASSED  [ 42%]
tests/test_safety.py::test_reserve_block_emits_warning PASSED            [ 57%]
tests/test_safety.py::test_same_ticker_lock_blocks_duplicate_concurrent_entry PASSED [ 71%]
tests/test_safety.py::test_unconfirmed_fill_is_never_treated_as_filled PASSED [ 85%]
tests/test_safety.py::test_dry_run_never_reaches_submission PASSED       [100%]

============================== 7 passed in 2.19s ==============================
```

7 tests, not 5, because the brief's five checks split into seven assertions
where "blocks below / allows above" naturally wanted three cases (below,
exactly at, above the floor) rather than two.

The existing suite (`python tests/run_all.py`, not pytest, not part of Step 4
but re-run because these changes touch the same code) also passes after the
three test-file fixes in section 2: `ALL CHECKS PASSED`, 11/11 checks.
`logs/positions.json` was confirmed at 64 positions and `data/calls.jsonl` at
70 records both before and after every test run in this stage (see section 9
for why 70, not the 65 recorded at the end of the previous stage).

---

## 5. Every configuration value now in use, and which file reads it

| Key | Type | Read by |
|---|---|---|
| `TELEGRAM_API_ID` | int | `config.py` (validates) → `runner.py` |
| `TELEGRAM_API_HASH` | str | `config.py` → `runner.py` |
| `TELEGRAM_CHANNEL` | str | `config.py` → `runner.py` |
| `WALLET_PRIVATE_KEY` | str, SENSITIVE | `config.py` → `wallet.py` |
| `HELIUS_RPC_URL` | str (validated as URL-shaped) | `config.py` → `wallet.py`, `trade_execution.py` |
| `JUPITER_API_KEY` | str, optional | `config.py` → `trade_execution.py`. **`market_data.py` still reads it independently via its own `os.getenv()`** — see section 9. |
| `DRY_RUN` | bool | `config.py` → `runner.py`, `trade_execution.py` |
| `MAX_POSITION_SOL` | float, min 0.0001 | `config.py` → `runner.py` |
| `MAX_CONCURRENT_POSITIONS` | int, min 1 | `config.py` → `runner.py` |
| `MIN_SOL_RESERVE` | float, min 0 | `config.py` → `runner.py` |
| `SLIPPAGE_BPS` | int, 1–10,000 | `config.py` → `trade_execution.py` |
| `PRIORITY_FEE_LAMPORTS` | int, min 0 | `config.py` → `trade_execution.py` |

`src/config.py` is the only place any of these are cast or validated; every
other file reads the already-validated `config.<NAME>` attribute.

---

## 6. Every location touching the private key or seed phrase — SENSITIVE

| File : line | What happens |
|---|---|
| `src/config.py:112` | `WALLET_PRIVATE_KEY = _require("WALLET_PRIVATE_KEY")` — read from `.env` as a plain string. |
| `src/config.py:197` | Logged, **masked only** (`_mask()`, at most 2 characters shown at each end), in `log_resolved_config()`. |
| `src/wallet.py:34` | `keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)` — the private key is parsed into an in-memory `Keypair` object. This runs at **import time**, unconditionally. |
| `src/trade_execution.py:49` | `from wallet import get_keypair, public_key as wallet_public_key` — imports the keypair accessor. |
| `src/trade_execution.py:177–178` | `keypair = get_keypair()` then `keypair.sign_message(...)` — the moment the key actually signs something. Only reached inside `build_signed_transaction()`, which is only reached if `config.DRY_RUN` is `False`. |
| `src/runner.py:50` | `import wallet` — **new in this stage**. This is the line that makes `src/wallet.py:34` (above) run every single time `runner.py` starts, not just when `wallet.py` or `trade_execution.py` are run standalone. `runner.py` never calls anything that signs; it only reaches `wallet.get_balance()`, which uses the derived public key. |
| `generate_wallet.py:20,26` | Pre-existing, untouched this stage. Prints the private key to the terminal by design — a one-off, human-run tool. |

The one **new** exposure surface this stage introduces is `runner.py:50`
(`import wallet`) — every future run of the bot now parses the real private
key into memory, where previously it did not. This is a necessary consequence
of doing a real reserve check against a real balance; there was no way to
derive the public key `get_balance()` needs without going through
`wallet.py`'s existing structure, which the brief did not ask to be
refactored. The key is never logged, printed, or transmitted from this new
code path — only used by `Keypair.from_base58_string()` to compute the public
key already used elsewhere in the codebase.

---

## 7. Changed beyond what the brief asked for

- **Removed the now-redundant credential check** at the top of `runner.py`'s
  `main()` (`if not API_ID or not API_HASH or not CHANNEL: raise SystemExit(...)`).
  `import config` at the top of the file already fails loudly, earlier, with a
  more specific per-key message — the old check could never fire.
- **Removed `import os` and `from dotenv import load_dotenv`** from
  `runner.py` and **`import os` / `from dotenv import load_dotenv`** from
  `wallet.py` and `trade_execution.py` — dead once credential loading moved to
  `config.py`.
- **Corrected two log lines / one docstring paragraph in `runner.py`** that
  claimed "no wallet is loaded" — no longer true once `import wallet` was
  added (see section 6).
- **Fixed three existing test files** (`test_reject_paths.py`,
  `test_jupiter_fields.py`, `test_end_to_end.py`) that broke as a direct,
  necessary consequence of the new reserve check and position-size cap
  (detailed in sections 2 and 9). Not fixing them would have meant reporting
  success while `tests/run_all.py` was failing, which the brief's own Step 4
  instruction says not to do — I've applied that standard to the whole
  project's test suite, not only the new pytest file, since leaving it broken
  would misrepresent the state of the codebase.
- **Added `.pytest_cache/` to `.gitignore`.**

---

## 8. Anything in this brief not completed

Nothing was left undone. Two items were judged **already satisfied** rather
than requiring new code — see section 9 for the reasoning on each
(re-read-live-prices-on-reconnect, and the same-ticker lock itself).

---

## 9. Conflicts between this brief and the existing code, stated plainly

1. **The brief's "Stack" line names PostgreSQL and Telethon/Pyrogram.** This
   codebase has neither Postgres nor Pyrogram anywhere — confirmed by
   `grep -rli "postgres\|psycopg\|asyncpg\|pyrogram\|sqlalchemy"` across `src/`
   and `requirements.txt`, zero matches. All persistence is JSON/JSONL files;
   the only Telegram library is Telethon. I did not invent a Postgres
   connection string or a Pyrogram credential for infrastructure that does not
   exist. If this brief was written against a different, larger version of
   this project than what's on disk, I'd want to know rather than guess
   further.

2. **`.env` and `.env.example` already existed** when Step 1 said "Create"
   them. Treated as "extend, don't overwrite" — see section 2.

3. **The real wallet balance is roughly 0.17 SOL**, not the "roughly 3.15 SOL"
   the brief's Step 2c "known and accepted" note assumes. I did not touch the
   6×0.4=2.4 SOL exposure numbers (told not to), but flagging plainly: at the
   real current balance, `MIN_SOL_RESERVE=0.05` means the reserve check will
   block almost any real buy right now, since even entry_logic's smallest
   possible lot (0.2 SOL) leaves `0.17 - 0.2 = -0.03`, already under the
   reserve. That is the guard working correctly against the real number, not
   a bug — but it means Stage 1's own safety layer would currently block
   nearly all live trading until the wallet is funded closer to the figure
   the brief assumed.

4. **Same-ticker lock (2c's main requirement) already existed** before this
   stage — added in an earlier session as one of three entry-guard bug fixes
   (`open_position_for_ticker()` in `runner.py`, blocking a new entry when the
   same ticker already has an OPEN position under a different contract). No
   new code was needed for it; I only added the two new caps (concurrency,
   position size) that Step 2c asks for alongside it.

5. **"Re-read live prices on reconnect, not the last known price" (Step 3)
   was already true of the existing architecture.** `_monitor_once()` calls
   `market_data.fetch_market_caps()` fresh on every single cycle and passes
   that same-cycle price straight into `exit_logic.check_exit_conditions()` —
   there is no code path anywhere that caches a price across cycles for a
   trailing-stop decision. Confirmed by reading the call chain, not assumed.
   No code change was needed or made for this line of the brief.

6. **`market_data.py`'s `JUPITER_API_KEY` was deliberately left reading its
   own `os.getenv()` rather than being migrated to `config.JUPITER_API_KEY`.**
   It was already .env-sourced (nothing hardcoded to move), and
   `market_data.py` is the one file every prior stage of this project has
   treated as too safety-critical to touch without a specific reason — "make
   its config loading consistent with a new file" didn't feel like a specific
   enough reason to take that risk. Functionally identical either way; noted
   here rather than done silently.

7. **`tests/build_fixtures.py` and `tests/test_analysis_chain.py`** were not
   touched — they don't call `runner.open_position()` or anything that hits
   `wallet.get_balance()`, so they were never affected by the new guards.

---

## 10. Running the bot locally in dry run

```
cd C:\projects\solana_trading_bot
venv\Scripts\python.exe src\runner.py
```

(Or `python src\runner.py` if `venv` is already activated in the shell.)

The log line that confirms it started correctly — the first line `main()`
logs, printed after the entry_logic/exit_logic version checks pass and before
any Telegram connection is attempted:

```
DRY RUN - simulated fills only, no transaction is ever signed or submitted
```

Immediately followed by the channel name and poll interval, the restored
position count, the full resolved config (secrets masked, via
`config.log_resolved_config()`), the entry guard summary, and the trading
window status — all before `client.run_until_disconnected()` is ever reached.
If `.env` is missing or malformed, none of this is reached at all: the
process fails at the `import config` line, with `ConfigError` naming the
exact key and problem.

To run just the new safety tests: `venv\Scripts\python.exe -m pytest tests\test_safety.py -v`
(not bare `pytest`, and not `pytest tests\` — see the warning at the top of
that file about why).

To run the full existing suite: `venv\Scripts\python.exe tests\run_all.py`.
