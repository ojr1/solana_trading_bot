# Lazy Wallet Report — Stage 5

Branch `stage5-lazy-wallet`, created from `main`. **Not committed to `main`
yet** — the brief's own constraint 2 says not to, until the tests pass; they
now do (section 4). Not pushed.

---

## 1. Step 0 survey (posted and confirmed before any code was written)

Full detail was posted separately and confirmed. Summary:

- `config.py:112` required `WALLET_PRIVATE_KEY` unconditionally, at module
  import time, via `_require()` — no branch around it. `DRY_RUN` wasn't
  loaded until line 130, 18 lines later, so making the requirement
  conditional needed `DRY_RUN` known first — a restructure, not an `if`
  dropped in place.
- `wallet.py:34-35` built the `Keypair`/`public_key` at import time,
  unconditionally, confirmed matching the Stage 1 report.
- `get_balance()` call sites: `runner.py:370` (the reserve check) and
  `wallet.py`'s own standalone `main()`; every other reference was in test
  files, all mocked. `get_keypair()`: only `trade_execution.py:177`, inside
  `build_signed_transaction()`, reached only when `DRY_RUN` is false.
- **Second eager binding found and flagged before editing:**
  `trade_execution.py:49` did `from wallet import get_keypair, public_key as
  wallet_public_key` — binding `wallet.public_key` by name at
  `trade_execution.py`'s own import time. Deferring `wallet.py`'s key
  construction would have broken this with an `ImportError` the moment
  anything imported `trade_execution.py` — which `tests/test_safety.py`
  does directly. Approved as in-scope before any code was written.
- `check_reserve_ok()` called `wallet.get_balance()` unconditionally, even in
  `DRY_RUN`, by design — to prove the guard's logic against a real balance
  before it's ever relied on live. Fails closed on an RPC failure. With no
  key configured at all, this is a different failure mode than "Helius is
  down" — Step 2's decision (below) is what distinguishes them now.
- Import chain checked exhaustively: `entry_logic.py` (config only, never
  touches the wallet key, unaffected), `runner.py`, `trade_execution.py`,
  `wallet.py` itself, and `tests/test_safety.py` (exercises the full chain).
  Nothing else in the codebase imports either module.

---

## 2. Every file changed

| File | Before | After | What changed and why |
|---|---:|---:|---|
| `src/config.py` | 234 | 260 | `DRY_RUN` moved to load first (new dedicated section, before Telegram/Wallet). `WALLET_PRIVATE_KEY` is now `_optional()` when `DRY_RUN` is true, `_require()`d (identical message) when false. `log_resolved_config()`'s `WALLET_PRIVATE_KEY` line now prints `"(not set - dry run only)"` instead of masking `None`. |
| `src/wallet.py` | 88 | 136 | `Keypair` construction moved from module import time into `_load_keypair()`, called lazily by `get_keypair()`/`get_public_key()` (new)/`get_balance()`. New `NoWalletConfiguredError(RuntimeError)`, raised when no key is set. `main()` updated to call `get_public_key()` instead of the now-gone module-level `public_key` name. |
| `src/trade_execution.py` | 328 | 335 | The eager `from wallet import get_keypair, public_key as wallet_public_key` replaced with `import wallet`; both use sites inside `build_signed_transaction()` now call `wallet.get_keypair()`/`wallet.get_public_key()` at point of use — approved in Step 0. |
| `src/runner.py` | 1186 | 1206 | `check_reserve_ok()` gained an `except wallet.NoWalletConfiguredError` branch (Step 2, see section 3) before the existing generic `except RuntimeError`. Module docstring's SENSITIVE note updated — it previously said importing `wallet` unconditionally parses the private key, which is no longer true. |
| `tests/test_safety.py` | 409 | 520 | Four new tests (section 4) plus a `config_env` fixture. See section 4 for the two real bugs this surfaced and fixed during development, not just at the end. |

