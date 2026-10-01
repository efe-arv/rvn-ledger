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
