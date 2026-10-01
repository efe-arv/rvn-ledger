# Engineering notes

This document answers three questions: which invariants the ledger keeps, the
order in which it checks things, and what would change at 1000× the volume. It
then lists the assumptions made where the rules are silent, and what was left
out. The billing rules themselves, and the module that implements each one,
are in the [README](README.md#billing-rules-and-where-each-one-lives).

## 1. Invariants

| # | Invariant | Enforced by | Tested by |
|---|---|---|---|
| 1 | Money is integer minor units everywhere. No float is accepted as an amount or written to any output. | `money.py` rejects non-integers, booleans and negatives; `outputs.serialize` refuses floats | `test_money`, `test_outputs`, `test_hazards` (rule 7) |
| 2 | Rounding happens once per line, half up; the subtotal is the sum of already-rounded lines. | `money.round_half_up`, `money.usage_amount`, `money.subscription_amount` | `test_money`, `test_hazards` (rules 5, 7) |
| 3 | `total = subtotal − credit_applied`, never negative; `credit_applied + credit_remaining = credit`; more credit never raises a total. | `money.apply_credit`, re-checked by `audit.reconcile` | `test_invariants`, `test_hazards` (rule 8) |
| 4 | Every raw line ends in exactly one outcome — `accepted`, `duplicate_ignored`, `excluded_out_of_period`, `excluded_late` or `quarantined` — and the counts add up to the number of lines. | `selection` raises on a missing or second outcome; `audit.reconcile` | `test_selection`, `test_invariants` |
| 5 | Every accepted event is billed exactly once, and units are conserved: accepted units = `billable_units` = units across the tier lines. | `aggregation`; `audit.reconcile` checks each source is used once | `test_audit`, `test_invariants` |
| 6 | A later duplicate, the order of lines in the file, or another account's events never change an invoice. | `selection.copy_precedence` orders by `ingest_seq`, not arrival; aggregation is per account | `test_invariants` (seeded generated inputs) |
| 7 | Currencies never mix and are never converted: every fee and tariff is looked up in the account's currency; totals are kept per currency. | `subscription`, `tiers.metric_tiers`; `totals_by_currency` in the manifest | `test_invoice`, `test_hazards` (rule 9) |
| 8 | Periods are half-open in local time; the late cutoff is the local period end plus elapsed hours; proration counts local calendar days, so DST cannot add or remove a day. | `timing.period_bounds`, `timing.classify_time`, `subscription` | `test_timing`, `test_subscription`, `test_hazards` (rules 1, 3, 5) |
| 9 | Same input bytes + same code + same time-zone database → byte-identical outputs. No clock time, random id, host name or absolute path is written; every ordering is explicit. | `outputs.serialize`, `pipeline` (the manifest records input hashes, code hashes, Python and tzdata versions) | `test_pipeline` (reruns from another working directory and `TZ`), `test_invariants`, `test_outputs` |
| 10 | Every invoice line traces back to its events and its formula, and the outputs reconcile with that trail before anything is written. | `audit.build_audit`, `audit.reconcile` | `test_audit` |
| 11 | A bad event never stops a run; bad configuration always does, before any output exists. | `inputs` and `validation` (events) vs `context.prepare_context` (configuration) | `test_pipeline`, `test_hazards` (rule 4) |

**How they hold at runtime.** `rvn-ledger run` calls `audit.reconcile` before
publishing. It checks invariants 3, 4, 5 and 10 and recomputes every line from
the recorded inputs (fee, days, tariff, credit) through the same `money` and
`tiers` functions that built the invoice. Any mismatch exits with code 1 and
writes nothing. `rvn-ledger check` repeats the reconciliation on published
files and verifies their hashes against the manifest. The checks are ordinary
code, not `assert`s, so they still run under `python -O`.

## 2. Check precedence

**Configuration first.** Accounts (ids, IANA time zones, credits), the period
(its dates must agree with `days_in_period`) and plans (a fee for every plan an
account uses and a tariff for every metric on its period-end plan, all in the
account's currency) are validated before a single event is read. A
configuration error stops the run: it would make every invoice suspect, and it
cannot be quarantined line by line.

**Then each event line, in this order:**

1. **Parse.** Invalid UTF-8 or JSON, a value that is not an object, or repeated
   keys → quarantined (`invalid_utf8`, `invalid_json`, `not_object`, …). If the
   line's `event_id` and `ingest_seq` can still be read unambiguously, it keeps
   its place in step 3, so a later valid copy cannot replace it.
2. **Identity.** A non-empty string `event_id` and an integer `ingest_seq` are
   required; without them a line cannot be deduplicated → quarantined
   (`invalid_event_id`, `invalid_ingest_seq`).
3. **Deduplicate.** Among copies of one `event_id`, the lowest `ingest_seq`
   wins; the rest become `duplicate_ignored`. This runs *before* validation
   because rule 2 says the first copy wins "even if the payload differs": a
   later, valid-looking copy must not replace a broken first one, and validity
   must not influence which copy is chosen.
4. **Validate the winner.** Reasons are collected in a fixed order:
   `unknown_account`, `unknown_metric`, `invalid_units`, `nonpositive_units`,
   `invalid_ts`, `invalid_ingested_at`. All of them go to `audit.json`; the first
   goes to `quarantine.json`. This runs before the time checks because the
   period depends on the account's time zone and a readable `ts`.
5. **Period.** `ts` outside the account's local `[start, end)` →
   `excluded_out_of_period`.
6. **Late.** `ingested_at` more than `late_cutoff_hours_after_period_end` (48)
   hours after the account's local period end → `excluded_late`. Period comes
   first, so an event that is both is reported as out of period; neither is
   billed, only the label differs.
7. **Accept** and add the units to the account and metric.

**Then each account:** subscription segments → usage priced on the period-end
plan, split over cumulative tiers → each line rounded → subtotal → credit →
total.

## 3. What changes at 1000× the volume

The supplied month has 1,078 events, so 1000× is about one million. That was
measured rather than estimated (`scripts/benchmark.py`, results in
[docs/benchmark-results.json](docs/benchmark-results.json); Windows 11,
CPython 3.12, synthetic valid events for two accounts, full `run` including
reconciliation, publishing and the post-publish check):

| Scale | Events | Time (median of 3) | Per event | Peak Python memory | Output on disk |
|---|---:|---:|---:|---:|---:|
| 1× | 1,000 | 0.08 s | 78 µs | 3 MiB | 0.3 MiB |
| 10× | 10,000 | 0.54 s | 54 µs | 22 MiB | 2.9 MiB |
| 100× | 100,000 | 5.4 s | 54 µs | 216 MiB | 29 MiB |
| 1000× | 1,000,000 | 78 s | 78 µs | 2.1 GiB | 296 MiB |

Peak memory is Python allocations under `tracemalloc`, from a separate traced
run so tracing does not slow the timed ones. As process memory (peak working
set), the 1000× run used 2.5 GiB of RAM for a 170 MiB input file; almost all of
the 296 MiB output is `audit.json`. The 1× per-event figure is mostly fixed
start-up cost.

**What this says.** A million events a month is still a single-machine batch
job, and 78 seconds is not a problem for monthly billing. Memory is: every line,
parsed event and decision is held at once, the per-event cost rises at 1000×
(54 → 78 µs) as the process carries ~2 GiB of live objects, and 10,000× would
not fit. So the first change is bounded memory, not a distributed system.

**Where the time goes**, as approximate shares from profiling a 100,000-event
run: reading and strict parsing about a third, two-thirds of that being the
check for unpaired surrogates in every string; classification about 30%, mostly
parsing timestamps into exact fractions; writing the indented JSON outputs
about a quarter; reconciliation about 13% (it runs before publishing and again
in the post-publish check). Deduplication, aggregation and pricing are each
under 2%.

**What I would change, in order:**

1. **Two passes over the file instead of loading it.** Pass 1 keeps only the
   winning copy per `event_id` (`event_id → (ingest_seq, line)`). Pass 2 reads
   again, classifies each line against the winners and adds accepted units into
   per-account, per-metric totals. Memory becomes proportional to distinct event
   ids plus accounts × metrics. If the id index outgrows memory, sort by
   `event_id` on disk or use a disk-backed index; the result is the same.
2. **Write the audit as it streams.** One decision per line appended to a JSONL
   file in source order, with invoice lines pointing at source line numbers.
   Full traceability still costs storage proportional to the input; it just
   leaves memory.
3. **Cheaper per-event checks, same results.** Check for surrogates only in the
   fields that are read; parse timestamps into integer seconds and nanoseconds
   instead of fractions; write `audit.json` compactly; reconcile once and let
   `check` rely on the hashes plus a streaming reconciliation. The byte-identical
   and hand-verified tests guard each of these.
4. **Parallelise only after global deduplication.** Splitting raw events by
   account is wrong: copies of one `event_id` can name different accounts (the
   demo has one), so the winner must be chosen globally first. Shard the
   deduplication by `hash(event_id)`, then route winners to account shards for
   pricing. A single very large account can be split by metric, because totals
   only add. Merge invoices sorted by `account_id` so worker order never shows
   in the output.
5. **Operate it as a scheduled close.** A month can only be closed after the
   last account's late cutoff (the New York accounts here: local period end +
   48 h). Earlier runs are safe as provisional invoices because reruns are
   idempotent. At the supplied quarantine rate (19 of 1,078, about 1.8%),
   1000× means roughly 19,000 quarantined records a month, which needs triage
   grouped by reason and source rather than reading `quarantine.json` by hand
   (see [MODEL_USE.md](MODEL_USE.md)). Each run keeps its manifest binding
   inputs, code and time-zone data, so any rerun can be explained.

**What I would not add for 1000×:** a database, a queue or a service. A million
events a month does not need them; continuous ingestion or incremental
billing would, and that is a different product decision.

## 4. Assumptions

Where the rules are silent, the ledger makes these choices, and each one is
pinned by a test:

- **Equal `ingest_seq`** for two copies of one event: the earlier line in the file wins.
- **A broken first copy stays broken.** If the winning copy is invalid it is quarantined; a later valid copy is still a duplicate.
- **Late and out-of-period events are excluded, not quarantined.** `quarantine.json` only holds records that are unreadable or fail a rule-4 check.
- **Exactly 48 hours after the period end is on time.** Only "more than 48 hours" is late.
- **`quarantined_count`** counts quarantined records that name that account. Records for unknown accounts, and unreadable lines, belong to no invoice and appear only in `quarantine.json`.
- **Units must be a JSON integer**: `1.0`, `"5"` and `true` are `invalid_units`.
- **Timestamps need seconds and an explicit offset** (`Z` or `±HH:MM`); anything else is `invalid_ts` / `invalid_ingested_at`.
- **Tiers with no units produce no line**; an account with no usage gets subscription lines only.
- **The period-end plan** is the segment covering the last local day of the period; a segment starting on the exclusive end date does not count. If no segment covers that day, the configuration is rejected.
- **Days covered by no plan segment are not charged.** Overlapping segments are rejected.
- **A missing `credit_minor` is an error**, not zero.
- **JSON integers are limited to 256 digits.** Larger values in an event quarantine it (`integer_too_large`); in configuration they stop the run.

## 5. What was cut

- **Bounded memory.** The whole month is held in memory. That is fine up to the
  volumes measured above; section 3 describes the change for more.
- **Crash-safe publishing.** Outputs are staged and swapped in, with rollback
  on errors, but a power loss mid-write is only detected (by `check` and the
  staging directory), not prevented.
- **Signatures.** Manifest hashes detect changes; they do not prove who produced a file.
- **Incremental billing, corrections and quarantine resolution.** Each run bills
  one closed period from scratch; there is no workflow for fixing and
  re-admitting quarantined records.
- **Kept off `main`:** Excel import/export (`feature/excel`) and standalone
  scripts that re-derive every result from the raw inputs
  (`feature/independent-verifiers`). Both work; neither is needed to bill.
