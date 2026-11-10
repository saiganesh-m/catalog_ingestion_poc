# Catalog Ingestion POC Using JEV

## Assignment Overview

* **Project:** Catalog Ingestion Proof of Concept (POC) using JEV
* **Duration:** 4 Hours (Time-boxed)
* **Technology Stack:** Python 3.10+, JEV API (TypeSafe AI), SQL Database (SQLite / PostgreSQL)
* **Primary Interface:** FastAPI (with Swagger `/docs` interactive UI) & CLI runner
* **Frontend:** Not required (FastAPI Swagger UI provides complete interactive testing)

---

## 1. Business Problem & Division of Responsibilities

Industrial and commercial equipment suppliers provide product catalogs in disparate spreadsheet formats. Each supplier uses different column naming schemes (`Part Number`, `Product Code`, `SKU`), varying unit representations (`mm`, `m`, `kg`, `lbs`), and formatted price strings (`$1,250.00`, `€ 450.50`).

The goal is to build an ingestion application that reads supplier Excel catalogs, normalizes their contents into a standardized product structure, validates records, stages them for review, and idempotently approves valid products into a final database table.

### Explicit Division of Labor: Python vs. JEV

The assignment requires an explicit separation between probabilistic semantic decisions and deterministic data processing:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   DIVISION OF RESPONSIBILITIES                         │
├──────────────────────────────────┬─────────────────────────────────────┤
│ JEV (Probabilistic AI Decision)  │ Python (Deterministic Authority)    │
├──────────────────────────────────┼─────────────────────────────────────┤
│ • Semantic column-to-target field│ • Reading raw Excel cell data & row │
│   mapping using typed questions  │   coordinates (openpyxl)            │
│ • Disambiguating competing cols  │ • String parsing & decimal casting  │
│   (e.g., Unit Cost vs Ext Amount)│ • Unit normalization (mm->m, lbs->kg│
│ • Evaluating whether Rated Load  │ • Hard schema & range validation    │
│   represents payload             │ • Duplicate detection & idempotency │
│ • Filtering irrelevant columns   │ • Final gatekeeping (JEV confidence │
│ • Generating calibrated          │   CANNOT override failed numeric or │
│   probability & confidence scores│   required-field validation)        │
└──────────────────────────────────┴─────────────────────────────────────┘
```

---

## 2. Input Data & Edge Case Expectations

The application ingests an Excel workbook (`.xlsx`) containing approximately 10–15 product rows. The input dataset must specifically exercise the following eight scenarios:

1. **Header Synonyms:** Varying identifiers like `Part Number`, `Product Code`, or `SKU`.
2. **Product Name Variations:** Headers like `Description`, `Product Name`, or `Item Label`.
3. **Formatted Prices:** Prices embedded with currency symbols, spaces, and commas (e.g., `"$1,250.00"`, `"€ 450,50"`).
4. **Embedded Measurement Units:** Payload or reach values containing units (e.g., `"1500 mm"`, `"15 kg"`, `"25 lbs"`).
5. **Missing Required Fields:** A row missing a required value (e.g., blank SKU or Name).
6. **Invalid Numeric Values:** Values containing invalid characters or illegal ranges (e.g., `"N/A"`, `"-45.00"`).
7. **Duplicate Product Records:** The same SKU appearing multiple times within the file.
8. **Irrelevant Columns:** Columns unrelated to the target schema (e.g., `Warranty Duration`, `Warehouse Location`).

> **Constraint:** Raw Excel cells and row coordinates must be parsed programmatically. Source rows must never be manually rewritten or hardcoded into the application.

---

## 3. Target Product Schema & Data Provenance

### Standardized Target Schema
All supplier catalogs must be normalized into the following schema:

| Field | Type | Requirement & Rules |
| :--- | :--- | :--- |
| `sku` | String | **Required**. Unique product identifier. Must not be null or blank. |
| `name` | String | **Required**. Product display name. Must not be null or blank. |
| `unit_price` | Decimal | **Required**. Decimal value $\ge 0$. Cleaned of formatting and symbols. |
| `currency` | String | **Required**. Supported ISO 4217 currency code (e.g., `USD`, `EUR`, `GBP`). |
| `payload_kg` | Decimal/Float | **Optional**. Normalized to kilograms. Null if missing in source. |
| `reach_m` | Decimal/Float | **Optional**. Normalized to metres. Null if missing in source. |

### Strict Provenance & Audit Trail
Source provenance must be preserved separately from the normalized fields. For every processed row, the system must record:
* `source_filename`: Name of the source file.
* `source_sheet`: Worksheet name.
* `source_row_index`: 1-based original Excel row number.
* `raw_data`: Complete dictionary/JSON of the original column names and cell values.
* `jev_decisions`: The mapped fields, JEV model version, selected answers, probability distributions, and confidence scores.
* `normalization_notes`: Details on any unit or currency conversions applied.
* `validation_results`: Any validation errors or reasons for review/rejection.

---

## 4. Required Workflow

### A. Read the Source Excel Workbook
* Use `openpyxl` to extract worksheet names, headers, and row data.
* Preserve 1-indexed row numbers and cell locations.
* Preserve unparsed raw string representations for audit provenance.

### B. JEV Integration & Typed Questions
* Call the JEV API (`POST /api/v1/decide` on `https://jevtypesafeai.com` or configured gateway) using typed question primitives:
  * **`choice` questions:** Evaluate column headers and representative sample values against target fields `["sku", "name", "unit_price", "currency", "payload_kg", "reach_m", "irrelevant"]`.
  * **`noul` (Boolean) questions:** For ambiguous columns, ask whether a candidate (e.g., `Unit Cost` vs `Extended Amount`) represents the individual unit price.
* Handle uncertain outcomes: If confidence is below threshold ($< 0.80$) or no candidate fits, do not force a mapping; flag for review.
* Preserve the complete model response and confidence metrics for audit inspection.

### C. Normalization & Validation (Python)
* **Price Normalization:** Parse currency symbols and strip thousand separators. Cast to `Decimal`.
* **Currency Resolution:**
  * **Rule:** Do not silently guess ambiguous currency symbols (e.g., a bare `$` could be USD, CAD, or AUD). If unambiguous context is absent, route to `needs_review`.
* **Unit Normalization:**
  * Length: Convert `mm` to metres ($1000\text{ mm} = 1\text{ m}$), inches to metres ($1\text{ in} = 0.0254\text{ m}$).
  * Mass: Convert `g` to kg ($1000\text{ g} = 1\text{ kg}$), `lbs` to kg ($1\text{ lb} \approx 0.453592\text{ kg}$).
* **Missing Value Preservation:** Missing optional values remain `NULL` / `None`; never invent default values.
* **Validation Checks:**
  * Ensure `sku` and `name` are non-empty strings.
  * Ensure `unit_price >= 0`.
  * Validate that currency belongs to the supported list.
  * Detect duplicate SKUs in the catalog.
* **Critical Authority Rule:** **A high JEV confidence score must NEVER override a failed numeric or required-field validation.**

### D. Staging Area (Tri-State Classification)
Every row is saved into the staging table with exactly one of these statuses:
* **`ready`**: Required fields are valid, numeric checks pass, and mapping confidence is sufficient ($\ge 0.80$).
* **`needs_review`**: Mapping is uncertain ($< 0.80$), competing candidate columns exist, currency symbol is ambiguous, or duplicate SKU detected.
* **`rejected`**: Fails hard validation (missing required SKU/Name, unparseable price, negative value).

> **Requirement:** Every record marked `needs_review` or `rejected` MUST include an explicit, human-readable reason in `status_reason`.

### E. Approval & Idempotent Product Ingestion
* Provide an endpoint/command to approve eligible (`ready`) staged records into the final `products` table.
* Records with status `needs_review` or `rejected` must remain excluded from `products`.
* **Idempotency Rule:** Reprocessing the same file or re-approving records must not create duplicate entries in the `products` table (deduplication on unique `sku`).

### F. Complete Demonstration
The demonstration must show:
1. Original raw Excel catalog.
2. Extracted column headers and sample rows.
3. JEV mapping decisions, probability distributions, and confidence scores.
4. Staged records with tri-state status (`ready`, `needs_review`, `rejected`) and reasons.
5. Final approved products in the database.
6. Repeated execution demonstrating duplicate protection / idempotency.

---

## 5. Database & Environment Configuration

### Database
* **Default:** SQLite (`catalog.db`) with clear configuration for PostgreSQL switching via SQLAlchemy `DATABASE_URL`.
* **Tables:**
  1. `staging_records`: Source provenance, raw JSON, normalized JSON, JEV decisions, status, status_reason.
  2. `products`: Final approved products with unique constraint on `sku`.

### API Key & Mock Policy
* `JEV_API_KEY` read securely from `.env`.
* If `JEV_API_KEY` is not provided or live API calls fail, the system must gracefully fall back to a **clearly labeled Mock JEV Service** that returns realistic typed probabilities and confidences.
* The mock must be transparently reported and never misrepresented as a live API call.

---

## 6. Deliverables Checklist

- [ ] Python source code with clear separation of concerns.
- [ ] Dependency specification (`requirements.txt`).
- [ ] Database schema and initialization script (SQLAlchemy models).
- [ ] Comprehensive `README.md` with setup, execution, and endpoint documentation.
- [ ] Sample Excel catalog (`sample_catalog.xlsx`) exhibiting all 8 edge cases.
- [ ] Sample normalized output and ingestion summary JSON.
- [ ] Evidence of a live JEV API call (credentials redacted) or labeled mock output.
- [ ] Short evaluation report showing mapped fields, normalized values, and edge case handling.
- [ ] Documentation of known limitations, PostgreSQL migration steps, and future extensions (PDF parsing).
- [ ] **AI Coding Tool Disclosure:** Explicit acknowledgment of AI assistance and readiness to explain implementation decisions.

---

## 7. Scope Boundaries

Explicitly out of scope:
* React or other frontend SPA development.
* Vector databases and RAG pipelines.
* Model training or fine-tuning.
* 3D configuration, voice agents, or SAP integration.

**Core Mandate: Prioritize a clean, robust, and verifiable end-to-end flow.**
