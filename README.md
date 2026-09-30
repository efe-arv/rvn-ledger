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