No new file was created. `.env.example` is **unchanged** — no new environment
variable was added, per your explicit instruction not to invent a simulated
balance value. `entry_logic.py`, `exit_logic.py`, `market_data.py`,
`exit_analysis.py`, `entry_analysis.py` and every stage 1–4 report file are
all confirmed byte-identical to `main` (`git diff --stat`, no output for any
of them). The real `.env` itself was never touched — confirmed by `git
status` (it's gitignored and untracked either way) and by directly checking
afterward that `DRY_RUN=true` and the `WALLET_PRIVATE_KEY=` line are both
still present exactly as before.

---

## 3. Step 2 decision and reasoning

Implemented exactly as you specified, not re-litigated:

- **`DRY_RUN` true, no wallet:** `check_reserve_ok()` catches
  `wallet.NoWalletConfiguredError` specifically (checked *before* the
  generic `except RuntimeError`, since it's a subclass and would otherwise
  be swallowed by the broader clause first) and logs at `WARNING`:
  `"RESERVE CHECK UNAVAILABLE - no wallet is configured (dry run only,
  temporary until the wallet is funded). Allowing entry WITHOUT a reserve
  check, not because one passed."` — then returns `True` (allows the entry).
  The message is deliberately built to make it impossible to misread as a
  passed check.
- **`DRY_RUN` false, no wallet:** unreachable — `config.py` raises
  `ConfigError` at startup before `runner.py` (or anything else) ever runs.
  Asserted directly, not just assumed: `test_config_raises_when_dry_run_
  false_and_no_wallet_key`.
- **Wallet configured, balance fetch fails:** unchanged. The existing
  `except RuntimeError` branch, `RESERVE BLOCK` log and fail-closed `return
  False` are untouched — confirmed by reading the diff (that branch's code
  is identical) and by the pre-existing tests for it still passing unchanged
  (section 4).

No simulated-balance `.env` value was added, per your instruction — a
checked-against-fiction reserve would be worse than an honestly-skipped one,
and risks carrying a fake number into a context where it matters.
`check_reserve_ok()`'s original docstring reasoning (why it runs even in dry
run, to prove itself against a real balance before going live) is preserved
verbatim, with a new paragraph appended noting the no-wallet path is a
temporary state expected to end once the wallet is funded.

---

## 4. Verbatim test output

**`pytest tests/test_safety.py -v`:**
```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- ...\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\projects\solana_trading_bot
collecting ... collected 14 items

tests/test_safety.py::test_reserve_check_blocks_when_below_floor PASSED  [  7%]
tests/test_safety.py::test_reserve_check_allows_when_exactly_at_floor PASSED [ 14%]
tests/test_safety.py::test_reserve_check_allows_when_above_floor PASSED  [ 21%]
tests/test_safety.py::test_reserve_block_emits_warning PASSED            [ 28%]
tests/test_safety.py::test_same_ticker_lock_blocks_duplicate_concurrent_entry PASSED [ 35%]
tests/test_safety.py::test_unconfirmed_fill_is_never_treated_as_filled PASSED [ 42%]
tests/test_safety.py::test_dry_run_never_reaches_submission PASSED       [ 50%]
tests/test_safety.py::test_ten_positions_at_min_lot_fit_a_2_5_sol_wallet_eleventh_refused PASSED [ 57%]
tests/test_safety.py::test_dca_fill_refused_if_it_would_exceed_max_position_sol PASSED [ 64%]
tests/test_safety.py::test_no_lot_below_min_lot_sol PASSED               [ 71%]
tests/test_safety.py::test_config_loads_with_dry_run_true_and_no_wallet_key PASSED [ 78%]
tests/test_safety.py::test_config_raises_when_dry_run_false_and_no_wallet_key PASSED [ 85%]
tests/test_safety.py::test_importing_wallet_does_not_construct_a_keypair PASSED [ 92%]
tests/test_safety.py::test_reserve_check_allows_entry_when_no_wallet_configured PASSED [100%]

============================= 14 passed in 1.79s ==============================
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

`logs/positions.json` (64 positions) and `data/calls.jsonl` (70 records)
confirmed unchanged before and after every run this stage.

**Incidents during development, not swept under the final green run:**

1. **A real credential briefly appeared in plaintext tool output.** My first
   draft of the two config-reload tests used `monkeypatch.delenv
   ("WALLET_PRIVATE_KEY")` to simulate "the key is absent." `config.py`
   calls `load_dotenv()` on every reload, and `python-dotenv` defaults to
   `override=False` - it only skips a variable already *present* in
   `os.environ`. Deleting it makes it absent again, so the reload
   immediately re-read the real key from `.env` and repopulated it - which
   then printed in full inside a failed assertion's output. I flagged this
   to you the moment it happened, before doing anything else. Root cause
   fixed by setting the variable to an **empty string** instead of deleting
   it - present-but-blank is not overridden by `load_dotenv()`, and
   `config.py`'s own `_require()`/`_optional()` already treat blank the same
   as missing. No other artifact of the exposure was created (no file
   written, nothing persisted) beyond that one terminal output, which you
   were shown directly, not summarized around.
2. **A fixture-teardown ordering bug**, found immediately after fixing (1):
   pytest tears fixtures down LIFO, so `config_env`'s post-yield reload was
   running *before* `monkeypatch`'s own restore of the real environment, not
   after - meaning it reloaded `config.py` against the still-mutated test
   environment and itself raised at teardown. Fixed by calling
   `monkeypatch.undo()` explicitly, first, inside `config_env`'s own
   teardown, rather than relying on fixture ordering to get this right.
3. **A `pytest.raises(config.ConfigError, ...)` identity mismatch**:
   `pytest.raises()` evaluates `config.ConfigError` *before* the reload
   inside its `with` block runs, but the reload re-executes `class
   ConfigError(Exception): ...`, producing a new class object - so the
   exception actually raised, an instance of the *new* class, didn't match
   the *old* class reference `pytest.raises()` had already captured. Fixed
   by checking `type(exc).__name__ == "ConfigError"` and the message
   content directly, sidestepping class identity across a reload entirely.

None of these three were silently patched over - all three are explained in
comments at the point they matter in `tests/test_safety.py`, not just here.

---

## 5. WALLET_PRIVATE_KEY still required when DRY_RUN is false

**Confirmed, and enforced exactly as before.** `config.py`'s conditional:
```python
if DRY_RUN:
    WALLET_PRIVATE_KEY = _optional("WALLET_PRIVATE_KEY")
