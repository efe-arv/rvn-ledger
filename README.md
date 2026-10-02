# rvn-ledger

Turn a month of raw usage events into exact, reproducible, auditable invoices.

`rvn-ledger` is a command-line tool. It reads four files — the usage events, the
accounts, the price plans and the billing period — and writes one invoice per
account, a quarantine report for records it could not bill, an audit trail and a
run manifest. Money is integer minor units (cents, kuruş) end to end, every
invoice line can be traced back to the events that produced it, and running it
twice on the same inputs in the same runtime and time-zone environment produces
byte-identical files.

The event stream is assumed to be messy: delivered at least once, out of order,
sometimes duplicated with a different payload, sometimes late, malformed or for
an account that does not exist. None of that stops a run; each record gets
exactly one recorded outcome.

No network access, no database, no model calls. Python 3.11+ and the `tzdata`
package (installed automatically) are the only requirements.

## Quick start

```sh
git clone https://github.com/efe-arv/rvn-ledger.git
cd rvn-ledger
python -m pip install .

rvn-ledger run --input-dir examples/demo --out out/demo
rvn-ledger check --out out/demo
rvn-ledger explain --out out/demo --events examples/demo/events.jsonl
```

With uv, run `uv sync` instead of the install and prefix each command with
`uv run`, for example `uv run rvn-ledger check --out out/demo`.

Expected: `OK: 2 invoices; 3 quarantined -> out/demo`. The demo is a small
synthetic month whose invoices were worked out by hand in
[examples/demo/README.md](examples/demo/README.md): a plan change mid-month,
cumulative tiers, half-up rounding, a capped credit, a conflicting duplicate, a
local-midnight boundary, a late arrival and an account with no usage.

To install the command without cloning:

```sh
python -m pip install git+https://github.com/efe-arv/rvn-ledger.git
# or
uv tool install git+https://github.com/efe-arv/rvn-ledger.git
```

`python -m rvn_ledger ...` works anywhere `rvn-ledger ...` does.

## Running it on your own data

Put `events.jsonl`, `accounts.json`, `plans.json` and `period.json` in `./data`, then:

```sh
rvn-ledger run              # reads ./data, writes ./out
rvn-ledger check            # re-verifies ./out
```

| Option | Meaning |
|---|---|
| `--input-dir DIR` | Directory holding the four inputs (default `data`). |
| `--events`, `--accounts`, `--plans`, `--period` | Path to one input; overrides `--input-dir` for that file. |
| `--out DIR` | Output directory (default `out`). |
| `--json` | Print one machine-readable JSON summary instead of a human line. |

Exit codes: `0` success; `2` unusable input or configuration, usage error, I/O
failure, or an output set that fails `check`; `1` internal inconsistency (the run
did not reconcile with its own audit trail, so nothing was published).
Diagnostics go to stderr.

A bad *event* never stops a run — it is quarantined. Bad *configuration* (an
unknown time zone, a plan without a price in the account's currency, overlapping
plan segments, a period whose `days_in_period` disagrees with its dates) stops
the run before anything is written, because every invoice would be suspect.

## Inputs

- **`events.jsonl`** — one JSON object per line:
  `{"event_id", "ingest_seq", "account_id", "metric", "units", "ts", "ingested_at"}`.
  `ts` is when the usage happened and `ingested_at` when it was received, both
  ISO 8601 with an explicit offset. A UTF-8 byte order mark at the start of the
  file is ignored for parsing; every hash still covers the bytes as written.
- **`accounts.json`** — accounts with an IANA `timezone`, a `currency`, a
  `credit_minor` and dated `plan_segments` (`[from, to)` local dates).
- **`plans.json`** — per plan and currency: a full-period `subscription_fee_minor`
  and, per metric, tiers of `{from_units, to_units, unit_price_micros}`.
- **`period.json`** — local period start and exclusive end, `days_in_period`,
  `late_cutoff_hours_after_period_end` and the list of metrics.

## Billing rules, and where each one lives

Each rule is implemented once. The audit check (`rvn-ledger check`) calls the
same functions, so changing a rule means changing one place plus the tests that
pin the old behaviour.

