# Changelog

## 1.1.0 — 2026-10-01

- Add `explain` with source line/hash, ordered reasons, exact record selection, JSON output and optional hash-verified original field values.
- Add checked XLSX reports with invoices, line details, quarantine, source references, input hashes and currency-separated summary.
- Add explicit v1 lossless input-workbook export/import. JSON text is preserved without Excel numeric/date inference; arbitrary business-spreadsheet mapping is not included.
- Reject formulas, unsupported workbook shapes, unsafe XML/relationships and oversized archives; preserve large integer precision and refuse existing destinations.
- Install Excel/XML dependencies automatically. Existing `run`, `check` and billing rules are unchanged.
- `diff` and record resolution remain out of scope.
