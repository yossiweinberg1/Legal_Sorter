# Legal Case Sorter (metadata-only)

Local, offline-first pipeline for pulling, tagging, and full-text
searching legal case documents — **without keeping the original files**.
Only extracted text + tags live in a small local database; the source
file is deleted once it's safely indexed. If you need the actual
document again, you repull it (from CourtListener, or the original URL).
No cloud AI, no generative tagging — every tag is extracted straight from
the document text, so nothing gets invented.

## What it does (the pipeline)
1. Watches a `pull_folder` for new PDFs/docs/browser saves
2. Extracts raw text (PyMuPDF / python-docx / BeautifulSoup) — and, for
   HTML saves, tries to recover the original page URL
3. Tags it: legal citation regex + parties/dates/jurisdiction regex +
   TF-IDF keywords compared against your existing corpus
4. Assigns a SHA256 content ID — also gives automatic de-dup: pull the
   same case twice and it's recognized instantly, second copy discarded
5. Saves ID + tags + **full extracted text** + a virtual folder label
   (e.g. `Jurisdiction_CA/2023/ContractLaw`) into a SQLite database —
   this is now the whole "archive".  An FTS5 virtual table is built on
   top so full-text search is fast regardless of archive size.
6. **Deletes the original file** — but only if it knows how to get it back:
   - CourtListener pulls always carry a source URL (guaranteed repullable)
   - HTML saves get one if a canonical URL is found in the page
   - A file with **no known source** (e.g. you dragged in a personal PDF
     with no online home) is instead moved to `pending_folder`, left
     intact, so you don't lose the only copy of something irreplaceable

This means storage stays tiny — you're keeping searchable text and tags
for potentially thousands of cases, not the PDFs themselves.

---

## Getting Started

### Recommended: guided setup wizard
For new users, start here:

```bash
python setup_wizard.py
```

The wizard:
- creates or updates the local virtual environment
- installs dependencies
- helps you choose working folders
- optionally stores your CourtListener token
- optionally configures Ollama or LM Studio
- runs a health check
- can load demo data so you can see the system working immediately

You can re-run it later for repair or reconfiguration:

```bash
python setup_wizard.py --cli
# or, in the desktop app: Settings → Setup / Repair Wizard
```

## Setup

Need the owner/operator version of setup? See `README_OWNER_SETUP.md`.

### 1. Install Python packages (manual / power-user path)
```
pip install -r requirements.txt
```

### 1a. One-command local bootstrap
```
python bootstrap_local.py
```
This creates `.venv`, installs dependencies, and runs a local health check.
If you want the friendlier guided setup flow instead, use `python setup_wizard.py`.

### 1b. VS Code quick connect
1. Open your project root (the folder containing `README.md`) in VS Code
2. Create and activate a virtual environment in that folder
3. Select that interpreter in VS Code (`Python: Select Interpreter`)
4. Install dependencies: `pip install -r requirements.txt`
5. Run from VS Code terminal: `python app.pyw` (desktop UI) or
   `uvicorn web_app:app --reload` (web interface)
6. On Windows, you can also double-click `LegalSorter.vbs` to launch the
   desktop UI without a visible console window.

### 2. Edit `config.yaml`
- `pull_folder`, `index_folder`, `pending_folder` — paths on your internal
  drive. All three stay small; `index_folder` holds just the database.
- If you used the setup wizard, these are usually written through `.env`
  overrides automatically, so you may not need to edit them by hand.
- `keep_if_no_repull_source` — leave `true` (default) unless you're fine
  losing files with no known online source.