| # | Rule | Code |
|---|---|---|
| 1 | **Period and timezone.** An event counts if its `ts`, in the account's own time zone, falls in `[period start, period end)`. | `timing.period_bounds`, `timing.classify_time` |
| 2 | **Deduplication.** Same `event_id` = same event. The first copy by `ingest_seq` wins; later copies are ignored even if their payload differs. | `selection.copy_precedence`, `selection.deduplicate` |
| 3 | **Late arrivals.** An event ingested more than `late_cutoff_hours_after_period_end` (48) after the account's *local* period end is excluded. | `timing.classify_time` (hours come from `period.json`) |
| 4 | **Quarantine, never crash.** Missing or non-integer units, unknown metric, unparseable timestamp, units ≤ 0, unknown account, or an unreadable line: recorded with a reason, run continues. | `validation.validate_event` (reason order), `inputs.read_events` (unreadable lines) |
| 5 | **Subscription.** Charged per plan segment, prorated by whole local days: `round_half_up(fee_minor × days ÷ days_in_period)`. | `subscription.account_subscription`, `money.subscription_amount` |
| 6 | **Usage tiers.** Priced on the plan in effect at period end; tiers are cumulative over the whole period, each bracket is `[from, to)`, each tier is its own line. | `subscription.account_subscription` (period-end plan), `tiers.split_units` |
| 7 | **Money.** Integer minor units only. Each line is `round_half_up(units × unit_price_micros ÷ 10000)`; the subtotal is the sum of already-rounded lines. | `money.usage_amount`, `money.round_half_up` |
| 8 | **Credits.** Applied after the subtotal, capped at it; a total is never negative; unused credit is reported as `credit_remaining_minor`. | `money.apply_credit` |
| 9 | **Currency.** Never mixed and never converted. An account with no usage still gets a subscription-only invoice. | `subscription._fee`, `tiers.metric_tiers` (prices must exist in the account's currency) |

Checks run in this order for every event line: **parse → deduplicate → validate
(quarantine) → period → late → bill**. Deduplication comes first so a later
"fixed" copy can never replace the first copy; validation comes before the time
checks because a period cannot be evaluated for an unknown account or an
unreadable timestamp. [ENGINEERING.md](ENGINEERING.md) explains each choice.

## Outputs

| File | Contents |
|---|---|
| `invoices.json` | One invoice per account, sorted by `account_id` (shape below). |
| `quarantine.json` | `[{"event_id", "reason"}]` in source-line order. |
| `audit.json` | Every raw line's decision (status, reasons, the winning line for a duplicate, the line's SHA-256) and, per invoice, the plan segments, the tariff used, each line's formula, the source events per metric and the credit arithmetic. |
| `manifest.json` | Input hashes and sizes, counts per outcome, totals per currency, Python / tzdata / code versions, the source and TZif hash of every billed time zone, and output hashes. |

An invoice from the demo — a plan change on the 16th, usage crossing a tier, a
half-unit storage line rounded up, and a credit of 5:

```json
{
  "account_id": "demo-try",
  "currency": "TRY",
  "timezone": "Europe/Istanbul",
  "billable_units": {"api_calls": 12, "storage_gb_hours": 5},
  "lines": [
    {"kind": "subscription", "plan_id": "basic", "days": 15, "amount_minor": 45},
    {"kind": "subscription", "plan_id": "pro", "days": 15, "amount_minor": 60},
    {"kind": "usage", "metric": "api_calls", "tier_from": 0, "tier_to": 10, "units": 10, "amount_minor": 5},
    {"kind": "usage", "metric": "api_calls", "tier_from": 10, "tier_to": null, "units": 2, "amount_minor": 3},
    {"kind": "usage", "metric": "storage_gb_hours", "tier_from": 0, "tier_to": null, "units": 5, "amount_minor": 1}
  ],
  "subtotal_minor": 114,
  "credit_applied_minor": 5,
  "credit_remaining_minor": 0,
  "total_minor": 109,
  "quarantined_count": 1
}
```

Every raw line ends in exactly one of `accepted`, `duplicate_ignored`,
`excluded_out_of_period`, `excluded_late` or `quarantined`; the counts are in
the manifest and always add up to the number of lines.

