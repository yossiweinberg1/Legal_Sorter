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

## Setup

### 1. Install Python packages
```
pip install -r requirements.txt
```

### 1b. VS Code quick connect
1. Open your project root (the folder containing `README.md`) in VS Code
2. Create and activate a virtual environment in that folder
3. Select that interpreter in VS Code (`Python: Select Interpreter`)
4. Install dependencies: `pip install -r requirements.txt`
5. Run from VS Code terminal: `python app.pyw` (desktop UI) or
   `uvicorn web_app:app --reload` (web interface)

### 2. Edit `config.yaml`
- `pull_folder`, `index_folder`, `pending_folder` — paths on your internal
  drive. All three stay small; `index_folder` holds just the database.
- `keep_if_no_repull_source` — leave `true` (default) unless you're fine
  losing files with no known online source.

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

### Web interface (read-only, searchable website)
```
uvicorn web_app:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000 in your browser.

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

