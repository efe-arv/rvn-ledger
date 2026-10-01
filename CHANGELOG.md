# Changelog

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