**Reproducibility.** Output contains no clock time, random id, host name or
absolute path, and ordering is explicit everywhere. Identical input bytes with
the same code, Python version and implementation, and time-zone data/provenance
give byte-identical outputs. The manifest records these prerequisites so a
difference can be explained; changing Python versions changes the manifest even
when the invoices, quarantine and audit bytes stay identical.

## Tracing a number back to its events

```sh
rvn-ledger explain --out out                       # every quarantined record and why
rvn-ledger explain --out out --event-id ev_123     # one event, including its duplicate copies
rvn-ledger explain --out out --line 42 --events data/events.jsonl   # one source line, with its original field values
rvn-ledger explain --out out --account acct_013   # one invoice: every line's formula, its source events, the credit and the total
```

For an invoice line, `audit.json` → `invoices.<account>.usage_sources.<metric>`
lists the accepted events (id, source line, units) that sum to it, and
`usage_lines` / `subscription_lines` carry the formula that produced each amount.

## How correctness is checked

- **`rvn-ledger check`** re-hashes the published files against the manifest and
  reconciles invoices with the audit trail: dispositions cover every line once,
  every accepted event is billed exactly once, units are conserved into tiers,
  subtotal = sum of lines, total = subtotal − credit and never negative, every
  amount recomputes from its recorded inputs, every event identity has one
  canonical line and every duplicate points to it, invoice accounts match the
  audit accounts exactly once, numeric fields are integers (never floats or
  booleans), all recorded tariffs are valid even with zero usage, the manifest
  rules are a valid period that the segments, the period-end plan and the metrics
  agree with, and the manifest names the time-zone
  data of exactly the zones that were billed. `run` performs the same check before
  it writes anything.
- **`tests/test_hazards.py`** — one end-to-end test per billing rule and input
  hazard, small enough to verify by hand. Start here.
- **`tests/test_invariants.py`** — seeded generated inputs (duplicates, junk,
  late and out-of-period events) checked against the invariants above, plus
  independence from later duplicates, from line order (given distinct
  `ingest_seq` values) and from other accounts (given distinct `event_id`s).
- **`tests/fixtures.py`** — one hand-derived input set covering every hazard,
  with its expected invoices and quarantine.
- **`scripts/e2e.py`** — drives the installed command on the demo from outside
  the checkout and compares against the hand-derived outputs.

```sh
python -m unittest discover -s tests
python -O -m unittest discover -s tests      # checks are real code, not asserts
python scripts/e2e.py                        # needs a non-editable install: pip install .
```

CI runs all three on Linux and Windows with Python 3.11 and 3.13.

## Project layout

```text
src/rvn_ledger/
  cli.py           run / check / explain
  pipeline.py      one run: inputs in, four documents out
  inputs.py        strict JSON parsing; every event line kept with its hash
  selection.py     deduplication and the one-outcome-per-line classification   (rule 2)
  validation.py    event field checks and quarantine reasons                    (rule 4)
  timing.py        local periods and the late cutoff                            (rules 1, 3)
  subscription.py  plan segments, proration, period-end plan                    (rules 5, 6, 9)
  tiers.py         tariffs and cumulative bracket splitting                     (rule 6)
  money.py         rounding, line amounts, credit                               (rules 5, 7, 8)
  aggregation.py   accepted units per account and metric, with sources
  invoice.py       invoice assembly in the output shape
  audit.py         audit trail and its reconciliation
  outputs.py       deterministic JSON and staged publication
  diagnostics.py   explain
examples/demo/     a hand-checked synthetic month
scripts/           e2e.py, benchmark.py
docs/history/      working notes from development review rounds (index in its README)
```

## Branches

- **`main`** — the command-line tool described here.
- **`feature/excel`** — optional Excel support: a checked report workbook and a
  lossless input-workbook export/import (`pip install ".[excel]"`).
- **`feature/independent-verifiers`** — standalone scripts that re-derive every
  line's status, the subscriptions, the aggregation and the invoices from the raw
  inputs without using the billing modules, and write hashed receipts.

## Further reading

- [ENGINEERING.md](ENGINEERING.md) — invariants, check precedence, and what changes at 1000× the volume.
- [MODEL_USE.md](MODEL_USE.md) — where a language model belongs in a system like this, and where it does not.
- [CHANGELOG.md](CHANGELOG.md)
