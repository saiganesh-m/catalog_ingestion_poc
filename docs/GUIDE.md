# Catalog Ingestion POC – Complete Guide

This guide explains the whole project: what it does, how every file fits together, how data flows,
how to run, demo, test and inspect it, and how to move it to PostgreSQL or MySQL.
The short version is in the [README](../README.md).

## Contents

1. [What the system does](#1-what-the-system-does)
2. [Architecture and how the files connect](#2-architecture-and-how-the-files-connect)
3. [File-by-file reference](#3-file-by-file-reference)
4. [The pipeline step by step](#4-the-pipeline-step-by-step)
5. [The database](#5-the-database)
6. [Configuration (.env)](#6-configuration-env)
7. [Running it: scripts, CLI and API](#7-running-it-scripts-cli-and-api)
8. [Demoing with a new workbook](#8-demoing-with-a-new-workbook)
9. [Checking the data in the database](#9-checking-the-data-in-the-database)
10. [Switching database: PostgreSQL and MySQL](#10-switching-database-postgresql-and-mysql)
11. [The test suite](#11-the-test-suite)
12. [JEV modes, evidence and cost](#12-jev-modes-evidence-and-cost)
13. [Troubleshooting](#13-troubleshooting)
14. [Known limitations](#14-known-limitations)
15. [Glossary](#15-glossary)

---

## 1. What the system does

Suppliers send product catalogs as Excel files. Every supplier names columns differently (`Part Number`,
`Product Code`, `SKU`), writes prices differently (`$1,250.00`, `€ 2.450,50`, `₹ 1,85,000.00`) and uses
different units (`1500 mm`, `78 in`, `50 lbs`). The system turns any such file into one standard product
record per row:

| Field | Type | Rule |
|---|---|---|
| `sku` | text | required |
| `name` | text | required |
| `unit_price` | decimal | required, ≥ 0 |
| `currency` | ISO code | required, one of `SUPPORTED_CURRENCIES` |
| `payload_kg` | number | optional, kilograms, > 0 |
| `reach_m` | number | optional, metres, > 0 |

Every row is **staged** with exactly one status and, unless it is ready, a reason:

- `ready` – valid and every needed column was identified with confidence
- `needs_review` – something is uncertain or conflicting; a human decides
- `rejected` – the row breaks a hard rule (blank SKU/name, unreadable or negative price)

`ready` rows are then **approved** into the final `products` table. Importing the same file again, or
approving again, never creates duplicates.

**The division of labour** – the central design decision:

| JEV (TypeSafe AI decision model) | Python |
|---|---|
| Decides **what each column means** (semantic, probabilistic) | Decides **whether each value is valid** (deterministic) |
| Never sees or changes row values beyond 3 samples per column | Parses, converts, validates, classifies, stores |
| Its confidence is recorded for audit | Has the final say: **a high JEV confidence never overrides a failed check** |

---

## 2. Architecture and how the files connect

### Layers

```
 ┌───────────────────────────── Entry points ─────────────────────────────┐
 │  app/main.py (REST API)   app/cli.py (command line)   scripts/*.py     │
 └────────────────────────────────┬───────────────────────────────────────┘
                                  │ call
 ┌────────────────────────────────▼───────────────────────────────────────┐
 │  app/routers/  ingest.py  staging.py  products.py   (HTTP only)        │
 └────────────────────────────────┬───────────────────────────────────────┘
                                  │ call
 ┌────────────────────────────────▼───────────────────────────────────────┐
 │  app/services/  (all business logic)                                   │
 │                                                                        │
 │   ingestion_service ──► excel_parser                                   │
 │          │         ──► jev/service ──► jev/client ──► TypeSafe API      │
 │          │                        ──► jev/mock   (offline stand-in)    │
 │          │                        ──► jev/evidence (audit files)       │
 │          │         ──► normalizer                                      │
 │          │         ──► validator ──► normalizer (result type)          │
 │   approval_service                                                     │
 │   review_service ──► normalizer, validator, approval_service           │
 └────────────────────────────────┬───────────────────────────────────────┘
                                  │ read / write
 ┌────────────────────────────────▼───────────────────────────────────────┐
 │  app/models/catalog.py (tables)  ◄── app/db/database.py (engine)       │
 │  app/schemas/catalog.py (API request/response shapes)                  │
 └────────────────────────────────┬───────────────────────────────────────┘
                                  │
                    catalog.db (SQLite) / PostgreSQL / MySQL

 app/core/config.py  ── read by every layer (settings from .env, project paths)
```

Rules that keep this clean:

- **Routers and the CLI contain no business logic**; they translate HTTP / command-line input into
  service calls and service results into responses. The API and the CLI therefore behave identically.
- **Services never import routers or the CLI.**
- **Only `jev/client.py` talks to the network**; only `jev/evidence.py` writes evidence files.
- **The normalizer reads and converts; the validator judges.** Neither touches the database.

### One request, end to end

`POST /api/v1/ingest/upload` with `sample_catalog.xlsx`:

1. `routers/ingest.py` reads the upload and calls `IngestionService().process_catalog(...)`.
2. `ingestion_service` hashes the bytes (SHA-256) and calls `excel_parser.parse(...)`.
3. It calls `jev/service.map_columns(headers, samples)`, which calls the TypeSafe API once or twice
   through `jev/client.py`, saves each exchange via `jev/evidence.py`, and returns a `JevMappingResult`.
4. For every row: `normalizer.normalize_row(...)` → `validator.validate_and_classify(...)` → a check
   against already-approved products → insert or update a `StagingRecord`.
5. One commit; an `IngestionSummary` goes back to the router, which returns it as JSON.

---

## 3. File-by-file reference

### Project root

| File | What it is |
|---|---|
| `README.md` | Overview and quick reference |
| `requirements.txt` | Python dependencies; optional PostgreSQL/MySQL drivers listed as comments |
| `pytest.ini` | Test discovery (`tests/`), lets tests `import app`, prints a summary of skips/failures |
| `.env` | Your settings and API key. **Never committed** |
| `.env.example` | All settings with explanations and no secrets – copy to `.env` |
| `.gitignore` | Keeps `.env`, `venv/`, `catalog.db`, caches out of git |
| `catalog.db` | Local SQLite database, created automatically, not committed |

### `app/` – the application package

| File | Responsibility | Used by |
|---|---|---|
| `main.py` | Creates the FastAPI app, registers the three routers, creates tables on startup (`lifespan`) | `uvicorn app.main:app` |
| `cli.py` | Command-line interface: `ingest`, `staging`, `show`, `approve`, `resolve`, `products`, `reset-db`. Each command opens a DB session and calls a service | `python -m app.cli …` |
| `core/config.py` | `Settings` loaded from `.env`; `BASE_DIR`, `DATA_DIR`, `OUTPUT_DIR`; makes a relative SQLite path relative to the project, not the current folder; `is_jev_live`, `supported_currency_list` | everything |
| `db/database.py` | SQLAlchemy `engine`, `SessionLocal`, `Base`; `get_db()` (one session per API request); `init_db()` (create missing tables); `pool_pre_ping` for server databases | models, routers, CLI, scripts, tests |
| `models/catalog.py` | Tables `staging_records` and `products` (see [section 5](#5-the-database)) | services, routers, CLI, scripts |
| `schemas/catalog.py` | Pydantic shapes: `StagingRecordRead`, `ProductRead`, `IngestionSummary`, `ApprovalSummary`, `ResolveRequest`, `ResolveResponse` – what the API accepts/returns and what Swagger documents | routers, services |
| `routers/ingest.py` | `POST /api/v1/ingest/upload` (400 for unreadable workbook/unknown sheet, 503 when `JEV_MODE=live` and JEV fails), `GET /api/v1/ingest/health` | `main.py` |
| `routers/staging.py` | `GET /api/v1/staging` (filters `status`, `filename`), `GET /api/v1/staging/{id}`, `POST /api/v1/staging/{id}/resolve` (404 / 409 on errors) | `main.py` |
| `routers/products.py` | `POST /api/v1/products/approve` (optional `staging_ids`), `GET /api/v1/products` | `main.py` |

### `app/services/` – the business logic

| File | Responsibility | Key functions |
|---|---|---|
| `excel_parser.py` | Reads a workbook with openpyxl (`data_only=True`): header row 1, data from row 2, skips blank rows, keeps 1-based row numbers, cell coordinates (`A2`), and up to 4 distinct sample values per column. Unknown sheet name → error | `ExcelParser.parse(source, filename, sheet_name)` → `WorkbookExtraction` |
| `jev/client.py` | HTTP POST to `{JEV_API_BASE_URL}/systemone` with `Authorization: Bearer <key>`. TLS via the OS certificate store (`truststore`), retries on 408/429/5xx/network errors with backoff (honours `Retry-After`), readable `JevApiError` (401 bad key, 422 malformed request…), records `x-typesafe-request-id` | `JevClient.system_one(payload)` → `JevHttpResult` |
| `jev/service.py` | **Column-mapping decision.** Builds the typed questions, asks in two rounds, assigns a verdict per column and a status per field, produces review reasons. Chooses live / mock / fallback per `JEV_MODE`; never mixes them | `JevService.map_columns(headers, samples)` → `JevMappingResult` |
| `jev/mock.py` | Offline stand-in: a keyword table that answers the same request with the same response shape. Model name says "mock … not a real JEV call" | `MockJevEngine.respond(payload)` |
| `jev/evidence.py` | Writes each live exchange (request, response, status, request id, timing, error) to `output/jev_evidence/jev_call_<timestamp>.json` with the key replaced by `***REDACTED***` | `save_jev_exchange(...)` |
| `normalizer.py` | Prices → `Decimal` + currency marker; currency resolution; payload → kg and reach → m. Anything it cannot read exactly becomes an error or issue, never a guess | `parse_decimal`, `Normalizer.normalize_row`, `parse_price`, `resolve_currency`, `normalize_payload_kg`, `normalize_reach_m` |
| `validator.py` | Turns a normalized row plus the mapping result into `ready` / `needs_review` / `rejected` with every reason; tracks duplicate SKUs within a file | `Validator.validate_and_classify(...)` → `ValidationOutcome` |
| `ingestion_service.py` | Orchestrates the whole import; idempotent staging (update in place by file hash + sheet + row); flags a known SKU with changed values; never overwrites a reviewer's decision | `IngestionService.process_catalog(source, filename, db, sheet_name)` → `IngestionSummary` |
| `approval_service.py` | Copies `ready`, not-yet-approved rows into `products`; never inserts a SKU twice; rolls back on a constraint violation | `ApprovalService.approve_eligible_records(db, staging_ids)` → `ApprovalSummary` |
| `review_service.py` | Resolves a `needs_review` row: reject, or approve with optional corrections that are re-parsed and re-validated exactly like ingestion; records the decision; an approval goes straight into products | `ReviewService.resolve(db, id, action, note, overrides)` |

### `scripts/` – operational tools

| Script | Purpose | Live JEV calls | Resets DB |
|---|---|---|---|
| `check_jev.py` | Connectivity check: strict live two-round mapping of 3 sample columns; prints answers, timings, request ids, evidence paths; exit code 1 on failure | 2 | no |
| `run_demo.py` | The six demonstration steps from the brief on one workbook (default the main sample); step 6 verifies idempotency with 5 PASS/FAIL checks; exit code 1 on failure | 4 | **yes** |
| `export_deliverables.py` | Imports both samples, approves, re-imports, and writes every `output/` deliverable including `EVALUATION.md` with 15 automated edge-case checks | ~6 | **yes** |
| `generate_sample_catalog.py` | Rebuilds `data/sample_catalog.xlsx` and `data/sample_catalog_supplier_b.xlsx` from the rows defined in the script | 0 | no |
| `inspect_db.py` | Read-only look into the database: overview, schema, or any `SELECT` | 0 | no |

### Other folders

| Folder | Content |
|---|---|
| `tests/` | The pytest suite ([section 11](#11-the-test-suite)) |
| `data/` | The two sample workbooks |
| `output/` | Generated deliverables: `EVALUATION.md`, `ingestion_summary.json`, `sample_normalized_output.json`, `approved_products.json`, `jev_call_evidence.json`, and `jev_evidence/` |
| `docs/` | `original_brief.txt` (the assignment as received), `ASSIGNMENT.md` (expanded restatement), this guide |

---

## 4. The pipeline step by step

We follow one row through the system: **row 5** of `sample_catalog.xlsx`.

| Part Number | Product Description | Unit Cost | Extended Amount | Rated Load | Working Reach | Currency | Warranty Duration |
|---|---|---|---|---|---|---|---|
| RB-1004 | Heavy Industrial Palletizer | $8,900.00 | $8,900.00 | 50 lbs | 78 in | USD | 5 Years |

### 4.1 Read the workbook – `excel_parser.py`

Output: headers, and for each row `{header: raw text}`, `{header: "A5"…}`, the Excel row number (5) and
the sheet name. Up to 3 sample values per column are later shown to JEV.

### 4.2 Map the columns – `jev/service.py`

**Round 1 – one `choice` question per column.** The request body (documented System One format):

```json
{
  "model": "jev-latest",
  "state": {
    "catalog_context": "Supplier product catalog for industrial robotics and automation equipment",
    "columns": [{"header": "Unit Cost", "sample_values": ["$1,250.00", "€ 2.450,50", "£ 850.00"]}, "..."]
  },
  "questions": {
    "map_col_2": {
      "type": "choice",
      "instructions": {
        "column": {"header": "Unit Cost", "sample_values": ["$1,250.00", "€ 2.450,50", "£ 850.00"]},
        "question": "Which standard product field does the supplier spreadsheet column `column` hold?"
      },
      "criteria": {
        "sku": "Unique product identifier, e.g. part number, product code, SKU or item code",
        "name": "Human-readable product name, title or description",
        "unit_price": "Price of ONE single unit (not an extended, line or order total)",
        "currency": "Currency code or currency name that the prices are quoted in",
        "payload_kg": "Payload or rated load: the mass the product can carry or lift",
        "reach_m": "Reach: the maximum working distance or arm length",
        "irrelevant": "None of the above, e.g. warranty, stock location, lead time, quantity or totals"
      }
    }
  }
}
```

JEV answers each question with the chosen option, a probability per option and a confidence:

```json
"map_col_2": {"type": "choice", "choice": "unit_price", "confidence": 1.0,
              "probabilities": {"unit_price": 1.0, "sku": 0.0, "...": 0.0}}
```

**Round 2 – a `noul` (yes/no, 0–1) check, only for columns that need it:**

- mapped to `unit_price`, `payload_kg` or `reach_m` (a wrong mapping silently corrupts numbers),
- confidence below `CONFIDENCE_THRESHOLD` (0.80),
- two or more columns chose the same field.

```json
"check_col_2": {"type": "noul", "instructions": {"column": {...},
   "candidate_field": {"name": "unit_price", "meaning": "Price of ONE single unit ..."},
   "question": "Does `column` hold `candidate_field`?"}}
→ {"type": "noul", "noul": 0.98}
```

If no column needs a check, round 2 is skipped.

**Verdict per column (Python):**

| Verdict | Condition | Effect |
|---|---|---|
| `confident` | choice confidence ≥ 0.80, not checked | mapped |
| `confirmed` | check ≥ 0.70 (even if the choice was < 0.80) | mapped |
| `ruled_out` | check ≤ 0.30 | treated as irrelevant |
| `uncertain` | check between 0.30 and 0.70, or missing | left **unmapped** |
| `irrelevant` | JEV chose `irrelevant` | ignored |
| `unanswered` | no usable answer | left unmapped |

**Status per target field (Python):**

| Status | Condition | Consequence for every row |
|---|---|---|
| `mapped` | exactly one confident/confirmed column | none |
| `conflict` | two or more confident/confirmed columns | review: "Competing columns for 'name' …" |
| `uncertain` | only uncertain candidates | review: "No confident column for 'payload_kg' (candidates: …)" |
| `absent` | no candidate | review if the field is required (`sku`, `name`, `unit_price`); fine if optional |

For our row the result is: `Part Number→sku`, `Product Description→name`, `Unit Cost→unit_price`
(check 0.98), `Extended Amount→irrelevant`, `Rated Load→payload_kg` (check 0.97),
`Working Reach→reach_m`, `Currency→currency`, `Warranty Duration→irrelevant`.

### 4.3 Normalize – `normalizer.py`

Only mapped columns are read.

**Numbers** (`parse_decimal`) – the whole text must be a number, otherwise it is an error:

| Pattern | Reading | Examples |
|---|---|---|
| `,` and `.` both present | the last one is the decimal separator | `1,250.00` → 1250.00, `2.450,50` → 2450.50 |
| only `,`, followed by exactly 3 digits | thousands | `1,500` → 1500, `1,85,000` → 185000 |
| only `,`, followed by 1–2 digits | decimal | `12,5` → 12.5 |
| `.` repeated | thousands | `1.250.000` → 1250000 |
| `'` | thousands | `1'250.00` → 1250.00 |
| leading `-` or `( )` | negative | `-$450` → -450, `(450.00)` → -450.00 |
| anything else | error | `1e3`, `1-2k`, `1,250-ish`, `approx 10` |

**Prices** (`parse_price`): currency symbols (`$ € £ ₹ ¥ US$ CA$ C$ AU$ A$ S$ Rs.`) and ISO codes
(`USD 1,250`, `1250 EUR`) are removed first and remembered; the rest must be a number.
Placeholders like `N/A`, `TBD`, `POA` are errors.

**Currency** (`resolve_currency`):

| Situation | Result |
|---|---|
| currency column `USD` (or `€`) | that code, if supported |
| no column, unambiguous symbol (`€`, `CA$`, `Rs.`) | the matching code |
| no column, `$` (USD/CAD/AUD/SGD) or `¥` (JPY/CNY) | **review** – never guessed |
| column and symbol disagree (`€ 10` + `USD`) | **review** – currency conflict |
| unsupported code | **review** |
| nothing at all | **review** |

**Units** (`normalize_payload_kg`, `normalize_reach_m`): `<number> <unit>` converted with exact factors.

| Mass → kg | Length → m |
|---|---|
| kg/kgs/kilogram(s) ×1, g/gram(s) ×0.001, t/tonne(s) ×1000, lb/lbs/pound(s) ×0.45359237, oz/ounce(s) ×0.028349523125 | m/metre(s)/meter(s) ×1, mm ×0.001, cm ×0.01, km ×1000, in/inch(es)/`"` ×0.0254, ft/foot/feet ×0.3048 |

A number without a unit uses the unit in the header if it has one in brackets (`Payload (kg)`,
`Reach [mm]`); otherwise it is an issue. Unknown units are an issue. Results are rounded to 3 decimals
and every conversion is noted. Empty cells stay `NULL`.

Our row: `$8,900.00` → 8900.00 + `$`; currency column `USD` agrees with `$` → USD;
`50 lbs` → 22.68 kg; `78 in` → 1.981 m. Notes: `Converted 50.0 lbs -> 22.68 kg`, `Converted 78.0 in -> 1.981 m`.

### 4.4 Validate – `validator.py`

Checked in this order:

1. **Hard rules → `rejected`**: blank SKU / blank name (only if a column was mapped for it), missing or
   unreadable price, negative price. A rejection ends the evaluation.
2. **Review rules → `needs_review`**: currency ambiguous/conflicting/unsupported/missing; unreadable,
   unknown-unit or ≤ 0 payload/reach; any mapping issue from JEV; SKU already seen earlier in the file.
3. Otherwise **`ready`**.

Then `ingestion_service` adds one more check: if the SKU is **already an approved product**, identical
values stay `ready` (approval will skip it) and different values go to `needs_review` with the
differences listed (`unit_price 850.0000 -> 900.00`).

Our row: no rule triggered → **`ready`**.

### 4.5 Stage – `ingestion_service.py`

The row is stored in `staging_records` with raw values, normalized values, notes, cell coordinates,
JEV decisions, status, reasons. A row is identified by *(file SHA-256, sheet, Excel row)*:

- **same file again** → the existing row is updated (`updated_rows`), never duplicated;
- a row a **reviewer already resolved** is left untouched by a re-import;
- a **different file** (even with the same name) is a different set of rows.

### 4.6 Approve – `approval_service.py`

Selects `ready` rows that have no product yet and inserts them into `products`, linked via
`staging_record_id`. A SKU that already exists is skipped. Running it twice approves nothing new.

### 4.7 Review – `review_service.py`

Only `needs_review` rows can be resolved, always with a note.

- **reject** → status `rejected`, reason `Rejected by reviewer: <note>`.
- **approve** → optional corrections (`sku`, `name`, `unit_price`, `currency`, `payload_kg`,
  `reach_m`) are parsed with the normalizer and the row is re-validated. If it is still not valid, or
  the SKU is already a product, the approval is refused with the reason. Otherwise the row becomes
  `ready` and is approved immediately.
- The decision (action, corrections, note, previous status and reason, time) is stored in
  `review_resolution`.

---

## 5. The database

### `staging_records` – one row per imported spreadsheet row

| Column | Content |
|---|---|
| `id` | primary key |
| `source_filename`, `source_file_hash`, `source_sheet`, `source_row_index` | where the row came from; the last three are **unique together** |
| `cell_coordinates` | `{"Part Number": "A5", …}` |
| `raw_data` | the original cell text, by header |
| `normalized_data` | `{"sku", "name", "unit_price" (string), "currency", "payload_kg", "reach_m"}` – kept for rejected rows too |
| `normalization_notes` | list of conversions applied |
| `jev_decisions` | see below |
| `status` | `ready` / `needs_review` / `rejected` |
| `status_reason` | all reasons joined with ` \| ` |
| `validation_results` | the same reasons as a list |
| `review_resolution` | the reviewer's decision, if any |
| `created_at`, `updated_at` | timestamps (UTC) |

`jev_decisions` holds, for the whole sheet:

| Key | Content |
|---|---|
| `execution_mode` | `LIVE (TypeSafe JEV API)` or `MOCK (…)` |
| `is_live`, `model_name` | e.g. `true`, `jev-1.14.0` |
| `calls` | per call: round (`mapping`/`follow_up`), outcome, request id, duration, token usage, evidence file |
| `mappings` | header → field / `irrelevant` / `unmapped` |
| `field_decisions` | per column: choice, confidence, probabilities, check value, verdict, note |
| `field_status` | per field: status, column, candidates, reason |
| `mapping_issues` | review reasons that apply to every row |
| `blocker_message` | why the mock was used, if it was |
| `raw_responses` | JEV's complete responses |

### `products` – the approved catalog

| Column | Content |
|---|---|
| `id` | primary key |
| `sku` | **unique** |
| `name`, `unit_price` (Numeric 12,4), `currency`, `payload_kg`, `reach_m` | the product |
| `staging_record_id` | the staged row it was approved from |
| `source_filename`, `source_row_index` | provenance |
| `approved_at` | timestamp |

Tables are created by `init_db()` (on API startup, on every CLI command, in the scripts).
`python -m app.cli reset-db --yes` drops and recreates them. There are no migrations: after a schema
change, reset the database (the demo and export scripts do this themselves).

---

## 6. Configuration (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `JEV_API_KEY` | empty | TypeSafe API key. Empty → mock (labelled) |
| `JEV_API_BASE_URL` | `https://api.typesafe.ai/v1` | requests go to `{base}/systemone` |
| `JEV_MODEL` | `jev-latest` | model requested; the response says which version answered |
| `JEV_MODE` | `auto` | `auto` live with labelled-mock fallback · `live` live only · `mock` never call the network |
| `JEV_TIMEOUT_SECONDS` | `10` | per attempt |
| `JEV_MAX_RETRIES` | `2` | retries for 408/429/5xx/network errors |
| `JEV_SAVE_EVIDENCE` | `true` | save live exchanges to `output/jev_evidence/` |
| `DATABASE_URL` | `sqlite:///catalog.db` | any SQLAlchemy URL ([section 10](#10-switching-database-postgresql-and-mysql)) |
| `CONFIDENCE_THRESHOLD` | `0.80` | minimum choice confidence before a column is trusted without a check |
| `SUPPORTED_CURRENCIES` | `USD,EUR,GBP,INR,CAD,AUD,JPY,CHF,SGD,CNY` | allowed ISO codes |

A one-off override for a single command in PowerShell:
`$env:JEV_MODE="live"; python -m scripts.run_demo; Remove-Item Env:JEV_MODE`

---

## 7. Running it: scripts, CLI and API

### Setup (once)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # then put your key in JEV_API_KEY
```

Every new terminal: `cd D:\jev-assignment` and `.\venv\Scripts\Activate.ps1`. All commands run from the
project root.

### Which command when

| Goal | Command |
|---|---|
| Is everything working? (free) | `python -m pytest -v` |
| Is the JEV key/connection working? | `python -m scripts.check_jev` |
| Show the six required demo steps | `python -m scripts.run_demo` |
| Same, on another workbook | `python -m scripts.run_demo data\sample_catalog_supplier_b.xlsx` |
| Regenerate the submission outputs | `python -m scripts.export_deliverables` |
| Work step by step (import, review, approve) | `python -m app.cli …` |
| Use the REST API / Swagger | `uvicorn app.main:app --reload` |
| See what is in the database | `python -m scripts.inspect_db` |
| Recreate the sample files | `python -m scripts.generate_sample_catalog` |

`run_demo` and `export_deliverables` **reset the database**; the CLI and the API keep data between runs.

### `run_demo` – what each step shows

1. The workbook exactly as read: every column, every row, Excel row numbers.
2. JEV: model, mode, each call with request id and evidence file; per column the choice, confidence,
   top probabilities, yes/no check, verdict and final mapping; then the per-field resolution table.
3. Staged rows: SKU, name, price + currency, payload, reach, conversions, status.
4. Every `needs_review` / `rejected` row with its reason.
5. Approval result and the products table with provenance.
6. Re-import + second approval, then five checks: no new staging rows, staging count unchanged,
   no new products, product count unchanged, same statuses. Exit code 1 if any fails.

### CLI reference – `python -m app.cli <command>`

| Command | Purpose |
|---|---|
| `ingest <file> [--sheet NAME]` | import into staging; prints JEV mode, evidence files, row counts, mappings, mapping issues |
| `staging [--status ready\|needs_review\|rejected] [--file NAME]` | list staged rows with IDs and reasons |
| `show <id>` | full audit trail of one row as JSON |
| `resolve <id> --approve [--set field=value …] --note "…"` | approve a review row (corrections re-validated) |
| `resolve <id> --reject --note "…"` | reject a review row |
| `approve [--ids 1 2 …]` | approve ready rows (idempotent) |
| `products` | list approved products |
| `reset-db --yes` | drop and recreate all tables |

Typical session:

```powershell
python -m app.cli reset-db --yes
python -m app.cli ingest data\sample_catalog.xlsx                 # ready 7 | needs_review 2 | rejected 4
python -m app.cli staging --status needs_review                    # note the ID of RB-1009
python -m app.cli show <ID>
python -m app.cli resolve <ID> --approve --set currency=USD --note "Supplier confirmed USD"
python -m app.cli approve                                          # 7 new products
python -m app.cli approve                                          # 0 – idempotent
python -m app.cli ingest data\sample_catalog.xlsx                  # new 0, updated 13
python -m app.cli ingest data\sample_catalog_supplier_b.xlsx
python -m app.cli staging --file sample_catalog_supplier_b.xlsx    # RB-1003 needs_review (price changed)
python -m app.cli approve
python -m app.cli products                                         # 11 products
```

### API reference

Start: `uvicorn app.main:app --reload` → Swagger UI at http://127.0.0.1:8000/docs (ReDoc at `/redoc`).

| Method | Path | Body / parameters | Returns |
|---|---|---|---|
| GET | `/` | – | links |
| GET | `/api/v1/ingest/health` | – | JEV mode, endpoint, model, currencies, threshold |
| POST | `/api/v1/ingest/upload` | form file `file`; query `sheet` | `IngestionSummary` (201) · 400 unreadable · 503 live JEV failure |
| GET | `/api/v1/staging` | query `status`, `filename`, `limit` | list of staged rows |
| GET | `/api/v1/staging/{id}` | – | one staged row · 404 |
| POST | `/api/v1/staging/{id}/resolve` | `{"action": "approve"\|"reject", "overrides": {...}, "note": "..."}` | row + product · 404 · 409 with reason |
| POST | `/api/v1/products/approve` | query `staging_ids` (repeatable, optional) | `ApprovalSummary` |
| GET | `/api/v1/products` | query `limit` | list of products |

Swagger walkthrough: health → upload `data/sample_catalog.xlsx` → `GET /staging?status=needs_review`
→ resolve the RB-1009 id with `{"action": "approve", "overrides": {"currency": "USD"}, "note": "Supplier confirmed USD"}`
→ try approving the duplicate RB-1001 row (409) → `POST /products/approve` twice (7, then 0)
→ `GET /products` (8) → upload the same file again (`new_rows: 0`).

PowerShell equivalents (server running; `curl.exe`, not the PowerShell `curl` alias):

```powershell
curl.exe -F "file=@data/sample_catalog.xlsx" http://127.0.0.1:8000/api/v1/ingest/upload
curl.exe "http://127.0.0.1:8000/api/v1/staging?status=needs_review"
curl.exe -X POST http://127.0.0.1:8000/api/v1/staging/10/resolve -H "Content-Type: application/json" -d '{\"action\":\"approve\",\"overrides\":{\"currency\":\"USD\"},\"note\":\"Supplier confirmed USD\"}'
curl.exe -X POST http://127.0.0.1:8000/api/v1/products/approve
curl.exe http://127.0.0.1:8000/api/v1/products
```

---

## 8. Demoing with a new workbook

Any supplier file works as long as it is **`.xlsx`**, the **header is in row 1** and data starts in
row 2. Header names can be anything – that is what JEV is for.

1. Put it in `data\`, e.g. `data\their_catalog.xlsx`.
2. Full demonstration: `python -m scripts.run_demo data\their_catalog.xlsx`
3. Or interactively:
   ```powershell
   python -m app.cli reset-db --yes
   python -m app.cli ingest data\their_catalog.xlsx --sheet "Price List"   # --sheet only if needed
   python -m app.cli staging --status needs_review
   python -m app.cli show <ID>
   python -m app.cli approve
   ```
4. Or upload it in Swagger (`sheet` field for a specific worksheet).

What to point out:

- **Step 2 / the Field resolution table**: how real JEV read *their* headers, and which columns were
  double-checked.
- An **uncertain column is not a failure**: the field shows `uncertain`, rows go to `needs_review` with
  the candidates listed – the brief asks not to force uncertain mappings.
- **Unknown formats or units** go to review with the exact value quoted, never guessed.
- **Step 6**: re-importing their file changes nothing.

If the workbook has several sheets and you do not pass `--sheet`, the sheet that was active when the file
was saved is imported. A wrong sheet name is an error listing the available sheets.

---

## 9. Checking the data in the database

### Option 1 – the CLI (business view)

```powershell
python -m app.cli staging                         # all staged rows
python -m app.cli staging --status rejected
python -m app.cli show 5                          # everything about one row
python -m app.cli products
```

### Option 2 – `inspect_db` (raw view, any database)

```powershell
python -m scripts.inspect_db                      # tables, row counts, status per file
python -m scripts.inspect_db --schema             # columns, types, unique constraints
python -m scripts.inspect_db --sql "SELECT sku, name, unit_price, currency, payload_kg, reach_m FROM products"
```

Only read-only statements are accepted, so it cannot damage data.

Useful queries:

```powershell
# Every flagged row with its reason
python -m scripts.inspect_db --sql "SELECT id, source_filename, source_row_index, status, status_reason FROM staging_records WHERE status <> 'ready' ORDER BY source_filename, source_row_index"

# Where each approved product came from, down to the cell
python -m scripts.inspect_db --sql "SELECT p.sku, p.unit_price, p.currency, s.source_filename, s.source_row_index, s.cell_coordinates FROM products p JOIN staging_records s ON s.id = p.staging_record_id"

# Status counts per file
python -m scripts.inspect_db --sql "SELECT source_filename, status, COUNT(*) AS n FROM staging_records GROUP BY source_filename, status"

# Rows a reviewer decided
python -m scripts.inspect_db --sql "SELECT id, status, review_resolution FROM staging_records WHERE review_resolution IS NOT NULL"
```

Reading inside the JSON columns depends on the database (in PowerShell write `` `$ `` for `$` inside double quotes):

| Database | Example |
|---|---|
| SQLite | `SELECT source_row_index, json_extract(normalized_data, '$.sku') FROM staging_records` |
| PostgreSQL | `SELECT source_row_index, normalized_data->>'sku' FROM staging_records` |
| MySQL | `SELECT source_row_index, normalized_data->>'$.sku' FROM staging_records` |

```powershell
python -m scripts.inspect_db --sql "SELECT DISTINCT source_filename, json_extract(jev_decisions, '`$.execution_mode') AS jev FROM staging_records"
```

### Option 3 – a GUI

- SQLite: open `D:\jev-assignment\catalog.db` in **DB Browser for SQLite** (free) or a SQLite viewer
  extension in VS Code. Close it (or at least don't edit) while the app is writing.
- PostgreSQL: pgAdmin or DBeaver. MySQL: MySQL Workbench or DBeaver.

### Option 4 – the API

`GET /api/v1/staging`, `GET /api/v1/staging/{id}`, `GET /api/v1/products` in Swagger.

---

## 10. Switching database: PostgreSQL and MySQL

The code is database-independent (SQLAlchemy). Only `DATABASE_URL` in `.env` changes. The tables are
created by the app; the **database itself** must exist first.

### PostgreSQL

```sql
-- in psql or pgAdmin
CREATE DATABASE catalog_db;
CREATE USER catalog WITH PASSWORD 'change-me';
GRANT ALL PRIVILEGES ON DATABASE catalog_db TO catalog;
ALTER DATABASE catalog_db OWNER TO catalog;
```

```powershell
pip install "psycopg[binary]"
# .env
DATABASE_URL=postgresql+psycopg://catalog:change-me@localhost:5432/catalog_db
```

### MySQL (8.x; also 5.7.8+ or MariaDB 10.2.7+)

```sql
-- in the mysql client or MySQL Workbench
CREATE DATABASE catalog_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'catalog'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON catalog_db.* TO 'catalog'@'localhost';
```

```powershell
pip install pymysql cryptography
# .env
DATABASE_URL=mysql+pymysql://catalog:change-me@localhost:3306/catalog_db?charset=utf8mb4
```

- `utf8mb4` is required on the database **and** in the URL, or values containing `€ £ ₹` fail.
- `cryptography` is needed for MySQL 8's default password authentication.
- MySQL closes idle connections (`wait_timeout`); the engine uses `pool_pre_ping` so this is handled.

### Then, for either

```powershell
python -m app.cli reset-db --yes          # creates the tables in the new database
python -m scripts.inspect_db --schema     # confirm the tables and unique constraints exist
python -m scripts.run_demo                # full check against the new database
```

Notes:

- Special characters in the password must be URL-encoded (`@` → `%40`, `:` → `%3A`, `#` → `%23`).
- Existing SQLite data is not copied; re-import the workbooks into the new database.
- The tests always use a temporary SQLite file, whatever `DATABASE_URL` says.
- For production: Alembic migrations instead of `create_all`; on PostgreSQL consider `JSONB`.

---

## 11. The test suite

Run: `python -m pytest -v` (about 172 tests, ~2 seconds, **no API calls, no cost**).

### How it is isolated – `tests/conftest.py`

Before any application code is imported it sets:

| Setting | Effect |
|---|---|
| `DATABASE_URL` → a SQLite file in a new temp folder | `catalog.db` is never touched |
| `JEV_MODE=mock`, `JEV_API_KEY=""` | no network calls |
| `OUTPUT_DIR` → the temp folder | evidence files from tests never land in `output/` |

Fixtures:

- `db_session` – creates all tables, gives a session, drops the tables afterwards (each test starts empty).
- `client` – a FastAPI `TestClient` on the same temporary database (endpoints without starting a server).

### How the live API is tested without the network

`test_jev_service.py` builds a real `JevClient` with an `httpx.MockTransport`. The transport receives the
exact HTTP request our code sends and answers in **TypeSafe's documented response format**. So request
building, response parsing, retries, error handling, evidence and the decision logic are all exercised
for real – only the network is replaced. Tables of inputs are written with `@pytest.mark.parametrize`,
so a failure names the exact input.

### What each file covers

| File | ~Tests | Covers |
|---|---|---|
| `test_smoke.py` | 7 | test isolation, project paths, table creation, `/` and `/health`, `inspect_db` read-only guard |
| `test_normalizer.py` | 97 | number formats (valid and invalid), prices with symbols/ISO codes, currency resolution and review cases, payload/reach conversions, unknown/missing units, header units, NULL preservation |
| `test_validator.py` | 22 | each problem → the right status; bad prices rejected regardless of mapping confidence; unresolved columns → review, not rejection; hard failures win |
| `test_jev_service.py` | 19 | documented request format for both rounds; follow-up only when needed; every verdict and field status; evidence redaction; retry on 529; fallback on 401; never mixing live and mock; strict live mode; mock mode offline; mock handles other suppliers' headers |
| `test_ingestion.py` | 10 | full pipeline on the sample (7/2/4) and other suppliers; ambiguous columns; idempotent re-import and approval; only ready rows approved; rejected rows keep values and cell positions; conversion notes; known SKU with a new price; missing name column |
| `test_review_and_cli.py` | 17 | review approve/reject; corrections validated; duplicates refused; re-import keeps reviewer decisions; API upload → resolve → approve with 400/404/409; the CLI end to end; the Supplier B workbook row by row |

### Running parts of it

```powershell
python -m pytest tests\test_normalizer.py -v        # one file
python -m pytest -k "currency" -v                   # tests whose name contains "currency"
python -m pytest -x                                 # stop at the first failure
python -m pytest --lf                               # rerun only what failed last time
```

### Adding a test

Put a function named `test_…` in a `tests/test_*.py` file. Ask for `db_session` for a clean database or
`client` for the API. For a new number/unit format, add one line to the relevant parametrized table in
`test_normalizer.py`.

The one warning in the output (`StarletteDeprecationWarning … httpx2`) comes from the FastAPI test
client library, not from this project.

---

## 12. JEV modes, evidence and cost

| `JEV_MODE` | Key set | Live call fails |
|---|---|---|
| `auto` (default) | live | labelled mock, blocker reported in summary/demo/audit |
| `live` | live | error: CLI prints it, API returns 503, `check_jev` exits 1 |
| `mock` | never calls | – |

Without a key: `auto` uses the labelled mock, `live` is an error.

**Evidence**: every live call (successful or failed) is saved as
`output/jev_evidence/jev_call_<UTC timestamp>.json` containing the URL, headers with the key redacted,
the full request body, status code, TypeSafe request id, duration, attempts and full response.
`output/jev_call_evidence.json` indexes them. Mock answers are never written there.

**Cost**: TypeSafe's published price is $0.042 per million input tokens (output free). Measured here:
a mapping round ≈ 3,000 tokens, a follow-up ≈ 600–800. One `run_demo` ≈ $0.0003–0.0005.

---

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | `pip install -r requirements.txt` (installs `truststore`) |
| `HTTP 401` from JEV | key wrong, expired or not activated – check `JEV_API_KEY` |
| `HTTP 422` from JEV | request rejected; the message names the field – see the evidence file |
| Demo says `MOCK (fallback…)` | read the `Notice` line: it is the live failure reason |
| `no such column` / `Unknown column` | database created by an older version – `python -m app.cli reset-db --yes` |
| `Sheet 'X' not found` | use one of the listed sheet names with `--sheet` |
| `Cannot read workbook` (API 400) | not a real `.xlsx`, or no header row |
| `.\venv\Scripts\Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `uvicorn` port 8000 in use | `uvicorn app.main:app --reload --port 8001` |
| `ModuleNotFoundError: app` | run from the project root with `python -m …` |
| MySQL `Incorrect string value` | database or URL not `utf8mb4` |
| MySQL `cryptography package is required` | `pip install cryptography` |

---

## 14. Known limitations

- `1.250` (single dot, three digits) is read as 1.25, not 1250 – genuinely ambiguous.
- Units are a fixed list; anything else goes to review.
- One sheet per import; duplicate header names within a sheet are not supported.
- Thresholds (0.80 choice, 0.70/0.30 check) are reasoned defaults, not calibrated on real supplier data.
- A known SKU with new values goes to review; there is no "update the existing product" workflow.
- No migrations (`create_all`), no API authentication.
- Formula cells are read as their last saved value; never-calculated formulas read as empty.
- PDF input (optional in the brief) is not implemented.
- The mock is a keyword lookup for tests and offline use only.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **JEV** | TypeSafe AI's decision model, called through the System One API |
| **System One** | TypeSafe's endpoint `POST /v1/systemone`: a state plus typed questions in, typed answers out |
| **`choice`** | a question with named options; answer = chosen option, probabilities, confidence |
| **`noul`** | a yes/no question; answer = a number from 0 (no) to 1 (yes) |
| **Verdict** | Python's judgement of one column after both rounds |
| **Field status** | Python's resolution of one target field: mapped / uncertain / conflict / absent |
| **Staging** | the holding table where every imported row waits with a status |
| **Idempotent** | doing it again changes nothing (re-import, re-approve) |
| **Provenance** | where a value came from: file, hash, sheet, row, cell, raw text, JEV decision |
| **Evidence** | the saved, redacted live JEV request/response files |
| **Mock** | the offline stand-in for JEV, always labelled |
