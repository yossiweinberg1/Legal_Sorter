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
3. Tags it: legal citation regex + spaCy NER (parties, courts, dates,
   jurisdictions) + TF-IDF keywords compared against your existing corpus
4. Assigns a SHA256 content ID — also gives automatic de-dup: pull the
   same case twice and it's recognized instantly, second copy discarded
5. Saves ID + tags + **full extracted text** + a virtual folder label
   (e.g. `Jurisdiction_CA/2023/ContractLaw`) into a SQLite database —
   this is now the whole "archive"
6. **Deletes the original file** — but only if it knows how to get it back:
   - CourtListener pulls always carry a source URL (guaranteed repullable)
   - HTML saves get one if a canonical URL is found in the page
   - A file with **no known source** (e.g. you dragged in a personal PDF
     with no online home) is instead moved to `pending_folder`, left
     intact, so you don't lose the only copy of something irreplaceable

This means storage stays tiny — you're keeping searchable text and tags
for potentially thousands of cases, not the PDFs themselves.

## Setup

### 1. Install Python packages
```
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### 1b. VS Code quick connect
1. Open your project root (the folder containing `README.md`) in VS Code
2. Create and activate a virtual environment in that folder
3. Select that interpreter in VS Code (`Python: Select Interpreter`)
4. Install dependencies:
   - `pip install -r requirements.txt`
5. Run from VS Code terminal:
   - UI: `python app.pyw`
   - Crawler: `python run.py crawl`

### 2. Edit `config.yaml`
- `pull_folder`, `index_folder`, `pending_folder` — paths on your internal
  drive. All three stay small; `index_folder` holds just the database.
- `keep_if_no_repull_source` — leave `true` (default) unless you're fine
  losing files with no known online source. If you flip it to `false`,
  the original gets deleted no matter what, per your instruction — just
  know that's not reversible for hand-added files with no source URL.

### 3. CourtListener (free legal case API)
Sign up at https://www.courtlistener.com/sign-in/, grab your API token,
paste it into `config.yaml` under `courtlistener.api_token`.

This is the only automated case-pulling source built in, on purpose —
it's free, official, and ToS-compliant (run by the nonprofit Free Law
Project). I didn't build scrapers for Westlaw/Lexis/Google Scholar since
those prohibit automated scraping. If you have your own PACER or Westlaw
account, tell me and I can wire a fetcher against your credentials.

### 3b. Token security (required)
- Do not keep real CourtListener tokens in `config.yaml`.
- Set token(s) as environment variables instead:
  - `COURTLISTENER_API_TOKEN` for one token
  - `COURTLISTENER_API_TOKENS` for multiple comma-separated tokens
- Optional: override API base URL with `COURTLISTENER_BASE_URL`.

### 4. Phone → laptop transfer (Syncthing)
1. Install Syncthing on your laptop: https://syncthing.net/downloads/
   (ARM64 Windows build available)
2. Install "Syncthing" or "Möbius Sync" on your phone (Play Store)
3. Pair the devices (QR code scan in the app)
4. Share a folder from your phone and sync it into your `pull_folder`
   path from `config.yaml`
5. Anything saved to that folder on your phone shows up on the laptop and
   gets processed automatically — device-to-device, no cloud in the middle

## Running it

Start the watcher:
```
python run.py
```

Pull cases from CourtListener straight into `pull_folder`:
```
python run.py pull "qualified immunity ninth circuit"
```

Full-text search everything you've indexed so far (works even though the
original files are gone — it's searching the extracted text):
```
python run.py search "breach of contract damages"
```

## Repulling a case later
- If `source_url` is a CourtListener link, just open it, or extend
  `legal_fetch.py` to re-download by URL (small addition — ask if you
  want it wired into `run.py` as a `repull <id>` command).
- If it's a general web URL, open it directly or re-save it into
  `pull_folder`.
- If a document is sitting in `pending_folder`, it was never deleted —
  it's just waiting because no repull source could be found.

## Notes on the tagging ("learning without hallucinating")
No LLM in the tagging path — regex + spaCy NER + TF-IDF, all of which
only surface text literally present in the document or statistically
derived from your own corpus. As your archive grows past ~15 documents,
TF-IDF keyword tags get more specific because they're computed *relative
to your other cases* — that's the "learns from its own data" behavior
without any generative risk.

## Student-facing assistant mode (RAG-first)
- The app now prioritizes retrieval-grounded study output from your local
  `documents` table.
- Output format is source-backed by design: answer/brief/IRAC/flashcards +
  quoted evidence + case IDs/ref numbers/source URLs.
- This is intended as a study aid workflow for law students and keeps source
  traceability visible.

## Hardware expectations for training/generation
- Current tiny local model path:
  - Minimum: 4–8 core CPU, 16 GB RAM, SSD
  - Better: NVIDIA GPU 8–16 GB VRAM, 32 GB RAM
- For a production-grade legal assistant:
  - Prefer strong hosted model + retrieval, or QLoRA/LoRA tuning
  - Practical fine-tune floor: 24 GB VRAM, 32–64 GB RAM, fast NVMe
  - Higher quality at scale: cloud A100/H100 class GPUs

Want document clustering next (auto-grouping similar cases beyond the
current tag-based folders)? That's a natural addition once you have more
volume — still fully local, no API calls, no risk of hallucinated labels.
