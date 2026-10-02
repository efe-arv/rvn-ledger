# Changelog

## 2.0.0 — 2026-10-02

- `main` is now the command-line tool only: `run`, `check`, `explain`. Excel support moved to the
  `feature/excel` branch and the standalone verification scripts to `feature/independent-verifiers`.
  The root `cli.py` shim is gone; use `rvn-ledger` or `python -m rvn_ledger`.
- Every billing rule is implemented once. Audit reconciliation now checks rule-independent invariants and
  recomputes each line from the recorded inputs through the same `money` and `tiers` functions, instead of a
  second copy of the arithmetic. `audit.json` records the tariff each usage line was priced on.
- Currencies are no longer a hard-coded list: any three-letter code is accepted if `plans.json` prices it.
- Tests reorganised: `tests/test_hazards.py` (one test per rule), `tests/test_invariants.py`, shared fixtures in
  `tests/fixtures.py`; review-round test files folded into the module they test.
- Timestamp fractions are capped at 256 digits (a longer one is `invalid_ts`), so whether a timestamp
  parses no longer depends on Python's int-string limit (`PYTHONINTMAXSTRDIGITS`).
- Stale verifier receipts and logs removed from the repository root; the scripts that write them live on
  `feature/independent-verifiers`.
- Apart from the timestamp-fraction limit, invoices and quarantine output for the same inputs are
  byte-identical to 1.2.0.

Two further external review rounds on the verification layer, merged before release.
Billing arithmetic and the `invoices.json`, `quarantine.json` and `audit.json` bytes are
unchanged by them; the manifest gained `versions.timezones`.

- `check` (and the pre-publication reconciliation) now ties the audit trail to the manifest rules: the
  rules must be a valid billing period read through the same strict parsers as `period.json`; every
  subscription segment must lie inside it, use its `days_in_period`, be chronological and non-overlapping;
  `period_end_plan_id` must be the plan covering the final local day and the plan every usage line was
  priced on; `billable_units` must list the manifest metrics in their order. Segments moved outside the
  period or contradictory manifest rules were previously accepted when the arithmetic still recomputed.
- `check` now requires every event identity to have exactly one canonical line (a line that never entered
  deduplication because its `ingest_seq` was unreadable is exempt) and every accepted event to carry a
  nonempty string identity. Two accepted lines billing the same `event_id` were previously accepted.
- The manifest records, per billed time zone, where zoneinfo actually resolved it from (`system` TZPATH
  file or the `tzdata` package), that source's version and the SHA-256 of the TZif bytes
  (`versions.timezones`). `versions.tzdata` is now a summary of those records and reads `mixed: ...` when
  zones came from different databases; it no longer assumes that the database holding the first `UTC` file
  on TZPATH served every zone. A system version is recorded as `2026c`, no longer `version 2026c`. `check`
  requires the provenance to name exactly the invoiced zones.
- `pipeline.tzdata_version` now takes the zone keys to summarise; `scripts/benchmark.py` reports the
  manifest's own summary.
- Each billed zone's rules are now built with `ZoneInfo.from_file` from one read of its TZif bytes, and
  `versions.timezones` records exactly those bytes. Previously the bounds came from the process-wide
  `ZoneInfo(key)` cache, which keeps whatever the first lookup in the process found and ignores a later
  `zoneinfo.reset_tzpath` or changed file, while the hash was taken by re-reading the file afterwards,
  so the record could describe rules the run did not bill on. Every account in one zone shares one
  resolution (`PeriodBounds.zone`); two different byte sequences under one key in one run is an error.
  The usable-key listing follows the search path in force at run time.
- A UTF-8 byte order mark at the start of `events.jsonl` is removed before the first line is parsed or
  its identity salvaged; the line's SHA-256 in `audit.json` and the file hash in the manifest still
  cover the BOM bytes. Previously the first record was quarantined as `invalid_json` without an
  identity, so a later copy of the same `event_id` became canonical and could bill usage. A BOM on any
  other line, or in a configuration file, is still rejected.
- `explain` failures are no longer reported as `publication failed`: an unreadable `--events` file is
  an input error (`error: --events: cannot read ...`) and a published set that fails its check is
  `check failed: ...`, both exit 2 as before.

- Require unique invoice account ids matching the audit accounts exactly, and
  require each duplicate to point to its eligible canonical line. A quarantined
  line with an unreadable ingestion sequence cannot be that target.
- Check exact integer types throughout invoice/audit numeric fields and manifest
  counts/totals; equal-valued floats and booleans are rejected by `check` and
  `explain`, as well as pre-publication reconciliation.
- Validate complete tariffs through the same parser for configuration and audit,
  including unused brackets and accounts with zero usage.
- Reject list/object account time zones as controlled configuration errors
  (exit 2), before any publication, instead of raising an unhandled TypeError.
- Clarify the Python runtime and time-zone provenance prerequisites for identical
  output bytes, and that configuration validation precedes event parsing, not
  the CLI's file reads.

## 1.2.0 — 2026-10-01

- Include the manifest-input validation, source receipt hashes and Windows Unicode display fixes in a new pinned release.
- Use one version source for package metadata, CLI and run manifests; validate release tags and installation documentation in CI.
- Bound JSON integers to 256 decimal digits so admitted arithmetic remains serializable; quarantine oversized events and reject oversized reference data before publication.
- Reject encrypted and unsupported ZIP members and report malformed compression as controlled XLSX input errors.
- Resolve account bounds, subscriptions, credits and tariffs once per run; share one usage aggregation between invoices and audit.
- Move spreadsheet dependencies to the optional `[excel]` extra and document the extension separately. Existing Excel commands require this extra when upgrading.
- Add a synthetic demo, hand-derived expected invoices, installed-package core/Excel E2E gates and an opt-in scaling benchmark.

## 1.1.0 — 2026-10-01

- Add `explain` with source line/hash, ordered reasons, exact record selection, JSON output and optional hash-verified original field values.
- Add checked XLSX reports with invoices, line details, quarantine, source references, input hashes and currency-separated summary.
- Add explicit v1 lossless input-workbook export/import. JSON text is preserved without Excel numeric/date inference; arbitrary business-spreadsheet mapping is not included.
- Reject formulas, unsupported workbook shapes, unsafe XML/relationships and oversized archives; preserve large integer precision and refuse existing destinations.
- Install Excel/XML dependencies automatically. Existing `run`, `check` and billing rules are unchanged.
- `diff` and record resolution remain out of scope.
