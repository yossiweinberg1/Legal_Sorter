# Legal Sorter Owner Setup

This guide is the practical setup checklist for getting Legal Sorter into a demoable and sellable state.

## 1. Decide what you are selling

The easiest commercial shape for this repository is:
- Desktop app for local/private use (`python app.pyw`)
- Web app for hosted demos and internal team access (`uvicorn web_app:app`)

If you want the fastest path to market, sell the web app first and keep the desktop app as an add-on.

## 2. Create local folders

From the repo root, create these folders:

```bash
mkdir -p data/pull data/index data/pending data/backups data/quarantine logs
```

## 3. Copy the environment file

```bash
cp .env.example .env
```

Populate `.env` with the values you actually plan to use:

```env
COURTLISTENER_API_TOKEN=
LLM_API_KEY=
LEGAL_SORTER_AUTH_ENABLED=true
LEGAL_SORTER_API_KEYS=admin:change-me-admin,operator:change-me-operator,reader:change-me-reader
LEGAL_SORTER_PULL_FOLDER=/absolute/path/to/data/pull
LEGAL_SORTER_INDEX_FOLDER=/absolute/path/to/data/index
LEGAL_SORTER_PENDING_FOLDER=/absolute/path/to/data/pending
LEGAL_SORTER_BACKUP_FOLDER=/absolute/path/to/data/backups
LEGAL_SORTER_QUARANTINE_FOLDER=/absolute/path/to/data/quarantine
LEGAL_SORTER_AUDIT_LOG_PATH=/absolute/path/to/logs/audit.log
LEGAL_SORTER_SUPPORT_EMAIL=support@yourdomain.com
LEGAL_SORTER_TELEMETRY_ENABLED=false
```

`src/config.py` now auto-loads `.env`, so you do not need to manually export these variables first.

## 4. Update `config.yaml`

Keep `config.yaml` as the base config, then let `.env` override machine-specific values.

Minimum production block:

```yaml
auth:
  enabled: true
  api_keys: []

production:
  enabled: true
  support_email: ""
  backup_folder: ""
  audit_log_path: "logs/audit.log"
  quarantine_folder: "quarantine"
```

Do not hardcode real secrets into `config.yaml`.

## 5. Local Python setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py health
python run.py readiness
python -m unittest discover -s tests -v
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py health
python run.py readiness
python -m unittest discover -s tests -v
```

## 6. Launch modes

### Desktop

```bash
python app.pyw
```

### Web

```bash
uvicorn web_app:app --host 0.0.0.0 --port 8000
```

### Docker demo / hosted setup

1. Keep `.env` in the repo root
2. Make sure the `data/` and `logs/` folders exist
3. Start the web app:

```bash
docker compose up --build
```

This uses:
- `Dockerfile`
- `docker-compose.yml`
- `.env`
- `config.yaml`

## 7. Minimum launch checklist before you market it

- Turn on auth and replace every default API key
- Run `python run.py readiness`
- Run `python run.py evaluate`
- Create a test backup with `python run.py backup`
- Restore that backup into a fresh folder with `python run.py restore ...`
- Confirm the audit log is being written
- Confirm your support email is real
- Decide whether you are selling local-only AI (Ollama) or cloud AI
- Add your legal disclaimer and terms before customer access

## 8. Recommended sales/demo flow

- Use the web UI for demos
- Preload a clean demo corpus into `data/index`
- Keep one admin key and one reader key for demos
- Use Ollama for privacy-focused demos, or OpenAI-compatible hosted models for convenience
- Keep backups on every demo instance

## 9. What still matters outside the repo

This repo can now be set up and packaged much more cleanly, but to truly market it you still need:
- branding/site/landing page
- pricing
- legal terms/privacy policy
- customer support process
- a hosting choice if you are selling the web version

Use this repo as the product core; use the checklist above as the deployment baseline.
