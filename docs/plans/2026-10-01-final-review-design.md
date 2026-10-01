# Final review implementation

The user authorized all final-review fixes and improvements, including publishing
the completed result to `main`. The billing rules and supplied-data invoice
contract stay authoritative.

## Decisions

- Release as **1.2.0**, with one version source, a matching immutable Git tag,
  pinned installation commands, and a CI check that tagged releases match the
  installed package. Keep the historical `v1.1.0` tag unchanged.
- Keep the assignment CLI focused on `run`, `check`, and `explain`. Preserve
  Excel as an optional `[excel]` dependency group with its own documentation.
  Missing Excel support produces an actionable input error, not an import crash.
- Bound JSON integers to **256 decimal digits**, including ignored fields. This
  is an explicit parser resource limit, not a tariff or rounding rule. It leaves
  room for products and aggregate growth below Python's minimum configurable
  640-digit decimal-conversion threshold. Oversized event integers quarantine
  the record; oversized reference configuration fails before publication.
  Recover unambiguous event identity using the same numeric limit.
- Reject encrypted ZIP members and unsupported compression before opening XML,
  and translate decompression failures into an input error. Never create an
  import destination for a rejected workbook.
- Prepare a frozen, typed billing context once: metric order, account bounds,
  subscriptions, credits, and period-end tariffs. Classify once and aggregate
  once; share those aggregates between invoice assembly and the audit trail.
  Preserve the existing module entry points used by tests and verifier scripts.
- Add a committed synthetic demo with hand-derived expected invoices. CI uses
  the installed console command from a separate working directory for the full
  JSON/Excel round trip and verifies a base installation without Excel.
- Add an opt-in, reproducible benchmark for 1x, 10x, and 100x synthetic batches.
  Publish measured time and peak traced Python allocations, not an unmeasured
  claim of 1000x capacity. Keep private assignment inputs out of Git.

## Alternatives considered

Keeping Excel mandatory would preserve the previous installation footprint but
keep optional transport dependencies in the assignment's core. Removing Excel
would discard working functionality. An optional extra preserves it explicitly.

Disabling Python's integer conversion limit globally would accept the reported
edge case but weaken a process-wide resource boundary. Catching only the late
formatting exception would still let one record abort the entire batch. A
documented input bound makes admission, arithmetic and serialization compatible.

A service or general dependency-injection framework is unnecessary. Small frozen
dataclasses and read-only mappings fit the existing batch design.

## Validation and completion

Reproduce the two new failures before fixing them, then test numeric boundaries,
canonical-copy behavior, bad archives and optional-dependency errors. Run the
full suite normally and under `-O`, exercise clean installed core/Excel packages,
and independently compare all supplied-data invoices and dispositions. Verify
repeated outputs and Excel round trips byte-for-byte. Inspect the final diff,
push `main`, wait for CI, publish and verify `v1.2.0`, and leave only `main` as a
remote branch.
