# Bank CSV exports

The connector id is `bank_csv`; it writes `transactions`. Revolut is the only real
bank mapping shipped. Example Bank is fictional and demonstrates a different layout.

## Get a Revolut export

1. Open Revolut and select the currency account you want to export.
2. Open the account's options and choose **Statement** (or **Account statement**).
3. Choose the date range, select **CSV**, and download or save the statement.
4. Repeat for other currency accounts. Import a CSV file, a folder of statements,
   or a ZIP containing CSVs. Set the import timezone to the export's local timezone.

Menu labels may vary by app version. PDF statements and translated header layouts
are not recognised by this mapping; create a user mapping for other CSV layouts.

## Layout and fields

The built-in Revolut CSV has one header row:

`Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance`

Header lookup trims whitespace and ignores case. Rows use comma delimiters,
UTF-8 (with optional BOM), dot decimals and local `%Y-%m-%d %H:%M:%S` timestamps.
Quoted commas and newlines are supported. ZIP members stream without disk extraction.

| Export field | Canonical use |
| --- | --- |
| Completed Date | `ts`; if empty, use Started Date |
| Started Date | Fallback `ts` |
| Amount, Fee | `amount = Amount - Fee`, decimal arithmetic |
| Currency | `currency`, also part of `account` |
| Description | `merchant_raw`; normalised `merchant`; transfer `counterparty` |
| Product | `account = revolut:{Product}:{Currency}` |
| Balance | Optional `balance` |
| Type | CARD_PAYMENT → card; TRANSFER → transfer; TOPUP → topup; EXCHANGE → exchange; FEE → fee; CARD_REFUND/REFUND → refund; otherwise other |
| State | Only COMPLETED is included; pending, reverted and other states are excluded |

Descriptions starting `To `, `Transfer from ` or `Payment from ` produce a
counterparty name. Category and contact_id remain empty for Revolut.
All unused columns are discarded, including personal or device identifiers; no
raw records or extras are retained in `meta`. Logs contain only skipped-row counts.
Invalid timestamps, amounts, missing required values and malformed row shapes are
skipped. Invalid mapping files raise an error naming the file and invalid field.

Local timestamps resolve with the import's IANA timezone: ambiguous times use
fold=0; nonexistent times shift forward by the DST gap. Canonical timestamps are UTC.

Merchant normalisation lowercases, removes URLs/domain tokens, `*terminal`
suffixes, `#digits`, runs of at least four digits, the words payment/purchase/card/pos,
and trailing two/three-letter country or city tokens. It collapses whitespace and
title-cases. An empty result falls back to the original description. This heuristic
can remove short words at the end of names; `merchant_raw` preserves the original.

## User mappings

Put YAML files in `$SHERD_HOME/bank_mappings/*.yaml` (default
`~/.sherd/bank_mappings`). They are loaded with the built-ins on each detection or
parse; there is no registration step. Mapping ids must be unique. Unknown fields
and inconsistent column alternatives are errors. Header matches score 0.95 when
all `detect.required_headers` are present; otherwise zero. If multiple mappings
match, the first built-in or lexically sorted user file wins; choose distinguishing
required headers to avoid this ambiguity.

```yaml
id: fictional_user_bank
display_name: Fictional user bank
detect:
  required_headers: [Date, Details, Debit, Credit]
csv:
  delimiter: ';'
  encoding: utf-8-sig
  decimal: ','
  thousands: '.'
  skip_rows: 2
  skip_footer_matching: '^END OF STATEMENT'
ts:
  column: Date
  fallback_column: Posted
  format: '%d.%m.%Y'
amount:
  debit: Debit
  credit: Credit
  fee: Charges
currency:
  fixed: RON
description: Details
balance: Balance
account: 'fictional:current'
counterparty:
  regex: '^To (?P<name>.+)$'
kind:
  column: Type
  map:
    PAYMENT: card
  default: other
include:
  column: Status
  allowed: [COMPLETED]
category: Category
```

- `csv` defaults to comma, UTF-8 with optional BOM, dot decimals, no thousands
  separator, no preamble or footer. `skip_rows` skips physical lines before the
  header; `skip_footer_matching` is a regex against the first cell and ends parsing.
- `ts.format` is a strftime format; an aware timestamp with `%z` retains its offset.
- `amount` takes either `column` or both `debit` and `credit`. Debit/credit mode uses
  credit minus debit, with blank cells treated as zero. An empty single amount
  column is invalid. Optional `sign: invert` flips the base amount; an optional fee
  is then subtracted. Blank fees are zero.
- `currency` takes `column` or `fixed`. `description` and optional `balance` and
  `category` are column names. Blank balance/category produce null values.
- `account` is a fixed string or a template with simple `{column}` placeholders.
- Optional `counterparty` takes a `column` or a regex on the description, requiring
  a named `name` group. Blank or unmatched values produce null.
- Optional `kind` maps source values to card/transfer/fee/topup/exchange/refund/other;
  the default is other. Optional `include.allowed` uses trimmed, case-sensitive
  value matching (as do kind maps).

See `mappings/example_bank.yaml`: semicolon CSV, comma decimals, period thousands,
two preamble lines, date-only timestamps, debit/credit amounts, fixed RON and a footer.

## Identity, fixtures and memory

`source_row_id = content_hash(mapping id, account, UTC ts isoformat, amount,
currency, merchant_raw, balance, occurrence_index)`. Occurrences reset per CSV,
so identical payments in one statement survive while overlapping exports dedupe.
Files and export dates never enter the hash. Duplicate counting uses a temporary
on-disk dbm index, removed when the iterator finishes or closes. It requires disk
space proportional to distinct transactions, while parsing memory stays bounded.

Synthetic fixtures cover Revolut basic/overlap/edge cases, Example Bank and a user
mapping. The overlap golden imports the first statement; the integration test
imports both, preserving repeated transactions. The user-mapping fixture has a
separate test that installs its YAML into a temporary SHERD_HOME and uses the same
golden harness; it is excluded from default demo fixture discovery because it
requires that temporary configuration. Goldens were generated and reviewed row by row.

Acceptance evidence in `tests/bank_csv/test_bank_csv.py`:

| Requirement | Evidence |
| --- | --- |
| Directory/file detection and 0.95/0 confidence | test_detect, test_detect_negative, test_detect_bounded |
| Header trimming/case and ZIP support | test_header_matching_and_zip |
| Pydantic mapping errors naming file and field | test_mapping_validation, test_user_mapping |
| Fee, sign, include filter and kind maps | test_user_mapping, test_revolut_fee_filter_kinds_identity |
| Decimal/thousands, debit/credit, preamble/footer and category | test_example_decimal_debit_credit_footer |
| Local/DST timestamps and malformed-row count | test_malformed_records_and_dst |
| Overlap, exact duplicate occurrences and second import inserts zero | test_overlap_occurrences_and_idempotency |
| Merchant normalisation (23 cases) | test_normalisation |
| Quoted newlines and malformed CSV shape | test_quoted_newlines_and_malformed_shape |
| 50 MB parse under 200 MB RSS | test_streaming, using synth_bank_csv.GENERATOR |
| Golden fixtures | shared test_goldens.py and test_user_mapping |

Manual benchmark on macOS/Python 3.12, seed 42: generated **524,288,281 bytes**
(500 MiB), parsed **1,034,099 rows** in **40.56 seconds**, peak RSS **69.19 MiB**.
Timing covers the fresh-process parser and its on-disk occurrence index, excluding
raw-file generation. The benchmark file and occurrence index were removed afterward.
