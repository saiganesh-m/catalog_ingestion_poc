# Catalog Ingestion POC using JEV

Reads supplier Excel catalogs with inconsistent headers, formats and units, maps them to one product
schema, validates every row, stages it as `ready` / `needs_review` / `rejected` with a reason, and
idempotently approves `ready` rows into a `products` table.

**JEV decides what each column means. Python decides whether the data is valid.**

- **Full guide** (architecture, every file, data model, tests, scripts, database access, troubleshooting): [docs/GUIDE.md](docs/GUIDE.md)
- Brief (authoritative): [docs/original_brief.txt](docs/original_brief.txt) ([docs/ASSIGNMENT.md](docs/ASSIGNMENT.md) is an expanded restatement)
- Evaluation report from a real run: [output/EVALUATION.md](output/EVALUATION.md)
- Live JEV request/response evidence (API key redacted): [output/jev_evidence/](output/jev_evidence/)

---

## 1. Python vs JEV

| JEV (TypeSafe AI System One, `POST /v1/systemone`) | Python (deterministic, final authority) |
|---|---|
| **Round 1 – `choice`**: which target field each column holds (`sku`, `name`, `unit_price`, `currency`, `payload_kg`, `reach_m`, or `irrelevant`), from the header plus 3 sample values | Read cells, row numbers and cell coordinates with openpyxl |
| **Round 2 – `noul` (yes/no)**, only where it matters: columns mapped to price/payload/reach, columns below 0.80 confidence, and columns competing for one field (e.g. *Unit Cost* vs *Extended Amount*, *Max Load* vs *Weight*) | Settle each field: exactly one confirmed/confident column → mapped; otherwise **uncertain / conflict / absent**, never forced |
| Probabilities and confidence for every answer, kept in the audit trail | Parse prices to `Decimal`, resolve currency, convert units, check required fields and ranges, detect duplicates |
| | Classify every row; **a JEV confidence never overrides a failed validation** |

`JEV_MODE` in `.env`: `auto` (live; on failure a clearly labelled mock with the blocker reported), `live` (live only; failures are errors), `mock` (offline). Live and mock answers are never mixed, and mock output is never saved as evidence.

## 2. Status rules

| Status | When |
|---|---|
| `ready` | Required values valid, every needed column mapped with confidence |
| `needs_review` | Uncertain/conflicting/missing column mapping, ambiguous `$`/`¥` or conflicting currency, duplicate SKU in the file, SKU already approved with different values, unreadable/unknown-unit/≤0 payload or reach |
| `rejected` | Blank SKU or name in a mapped column, unreadable price (`N/A`, `1e3`, `approx. 1250`), negative price |

Every `needs_review` / `rejected` row carries `status_reason` (readable) and `validation_results` (list).

