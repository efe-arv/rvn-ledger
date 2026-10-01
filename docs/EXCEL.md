# Optional Excel extension

Excel is a convenience extension after the core homework delivery. The billing
rules, JSON inputs and checked JSON output set remain authoritative. The base
installation supports `run`, `check` and `explain` without spreadsheet libraries.

```sh
uv tool install --force "rvn-ledger[excel] @ git+https://github.com/efe-arv/rvn-ledger.git@v1.2.0"
```

## Report export

```sh
rvn-ledger export --format xlsx --out ./out --file ./ledger-report.xlsx
```

Sheets: Summary, Invoices, InvoiceLines, Quarantine, Sources, InputHashes. Filters,
frozen headers, exact minor-unit amounts and separate currency totals support
review. Integers of at least 10^15 are text to avoid Excel precision loss.
Untrusted strings are text, never formulas. A report is not importable billing
input. Existing workbook paths are never overwritten.

## Lossless input transport

```sh
rvn-ledger export --kind inputs --input-dir ./data --file ./ledger-inputs.xlsx
rvn-ledger import --file ./ledger-inputs.xlsx --input-dir ./imported-data
rvn-ledger run --input-dir ./imported-data --out ./imported-out
rvn-ledger check --out ./imported-out
```

The v1 workbook schema transports JSON/JSONL explicitly; it does not interpret
arbitrary business spreadsheets. Normal JSON event text is editable in
`Events.text`. Nested account/plan/period configuration remains JSON in
`Config.text`. No date, money or numeric conversion is inferred.

Exact sheet order and headers:

| Sheet | Headers | Contract |
|---|---|---|
| Schema | `schema` | One data cell: `rvn-ledger-inputs-v1` |
| Events | `line`, `encoding`, `text`, `eol` | Consecutive physical lines starting at 1; `utf8` or `base64`; `LF`, or `NONE` for the final line only |
| Config | `file`, `part`, `encoding`, `text` | Only accounts.json, plans.json, period.json; consecutive parts starting at 1 for each file |

Each Events row carries exactly one physical source line. An empty event stream
has no data rows. Config parts concatenate into the exact original file. Large
configurations are chunked. Bytes unrepresentable in XML/Excel, including CR
line endings, use base64; editing base64 requires understanding the byte format.

Import validates the workbook, reconstructs inputs, runs billing and reconciles
before writing a **new** input directory. Invalid configuration creates no
destination. Invalid events remain quarantined under the core rules, including
the 256-digit JSON integer limit. Existing destinations, including empty
directories, are rejected. Caught write errors clean up the new directory;
publication is not crash-atomic. After interruption preserve evidence and rerun
to a different new directory.

Formulas, cached formulas, Excel errors, numeric/date cells in text columns,
unknown sheets/headers, external relationships, embedded objects, macros,
encrypted ZIP members, unsupported ZIP flags/compression and malformed XML are
rejected. Supported ZIP methods are stored and deflate.

Limits: 20 MiB archive/decoded inputs, 64 MiB expanded archive, fewer than
100,000 rows per exported sheet, and 30,000 UTF-16 code units per text cell.
Use direct JSONL for unusually long source lines.

Commands accept `--json`; rejected input returns exit 2 and diagnostics on
stderr. XLSX files are not encrypted; store them according to the sensitivity
of their input/output data.