- On Linux/macOS, replace the default Windows-style paths with local absolute paths.
- Optional production-readiness block (recommended before selling):
  ```yaml
  production:
    enabled: false
    support_email: "support@yourdomain.com"
    backup_folder: "/absolute/path/to/backups"
    audit_log_path: "logs/audit.log"
    quarantine_folder: "/absolute/path/to/quarantine"
    retention_days: 365
    quality_gate:
      citation_f1_min: 0.70
      entity_f1_min: 0.60
      min_cases: 1
      baseline_file: "tests/fixtures/gold_baseline.json"
  auth:
    enabled: true
    api_keys: []
  llm:
    fast_model: "gpt-4o-mini"
    accurate_model: "gpt-4o-mini"
    require_citations: true
    min_sources: 1
  ```
  Set API keys through environment variables for commercial deployments:
  `LEGAL_SORTER_AUTH_ENABLED=true` and
  `LEGAL_SORTER_API_KEYS="admin:secret-admin,operator:secret-ops,reader:secret-read"`.

### 3. CourtListener (free legal case API)
Sign up at https://www.courtlistener.com/sign-in/, grab your API token,
then set it as an environment variable (do **not** paste it into config.yaml):

```
# Windows
setx COURTLISTENER_API_TOKEN "your-token-here"

# Linux / Mac
export COURTLISTENER_API_TOKEN="your-token-here"
```

Multiple tokens (for higher rate limits):
```
setx COURTLISTENER_API_TOKENS "token1,token2,token3"
```

You can start from `.env.example` and load environment values from there in your shell.

### 4. LLM / AI assistant

#### Option A — Ollama (local, free, recommended)
1. Install Ollama: https://ollama.com (one-click Windows/Mac/Linux installer)
2. Pull a model: `ollama pull llama3` (or `mistral`, `phi3`, `gemma2`)
3. Edit `config.yaml`:
   ```yaml
   llm:
     base_url: "http://localhost:11434/v1"
     api_key: ""
     model: "llama3"
   ```
4. Hardware requirements:
   - 7B models (quantized): 8 GB RAM, any modern CPU
   - 13B models: 16 GB RAM
   - 70B models: 48 GB RAM or an NVIDIA GPU with 24 GB VRAM

#### Option B — LM Studio (GUI app)
1. Download from https://lmstudio.ai
2. Download a GGUF model (e.g. Mistral 7B)
3. Start the local server (it shows you the port, usually 1234)
4. Edit `config.yaml`:
   ```yaml
   llm:
     base_url: "http://localhost:1234/v1"
     api_key: "lm-studio"
     model: "local-model"
   ```

#### Option C — OpenAI / cloud
Set the `LLM_API_KEY` environment variable. Never put real keys in config.yaml:
```
setx LLM_API_KEY "sk-..."
```

---

## Running it

### Desktop UI
```
python app.pyw
```

Windows no-console launcher:
```
LegalSorter.vbs
```

### Web interface (read-only, searchable website)
```
uvicorn web_app:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000 in your browser.

Containerized demo/hosting option:
```
docker compose up --build
```

API docs are auto-generated at http://localhost:8000/docs

The web interface is **read-only** — it only exposes search, AI Q&A, and
case detail endpoints. Nothing can write to the database through it.

### Watcher / crawler
```
python run.py
```

Pull cases from CourtListener:
```
python run.py crawl
```

Health check (dependencies + config + DB readiness):
```
python run.py health
```

Strict production-readiness check (sellability baseline):
```
python run.py readiness
```

Quality benchmark + regression gate:
```
python run.py evaluate
# or custom dataset:
python run.py evaluate /absolute/path/to/gold_cases.jsonl
```

Deterministic production backup (zipped DB + config):
```
python run.py backup
```

Restore and verify a backup:
```
python run.py restore /absolute/path/to/backup.zip /absolute/path/to/target_index [/absolute/path/to/config.yaml]
```

Bulk ingest from CourtListener S3 dump:
```
python bulk_ingest.py
```

Train the local tiny LLM on your indexed cases:
```
python run_training.py
```

Verify data integrity (random spot-check against live CourtListener):
```
python auditor.py
```

Run local tests:
```
python -m unittest discover -s tests -v
```

---

## Pipeline overview

```
CourtListener API  ──┐
CourtListener S3  ───┼──► pull_folder ──► watcher.scan_once()
Hand-dropped files ──┘         │
                               │  extract text (PyMuPDF / docx / BS4)
                               │  analyze (citation regex, ruling keywords)
                               │  tag (entities, TF-IDF keywords)
                               │  dedup (SHA256 content ID)
                               │  assign ref_no (LC-000001, LC-000002, …)
                               ▼
                         SQLite database
                         ├── documents table (full text + tags + FTS5 index)
                         ├── citation_index
                         ├── cross_references
                         └── priority_queue
                               │
                     ┌─────────┴──────────┐
                     │                    │
               Desktop UI           Web interface
               (app.pyw)        (uvicorn web_app:app)
                     │                    │
                  LLM Q&A ◄──────────────┘
             (Ollama / LM Studio / OpenAI)