else:
    WALLET_PRIVATE_KEY = _require("WALLET_PRIVATE_KEY")
```
When `DRY_RUN` is false, this is `_require()` - identical function, identical
message, to what ran unconditionally before this stage. Verified by test
(`test_config_raises_when_dry_run_false_and_no_wallet_key`, passing) and
manually, in an isolated temp `.env` (not the real one): `DRY_RUN=false` with
no `WALLET_PRIVATE_KEY` line raised
`ConfigError: WALLET_PRIVATE_KEY is missing from .env...` exactly as before
this stage existed.

---

## 6. Full `.env.example` contents

**Unchanged** - no new variable was added (per instruction, no simulated
balance value). For completeness:
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

## 7. Changed beyond this brief

The `trade_execution.py` import fix (section 1/2) was beyond the brief's
literal file list but pre-approved in Step 0 before being made. Nothing else
went beyond the brief.

---

## 8. Conflicts with stage 1's decisions

**Stage 1's `check_reserve_ok()` docstring said the guard runs "even while
DRY_RUN is true... so the guard's logic is proven correct against the real
balance before it is ever relied on for a real trade."** That reasoning is
preserved verbatim and still holds whenever a wallet *is* configured. This
stage adds a state Stage 1 didn't anticipate - dry run with literally no
wallet at all - which that reasoning can't cover (there is no real balance to
prove the guard against). Not a contradiction of Stage 1's decision, but a
real gap it left open, now closed explicitly rather than left to fail
opaquely (as it did on the VPS deploy that prompted this stage).

**Stage 1's own report (`CHANGES_REPORT.md` section 6) flagged `import
wallet` in `runner.py` as "the one new exposure surface stage 1 introduced"** -
parsing the real private key into memory on every startup. This stage removes
that exposure specifically for the dry-run-with-no-key case: importing
`wallet.py` no longer touches the key at all unless something actually calls
`get_keypair()`/`get_public_key()`/`get_balance()`. When a key *is*
configured (as in this repo's own `.env` right now), it is still parsed into
memory the first time `check_reserve_ok()` runs (i.e. still on effectively
every real startup that reaches that point) - laziness changes *when*, not
*whether*, for the case where a key exists. The exposure Stage 1 flagged is
now avoided only in the specific case Stage 5 exists to fix: no key at all.

No other conflicts found with `main`'s state or with stage 1-4's decisions.

---

## 9. Exact command to run locally in dry run, without a wallet key

```
cd C:\projects\solana_trading_bot
```
Remove or blank the `WALLET_PRIVATE_KEY=` line in `.env` (with `DRY_RUN=true`
already set), then:
```
venv\Scripts\python.exe src\runner.py
```
`config.py` will load successfully with no key. The first reserve check will
log `RESERVE CHECK UNAVAILABLE - no wallet is configured...` at `WARNING` and
allow entries through, exactly as this report describes. To restore full
reserve-checking, add a real `WALLET_PRIVATE_KEY=` back to `.env` - the
existing behaviour (parse it, check the real balance) resumes automatically
the next time `check_reserve_ok()` runs, no restart-specific code needed
beyond restarting the process.
