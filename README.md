# rvn-ledger

Local, deterministic usage billing with audit reconciliation. Python 3.11+.
No network or model calls during billing. Dependencies (including Windows time-zone data) install automatically.

## Install (Linux, macOS, Windows PowerShell)

Install `uv` from https://docs.astral.sh/uv/getting-started/installation/ first.
This repository is **private**: your Git account must have access. Configure Git authentication through your credential manager or `gh auth setup-git`; never embed tokens in URLs.

```sh
uv tool install git+https://github.com/efe-arv/rvn-ledger.git
```

If prompted, `uv tool update-shell` adds the executable directory to PATH; reopen your terminal.

## Use

Put your authorized input files (`events.jsonl`, `accounts.json`, `plans.json`, `period.json`) in the current folder’s `data/`. Inputs and outputs are deliberately not shipped.

```sh
rvn-ledger --help
rvn-ledger --version
rvn-ledger run
rvn-ledger check
rvn-ledger run --input-dir ./data --out ./results --json
rvn-ledger check --out ./results --json
```

`-help`, `-run`, `-check` are compatibility aliases. `run` produces invoices, quarantine, audit and manifest, then checks their hashes and reconciliation. Missing inputs fail explicitly; there is no implicit sample-data fallback. Human summaries are default; `--json` emits one JSON object. Individual `--events`, `--accounts`, `--plans`, `--period` paths override `--input-dir`.

Exit codes: 0 success; 2 invalid input, usage, I/O or failed output check; 1 internal reconciliation failure. Diagnostics go to stderr.

Publication rolls back on caught replacement errors but is not crash/power-loss atomic. Consumers must run `check` before using output. Reconciliation verifies specified invariants; it is not a signature or independent proof of all billing rules.

For development: `uv sync`, then `uv run python -m unittest discover -s tests`. The legacy `uv run python cli.py run --input-dir data --out out --json` also works. No license or rights to third-party assignment material are asserted.

## Explain a record

```sh
rvn-ledger explain
rvn-ledger explain --line 12
rvn-ledger explain --event-id example-event --json
rvn-ledger explain --events ./data/events.jsonl --json
```

Without a selector, lists quarantined physical lines. Exact event selection includes all copies and the duplicate's canonical line. Reports ordered reason codes, relevant field, readable explanation and source line/hash. Output hashes and audit reconciliation are checked first. Original field values are shown only with `--events` whose hash matches the run; without it no source value is guessed. Does not modify or resolve records.

## Excel report

```sh
rvn-ledger export --format xlsx --out ./out --file ./ledger-report.xlsx
```

Sheets: Summary, Invoices, InvoiceLines, Quarantine, Sources, InputHashes. Filters, frozen headers, exact minor-unit amounts, separate currency totals. Large integers (15+ digits) are text to avoid Excel precision loss. Untrusted strings are text, never formulas. The JSON output set remains authoritative; the XLSX is a review report, not an importable billing input. Existing workbook paths are never overwritten.

## Excel input transport and import

```sh
rvn-ledger export --kind inputs --input-dir ./data --file ./ledger-inputs.xlsx
rvn-ledger import --file ./ledger-inputs.xlsx --input-dir ./imported-data
rvn-ledger run --input-dir ./imported-data --out ./imported-out
rvn-ledger check --out ./imported-out
```

**Scope of v1:** explicit lossless JSON/JSONL transport in Excel, not automatic interpretation of arbitrary business spreadsheets. Normal JSON event text is editable in Events.text. Nested account/plan/period configuration remains JSON text in Config.text. No inferred date, money or numeric conversions. Do not import the report workbook.

Input workbook schema (sheet order and exact headers are required):
- Schema: header `schema`, one data cell `rvn-ledger-inputs-v1`.
- Events: `line`, `encoding`, `text`, `eol`. Consecutive physical line numbers starting at 1. Encoding `utf8` (editable original JSON text) or `base64` (lossless unsupported bytes). EOL is `LF`; only the final line may use `NONE`. Each row carries exactly one physical source line. An empty event stream has no data rows.
- Config: `file`, `part`, `encoding`, `text`. Only accounts.json, plans.json, period.json; consecutive parts starting at 1 per file. Concatenated parts reconstruct each original file. Large configurations are split into chunks; bytes unrepresentable in XML/Excel (including CR line endings) use base64. Do not edit base64 unless you know the exact byte encoding.

Import validates the workbook, reconstructs inputs, runs the billing engine and reconciles before writing a **new** input directory. Invalid configuration fails with no destination. Invalid event records remain quarantined under the existing rules, not silently corrected or discarded. Existing destinations, even empty directories, are rejected. Publication cleans up caught write errors but is not crash-atomic; after interruption preserve evidence and rerun to a different new directory.

Formulas (including cached formulas), Excel error cells, numeric/date cells in text columns, unknown sheets/headers, external links, embedded objects, macros, ambiguous line/part numbers and oversized archives are rejected. Limits: 20 MiB archive/decoded input, 64 MiB expanded archive, fewer than 100,000 rows per sheet, 30,000 UTF-16 code units per text cell. Unusually long source lines should use direct JSONL instead.

All new commands accept `--json`; error diagnostics remain on stderr with exit 2. XLSX files are **not encrypted**: treat them as sensitive input/output and store them accordingly.

## Upgrade / pinned private installation

```sh
uv tool install --force git+https://github.com/efe-arv/rvn-ledger.git@v1.1.0
rvn-ledger --version
```

The same commands work in Windows PowerShell. Git credentials must already grant private repository access. `tzdata`, `openpyxl` and hardened XML parsing install automatically. No public package registry publication.
