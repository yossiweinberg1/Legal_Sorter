# Legal Sorter operations baseline

## Day-0 setup
- Configure `production.backup_folder`, `production.audit_log_path`, and `production.quarantine_folder`.
- Enable API auth with `LEGAL_SORTER_AUTH_ENABLED=true` and provision `reader`, `operator`, and `admin` API keys.
- Run `python run.py readiness` before exposing the service.

## Day-1 operations
- Review quarantined ingestion jobs through `/api/admin/jobs`.
- Replay recoverable failures with `POST /api/admin/replay/{job_id}` after correcting the source issue.
- Review audit events through `/api/admin/audit`.
- Create verified backups with `python run.py backup`.

## Restore drill
- Restore into an isolated target path with `python run.py restore /path/to/backup.zip /path/to/target_index`.
- Confirm the restored SQLite files exist and the app can start against the restored index.
- Record the drill date and outcome in your operational change log.
