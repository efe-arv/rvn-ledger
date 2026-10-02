# Output validation fixes

The review of main `1533ceb` reproduced five validation defects. Fix them without
changing valid invoices, event decisions, rounding, tariffs or the output format.

- Require unique invoice account ids matching the audit account keys exactly.
- Build the eligible canonical event map before checking duplicate pointers, so
  unreadable ingestion sequences cannot become canonical targets. Source-line
  order must not matter: the winning copy may appear later in the file.
- Reject non-string account time zones before using them as cache keys.
- Check the exact integer types of money, counts, units and source references
  before relying on numeric equality; Python floats and booleans can compare
  equal to integers.
- Reuse the tariff document validator for configuration and audit, including
  unused brackets and zero-usage accounts.

Targeted guards preserve the standard-library implementation and its existing
module boundaries. A second billing implementation or a schema framework would
add maintenance and dependencies without helping these fixes. A blanket CLI
exception handler would hide the timezone defect instead of validating input.

Regression tests must first fail on the reviewed version. They cover direct
reconciliation and installed `check`/`explain` with deliberately rehashed invalid
documents. Run the full suite normally and under optimized Python, installed
CLI E2E, independent supplied-data comparisons and external hostile-input probes.
Valid invoice, quarantine and audit bytes must match the baseline; manifest
source hashes change with the implementation. Check the final pushed commit's
Linux/Windows CI matrix.

Clarify the documentation's runtime/provenance prerequisites for identical
outputs and configuration validation before event parsing (not before file I/O).
