# rvn-ledger

Exact, reproducible usage invoices from four local files. Python 3.11+.
The billing path makes no network or model calls. It uses integer money,
account-local billing periods, deterministic duplicate selection and a checked
audit trail.

## Install the reviewed release

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first.
This repository is private: your Git account must have access. Use a credential
manager or `gh auth setup-git`; never embed tokens in URLs.

```sh
uv tool install --force git+https://github.com/efe-arv/rvn-ledger.git@v1.2.0
rvn-ledger --version
```

Expected version: `rvn-ledger 1.2.0`. `uv tool update-shell` can add the executable
directory to PATH; reopen the terminal afterward. The same commands work in
Windows PowerShell. `tzdata` installs automatically for IANA time zones.

`v1.2.0` includes the final review fixes. `v1.1.0` is a historical release and
does not include those fixes. Tags are immutable; use the version above when
upgrading. There is no public package-registry release.

## Three-minute synthetic demo

From a checkout of this repository, with the tool installed:

```sh
rvn-ledger run --input-dir ./examples/demo --out ./out/demo --json
rvn-ledger check --out ./out/demo
rvn-ledger explain --out ./out/demo --events ./examples/demo/events.jsonl
```

Expected: **2 invoices, 3 quarantined records**. The payable amounts are
**109 TRY minor units** and **0 USD minor units**. The synthetic fixture covers
plan changes, cumulative tiers, rounding, credits, conflicting duplicates,
local boundaries, late arrivals and an account with no usage.
[Hand-derived arithmetic and expected invoices](examples/demo/README.md) are
committed alongside it; these are not the private assignment inputs.

## Bill your inputs

Place your authorized `events.jsonl`, `accounts.json`, `plans.json` and
`period.json` files in `./data`, then run:

```sh
rvn-ledger run
rvn-ledger check
rvn-ledger run --input-dir ./data --out ./results --json
rvn-ledger check --out ./results --json
```

`run` produces `invoices.json`, `quarantine.json`, `audit.json` and `manifest.json`,
then checks hashes and reconciliation. Human summaries are default; `--json`
emits one JSON object. Individual `--events`, `--accounts`, `--plans`, `--period`
paths override `--input-dir`. `-help`, `-run`, `-check` remain compatibility aliases.
Missing inputs fail explicitly; there is no implicit demo-data fallback.

Exit codes: **0** success; **2** invalid input, usage, I/O or failed output check;
**1** internal reconciliation failure. Diagnostics go to stderr.

JSON integers support at most **256 decimal digits**, excluding a minus sign.
This parser resource bound leaves room for exact multiplication and aggregation
before decimal output. Oversized event values receive `integer_too_large` and
are quarantined; unambiguous identity still participates in duplicate selection.
Oversized reference configuration fails before publication. No values are
clamped, rounded early or silently corrected.

Publication rolls back caught replacement errors, but is not crash/power-loss
atomic. Consumers must wait for publication to finish and run `check` before
using outputs. Hashes and reconciliation verify consistency, not a signature or
independent proof of every pricing input.

## Explain a record

```sh
rvn-ledger explain --out ./results
rvn-ledger explain --out ./results --line 12
rvn-ledger explain --out ./results --event-id example-event --json
rvn-ledger explain --out ./results --events ./data/events.jsonl --json
```

Without a selector, lists quarantined physical lines. Exact event selection
includes all copies and the duplicate's canonical line. Reports ordered reasons,
relevant fields and source line/hash. Original values are shown only when
`--events` matches the run's input hash. Output verification happens first.
The command does not modify or resolve records.

## Optional Excel extension

Excel is separate from the assignment's core CLI. Install it explicitly:

```sh
uv tool install --force "rvn-ledger[excel] @ git+https://github.com/efe-arv/rvn-ledger.git@v1.2.0"
```

This adds `openpyxl` and hardened XML parsing. Existing Excel commands remain
available with the extra; without it they return an actionable error.
[Excel setup, report export and lossless input transport](docs/EXCEL.md) document
the workbook contract and limits. JSON remains authoritative.

## Development and verification

```sh
uv sync --extra excel
uv run --extra excel python -m unittest discover -s tests
uv run --extra excel python -O -m unittest discover -s tests
```

For the installed-package E2E gate, use a separate non-editable environment:

```sh
uv venv .e2e-venv
uv pip install --python .e2e-venv ".[excel]"
uv run --python .e2e-venv --no-project python scripts/e2e.py
```

CI also tests a base installation without Excel, validates release metadata,
and runs the suite normally and under `-O` on Windows/Linux and Python 3.11/3.13.
The E2E gate compares hand-derived invoices, reruns billing, checks diagnostics,
performs the full Excel round trip and rejects tampered outputs.

The legacy `uv run python cli.py run --input-dir data --out out --json` remains
available. Private inputs and outputs are deliberately excluded from Git.
No license or rights to third-party assignment material are asserted.

See [engineering decisions and measured scaling](ENGINEERING.md),
[model-use boundaries](MODEL_USE.md), and the [changelog](CHANGELOG.md).