## 3. Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then set JEV_API_KEY
```

## 4. Run

All commands run from the project root with the virtual environment active. Everything except
`pytest` calls the live JEV API (when `JEV_API_KEY` is set) and uses the local database `catalog.db`.

| Command | What it does | When to use it |
|---|---|---|
| `python -m pytest -v` | Test suite (throwaway database, mock JEV, no API calls) | After any change |
| `python -m scripts.check_jev` | One live two-round JEV call (strict live mode); prints answers, saves evidence | First thing, to confirm key + connectivity |
| `python -m scripts.generate_sample_catalog` | (Re)creates both sample workbooks in `data/` | Only if the sample files are missing |
| `python -m scripts.run_demo [file.xlsx]` | The 6 required demonstration steps on one file (resets the DB); step 6 checks idempotency, exit code 1 on failure | Showing the end-to-end flow |
| `python -m scripts.export_deliverables` | Live run of both samples (resets the DB) → `output/*.json` + `output/EVALUATION.md` with automated edge-case checks | Regenerating the submission outputs |
| `python -m scripts.inspect_db [--schema] [--sql "…"]` | Shows what is in the database (read-only) | Checking results at any point |
| `uvicorn app.main:app --reload` | REST API, Swagger UI at http://127.0.0.1:8000/docs | Interactive testing |

### Walkthrough: FastAPI / Swagger UI

1. `python -m app.cli reset-db --yes` (empty database), then `uvicorn app.main:app --reload`.
2. Open http://127.0.0.1:8000/docs. For each step below: expand the endpoint → **Try it out** → **Execute**.
3. `GET /api/v1/ingest/health` → `jev_live_calls_enabled: true`.
4. `POST /api/v1/ingest/upload` → choose `data/sample_catalog.xlsx` → response shows `ready_count 7, needs_review_count 2, rejected_count 4`, `jev_mode "LIVE (TypeSafe JEV API)"` and the evidence files.
5. `GET /api/v1/staging` with `status=needs_review` → note the `id` of SKU `RB-1009` (ambiguous `$`).
6. `POST /api/v1/staging/{record_id}/resolve` with that id and body
   `{"action": "approve", "overrides": {"currency": "USD"}, "note": "Supplier confirmed USD"}` → the response contains the new product.
   Try `{"action": "approve", "note": "x"}` on the duplicate RB-1001 row → **409** with the reason.
7. `POST /api/v1/products/approve` → `newly_approved_count 7`. Execute again → `0` (idempotent).
8. `GET /api/v1/products` → 8 products. Upload the same file again → `new_rows 0, updated_rows 13`.

The same from PowerShell (with the server running):

```powershell
curl.exe -F "file=@data/sample_catalog.xlsx" http://127.0.0.1:8000/api/v1/ingest/upload
curl.exe "http://127.0.0.1:8000/api/v1/staging?status=needs_review"
curl.exe -X POST http://127.0.0.1:8000/api/v1/staging/10/resolve -H "Content-Type: application/json" -d '{\"action\":\"approve\",\"overrides\":{\"currency\":\"USD\"},\"note\":\"Supplier confirmed USD\"}'
curl.exe -X POST http://127.0.0.1:8000/api/v1/products/approve
curl.exe http://127.0.0.1:8000/api/v1/products
```
(Replace `10` with the id from the staging list.)

### Walkthrough: CLI (`python -m app.cli …`)

| Command | Purpose |
|---|---|
| `reset-db --yes` | Drop and recreate all tables (refuses without `--yes`) |
| `ingest <file> [--sheet NAME]` | Import a workbook into staging; prints JEV mode, evidence files, counts and mappings |
| `staging [--status S] [--file NAME]` | List staged rows with their IDs and reasons |
| `show <id>` | Full audit trail of one row as JSON (cells, raw, normalized, JEV calls, review decision) |
| `resolve <id> --approve [--set field=value …] --note "…"` | Approve a `needs_review` row, optionally correcting values; it is approved into products immediately |
| `resolve <id> --reject --note "…"` | Reject a `needs_review` row |
| `approve [--ids …]` | Approve all `ready` rows not yet approved (idempotent) |
| `products` | List approved products |

```powershell
python -m app.cli reset-db --yes
python -m app.cli ingest data\sample_catalog.xlsx              # ready 7 | needs_review 2 | rejected 4
python -m app.cli staging --status needs_review                 # note the ID of RB-1009
python -m app.cli resolve <ID> --approve --set currency=USD --note "Supplier confirmed USD"
python -m app.cli approve                                       # Approved 7 new product(s)
python -m app.cli approve                                       # Approved 0 (idempotent)
python -m app.cli ingest data\sample_catalog_supplier_b.xlsx
python -m app.cli staging --file sample_catalog_supplier_b.xlsx # RB-1003 -> needs_review (price changed)
python -m app.cli approve                                       # Supplier B's 3 clean rows
python -m app.cli products                                      # 11 products
python -m app.cli show <ID>                                     # full audit trail of any row
```

Editable fields for `--set`: `sku`, `name`, `unit_price`, `currency`, `payload_kg`, `reach_m`
(e.g. `--set payload_kg="12 kg"`; a bare number is read in kg / m). Corrections are parsed and validated
exactly like ingestion, so a reviewer cannot approve an invalid value either.

The CLI, the API and the scripts share `catalog.db`; `run_demo` and `export_deliverables` reset it.

### API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/ingest/upload?sheet=` | Upload `.xlsx`; returns the ingestion summary (400 for unreadable files, 503 if `JEV_MODE=live` and JEV fails) |
| GET | `/api/v1/ingest/health` | JEV mode, endpoint, currencies, threshold |
| GET | `/api/v1/staging?status=&filename=` | Staged rows with full provenance |
| GET | `/api/v1/staging/{id}` | One staged row |
| POST | `/api/v1/staging/{id}/resolve` | `{"action": "approve"\|"reject", "overrides": {...}, "note": "..."}` |
| POST | `/api/v1/products/approve?staging_ids=` | Approve `ready` rows (idempotent) |
| GET | `/api/v1/products` | Approved products |

## 5. Data model and provenance

- `staging_records`: `source_filename`, `source_file_hash` (SHA-256), `source_sheet`, `source_row_index` (1-based), `cell_coordinates`, `raw_data`, `normalized_data`, `normalization_notes`, `jev_decisions` (mode, model, request IDs, evidence files, every answer, per-field status), `status`, `status_reason`, `validation_results`, `review_resolution`. Unique on *(file hash, sheet, row)*: re-importing a file updates its rows instead of duplicating them.
- `products`: unique `sku`, link to the staging row it came from. A `ready` row is approved at most once; a known SKU with different values goes to review instead of overwriting.

Tables are created on startup (`init_db()`); `python -m app.cli reset-db --yes` recreates them.

**Looking at the data** (works for SQLite, PostgreSQL and MySQL):

```powershell
python -m scripts.inspect_db                  # tables, row counts, status per file
python -m scripts.inspect_db --schema         # columns, types, unique constraints
python -m scripts.inspect_db --sql "SELECT sku, name, unit_price, currency FROM products"
```

Or open `catalog.db` in a SQLite GUI such as DB Browser for SQLite. More queries: [docs/GUIDE.md](docs/GUIDE.md#9-checking-the-data-in-the-database).

## 6. Sample data

- `data/sample_catalog.xlsx` – 13 rows, all 8 edge cases from the brief (result: 7 ready, 2 review, 4 rejected).
- `data/sample_catalog_supplier_b.xlsx` – different headers (`Product Code`, `Item Label`, `Price` vs `Line Total`, `Max Load` vs `Weight`, `Curr.`) and harder formats (`USD 48,500.00`, `1.250,00 €`, `CA$`, `¥`, `1,500 kg`, `0,5 t`, `48"`, `(450.00)`), a currency conflict, and RB-1003 at a new price.

## 7. Switching database (PostgreSQL or MySQL)

Only `DATABASE_URL` changes; the code is the same. Create an empty database first, install its driver, then create the tables.

| | PostgreSQL | MySQL 8 (5.7.8+ / MariaDB 10.2.7+) |
|---|---|---|
| Create the database | `CREATE DATABASE catalog_db;` | `CREATE DATABASE catalog_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;` |
| Install the driver | `pip install "psycopg[binary]"` | `pip install pymysql cryptography` |
| `.env` | `DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/catalog_db` | `DATABASE_URL=mysql+pymysql://user:password@localhost:3306/catalog_db?charset=utf8mb4` |
| Create the tables | `python -m app.cli reset-db --yes` | `python -m app.cli reset-db --yes` |
| Check | `python -m scripts.inspect_db --schema` | `python -m scripts.inspect_db --schema` |

- **MySQL needs `utf8mb4`** on both the database and the URL, otherwise `€`, `£` and `₹` in raw values fail to store.
- **Special characters in passwords** must be URL-encoded (`@` → `%40`, `#` → `%23`).
- Server databases use `pool_pre_ping`, so connections the server dropped (MySQL `wait_timeout`) are replaced automatically.
- **Tests always use their own temporary SQLite file**, whatever `DATABASE_URL` says.
- All column types are portable (JSON, Numeric, unique constraints). For production, replace `create_all` with Alembic migrations; on PostgreSQL consider `JSONB`.

## 8. Known limitations

- **Number ambiguity:** a single dot followed by 3 digits (`1.250`) is read as 1.25, not 1250.
- **Units** are a fixed list (kg, g, t, lb, oz; m, mm, cm, km, in, ft). Anything else goes to review rather than being guessed.
- **One sheet per ingestion** (choose with `--sheet` / `?sheet=`); duplicate header names within a sheet are not supported.
- **Thresholds** (0.80 choice confidence, 0.70/0.30 yes/no) are reasoned defaults, not calibrated on real supplier data.
- **SKU updates:** a known SKU with new values goes to review; there is no "update the existing product" flow.
- **No migrations** (`create_all` only), **no API authentication**.
- **Formulas:** cells are read with `data_only=True`, so a formula never calculated by Excel reads as empty.
- **PDF input** (optional in the brief) is not implemented; a text-based PDF could feed the same pipeline via `pdfplumber` table extraction.
- The **mock** is a keyword lookup used for tests/offline only; it is labelled everywhere and its answers are never presented as JEV output.

## 9. Project structure

```text
app/
  main.py, cli.py              FastAPI entry point, command-line entry point
  core/config.py               settings from .env, project paths
  db/, models/, schemas/       SQLAlchemy engine, tables, Pydantic models
  routers/                     ingest, staging (incl. resolve), products
  services/
    excel_parser.py            openpyxl reader (rows, coordinates, samples)
    jev/                       client (HTTP, TLS, retries), service (two-round decision), mock, evidence
    normalizer.py              prices, currencies, units
    validator.py               tri-state classification
    ingestion_service.py       pipeline + idempotent staging
    approval_service.py        idempotent approval
    review_service.py          resolving needs_review rows
scripts/                       check_jev, run_demo, generate_sample_catalog, export_deliverables, inspect_db
tests/                         pytest suite
data/                          sample workbooks
output/                        generated deliverables and JEV evidence
docs/                          the brief
```

## 10. AI tool disclosure

AI coding assistants were used to build this POC: **Google Antigravity** (initial scaffolding) and **Claude Code** (code review, restructuring, the JEV integration against TypeSafe's API documentation, parsing/validation fixes, idempotency, CLI, tests and this README). All design decisions were reviewed, and I can explain and modify any part of the implementation.