```

---

## Notes

### CI
GitHub Actions workflow (`.github/workflows/ci.yml`) runs:
- `python -m compileall -q .`
- `python -m unittest discover -s tests -v`
- `python run.py evaluate`

### Security / sellability baseline
- Role-based API key access (`reader`, `operator`, `admin`) for the web/API tier
- Durable audit-log events for search, case access, AI Q&A, ingest, replay, and admin reads
- Quarantine + replay workflow for failed ingestion instead of destructive deletion
- Backup manifests with checksum verification on restore
- Citation-grounded AI answers with refusal when grounding is insufficient

### Policy / operations docs
- `docs/LEGAL_DISCLAIMER.md`
- `docs/OPERATIONS.md`

### Search performance
The database now has an FTS5 virtual table (`documents_fts`) built on top
of every document's text, keywords, and virtual folder.  Search queries go
through FTS5 first (extremely fast, supports phrase search and boolean
operators) and fall back to a LIKE scan only on older SQLite builds that
lack FTS5.

### Custom tiny LLM (`src/llm/`)
The tiny transformer model trained by `run_training.py` is a ~10M parameter
demo model.  It can learn patterns from your case corpus but will not produce
polished legal answers.  Use it to understand the training pipeline — for
real Q&A, Ollama + a 7B model gives far better results.

Training now actually injects citation graph vectors into the residual stream
(was previously a no-op), uses temperature+top-k sampling during generation
(no more repetitive greedy output), and supports datasets up to 200 000 tokens.

### Hardware for training
- CPU-only (8-core, 16 GB RAM): slow but works for small corpora
- NVIDIA GPU (8 GB VRAM): recommended for any serious training run
- For fine-tuning a 7B model with LoRA/QLoRA: 24 GB VRAM minimum

### Token security
- Never put real API keys in `config.yaml` — use environment variables
- `token_manager.py` safely updates `config.yaml` only — it no longer
  rewrites source code files
- The `.gitignore` now excludes all generated artifacts, model weights,
  and runtime state files

### Data integrity
Run `python auditor.py` any time to spot-check a random locally stored
case against the live CourtListener API.

---

## Structured Barcode System

Every indexed document receives a **Smart Barcode** — a structured ID that
encodes the document's classification into a compact, human-readable string.

### Format

```
LS-{CT}-{JR}-{SM}-{YR}-{SQ}
```

| Segment | Length | Description | Examples |
|---------|--------|-------------|---------|
| `LS` | 2 | Namespace prefix | always `LS` |
| `CT` | 2 | Case type | `SC` Supreme Court · `CA` Circuit · `DC` District · `ST` State · `SB` Statute · `BR` Brief · `OT` Other |
| `JR` | 2-4 | Jurisdiction | `US` SCOTUS · `CA9` 9th Cir. · `NYS` S.D.N.Y. · `TEX` Texas · `UNK` Unknown |
| `SM` | 3 | Subject matter | `CON` Constitutional · `CRM` Criminal · `CIV` Civil rights · `CTR` Contracts · `TRT` Torts · `FAM` Family · `IMM` Immigration · `BNK` Bankruptcy · `PRP` Property · `LAB` Labor · `TAX` Tax · `OTH` Other |
| `YR` | 4 | Decision year | `2022` · `0000` if unknown |
| `SQ` | 6 | Sequence | ties back to the `LC-XXXXXX` reference number |

**Example IDs:**
```
LS-SC-US-CON-2022-000128   # U.S. Supreme Court, constitutional law, 2022
LS-CA-CA9-CIV-2019-000042  # Ninth Circuit, civil rights, 2019
LS-DC-NYS-CRM-2021-000007  # S.D.N.Y. district court, criminal, 2021
LS-ST-TEX-FAM-2020-000315  # Texas state court, family law, 2020
```

### Confidence Score

Every barcode has a `barcode_confidence` value (float `0.0` – `1.0`):

| Strategy | Confidence | When |
|----------|------------|------|
| `llm` | `0.90` | LLM successfully classified all segments |
| `rules` | `0.75` | Rule/gazetteer engine only (deterministic but limited) |
| `llm_with_fallback` → fallback | `0.65` | LLM attempted but failed; rules used |
| Manual confirm | `1.00` | Set by `DB.confirm_barcode()` |

When confidence is **≥ `confirm_threshold`** (default `0.85`, set in
`config.yaml` under `barcode.confirm_threshold`), the barcode is automatically
marked `barcode_confirmed = 1` and excluded from future re-generation passes.

### Generation Strategies

Configure `barcode.strategy` in `config.yaml`:

| Value | Behaviour |
|-------|-----------|
| `"rules"` | Pure regex/gazetteer — fast, no LLM needed |
| `"llm"` | LLM only — richer subject classification; raises on failure |
| `"llm_with_fallback"` | Try LLM first, fall back to rules (default) |

### Collision Handling

Because `SQ` is derived from the globally-unique `LC-XXXXXX` reference number,
true collisions are essentially impossible under normal operation.  When an edge
case (NULL ref\_no, manual edit, backfill) does produce a collision, a single
letter is appended to the SQ segment:

```
LS-ST-TEX-FAM-2020-000000    ← first document
LS-ST-TEX-FAM-2020-000000A   ← second (collision resolved)
LS-ST-TEX-FAM-2020-000000B   ← third
```

### Barcodes in Search Results and API

Barcodes and their confidence scores are included in **all** API outputs:

- `GET /api/search` — each result includes `barcode` and `barcode_confidence`
- `GET /api/cases` — each list item includes `barcode` and `barcode_confidence`
- `GET /api/case/{doc_id}` — full barcode metadata including `barcode_strategy` and `barcode_confirmed`
- `GET /api/barcode/{barcode}` — look up a document directly by its barcode
- `POST /api/ask` — each source citation includes `barcode` and `barcode_confidence`

The LLM also sees the barcode in its source labels during Q&A:
```
[SOURCE 1: LC-000042 / LS-CA-CA9-CIV-2019-000042]
```
This lets the model reason about court level, jurisdiction, and topic from the
ID alone without reading the full text.

### Re-generating Barcodes

To find and re-generate barcodes that are missing, failed, or below the
confidence threshold, run the CLI tool:

```bash
# Re-generate only low-confidence barcodes (threshold from config.yaml)
python regen_barcodes.py

# Use a custom threshold
python regen_barcodes.py --min-confidence 0.70

# Force re-generate everything (including manually confirmed barcodes)
python regen_barcodes.py --force

# Dry-run: show what would be processed, no changes made
python regen_barcodes.py --dry-run
```

You can also trigger re-generation via the API (admin role required):

```http
POST /api/admin/regen_barcodes?min_confidence=0.85&force=false
```

Response:
```json
{
  "candidates": 12,
  "succeeded": 11,
  "failed": 1
}
```
