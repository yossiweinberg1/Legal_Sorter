"""Standalone bulk-ingest crawler -- launched directly by app.py when
'S3 Bulk Dump' mode is selected (mirrors how run.py crawl works for API mode).

Design: pulls opinion text from CourtListener's free S3 bulk dump, drops it
into pull_folder with the same .meta.json sidecar format the API crawler
uses, then calls the SAME watcher.scan_once() the API crawler uses --
meaning every bulk case gets identical extraction, ruling analysis,
tagging, cross-referencing, reference numbering, dedup, and brief writing.
Nothing is reimplemented; bulk data is just a faster front door into the
same pipeline. The API crawler is then only needed for genuinely new
cases filed after this bulk snapshot was taken.
"""
import requests
import bz2
import sys
import csv
import re
import warnings
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Increase the CSV field size limit to 25 Megabytes to handle massive multi-page opinions
csv.field_size_limit(25000000)
import json
import time
import logging
from pathlib import Path
import xml.etree.ElementTree as ET

from src import config as cfgmod
from src.database import DB
from src import watcher as watchermod

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("legal_sorter.bulk")

BUCKET_URL = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/?prefix=bulk-data/"
NS = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}


def _html_to_text(html: str) -> str:
    """Strip HTML tags and return clean plain text, preserving paragraph breaks."""
    try:
        # Some CourtListener payloads are XML fragments; suppress the
        # XMLParsedAsHTMLWarning that bs4 emits when the lxml HTML parser
        # encounters them, without affecting warnings elsewhere in the process.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())
    except Exception:
        # Absolute last resort: crude tag-strip via regex
        return re.sub(r"<[^>]+>", " ", html).strip()


def get_latest_opinions_bulk_url() -> str:
    """Finds the newest bulk file that is SPECIFICALLY the opinions
    dataset -- not just the newest .csv.bz2 of any kind. Grabbing any
    dataset (courts, people, dockets, etc.) that happened to be most
    recently updated was the root cause of 'bad data' before.
    """
    response = requests.get(BUCKET_URL, timeout=30)
    root = ET.fromstring(response.content)
    contents = root.findall('.//s3:Contents', NS)

    files = []
    for item in contents:
        key = item.find('s3:Key', NS).text
        last_modified = item.find('s3:LastModified', NS).text
        if 'opinions' in key.lower() and key.endswith('.csv.bz2') and 'opinion-clusters' not in key.lower():
            files.append({'key': key, 'date': last_modified})

    if not files:
        raise RuntimeError("No 'opinions' bulk dataset found in S3 listing.")

    latest = sorted(files, key=lambda x: x['date'])[-1]
    log.info(f"[BULK] Selected dataset: {latest['key']}")
    return f"https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/{latest['key']}"


def _stream_csv_rows(url: str, chunk_size: int = 1024 * 64):
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        decompressor = bz2.BZ2Decompressor()
        buffer = ""
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            try:
                buffer += decompressor.decompress(chunk).decode('utf-8', errors='ignore')
            except Exception:
                continue
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                yield line
        if buffer:
            yield buffer


def run_bulk_batch(cfg: dict, db: DB, max_items: int = 100) -> int:
    """Pulls up to max_items new (non-duplicate) opinions from the bulk
    dump and drops them into pull_folder. Returns how many were queued.
    """
    state_file = Path(cfg["index_folder"]) / "bulk_ingest_state.json"
    state = {"url": None, "rows_consumed": 0}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not state.get("url"):
        state["url"] = get_latest_opinions_bulk_url()
        state["rows_consumed"] = 0

    pull_folder = Path(cfg["pull_folder"])
    pull_folder.mkdir(parents=True, exist_ok=True)
    base_url = cfg.get("courtlistener", {}).get("base_url", "https://www.courtlistener.com/api/rest/v4")

    queued = 0
    skipped_dupe = 0
    rows_seen = 0

    try:
        # ONE persistent DictReader over the whole stream. This is the fix:
        # opinion text almost always contains embedded newlines, and a
        # single csv.reader/DictReader correctly tracks "we're still inside
        # a quoted multi-line field" across calls to next() -- but only if
        # it's the SAME reader instance for the whole file. Building a new
        # reader per physical line (the old approach) had no memory of an
        # open quote from the previous line, so any multi-line opinion text
        # got shredded into corrupted fragments.
        reader = csv.DictReader(_stream_csv_rows(state["url"]), escapechar='\\')
        for row in reader:
            rows_seen += 1
            if rows_seen <= state["rows_consumed"]:
                continue

            opinion_id = row.get("id")

            # === THE CORRUPTION FIX ===
            # Verifies the ID is present AND strictly numeric.
            # If a quote misalignment shoves an HTML paragraph here, this catches it.
            if not opinion_id or not str(opinion_id).strip().isdigit():
                # Grab a tiny preview of the broken data for your engine logs
                bad_preview = str(opinion_id)[:40].replace('\n', ' ') if opinion_id else "None"
                print(f"[WARN] Row {rows_seen} malformed. Skipping bad ID: {bad_preview}...", flush=True)

                state["rows_consumed"] = rows_seen
                continue

            # Clean up the valid numeric ID string
            opinion_id = str(opinion_id).strip()
            # ==========================

            plain = (row.get("plain_text") or "").strip()
            html_fallback = (row.get("html_with_citations") or row.get("html") or "").strip()

            if plain:
                text = plain
            elif html_fallback:
                text = _html_to_text(html_fallback)
            else:
                text = ""

            if not text:
                state["rows_consumed"] = rows_seen
                continue

            # Synthetic URL for bulk data
            bulk_file = state["url"].split("/")[-1]   # opinions-2024-07-01.csv.bz2
            source_url = f"bulk://{bulk_file}#{opinion_id}"


            existing = db.safe_execute(
                "SELECT 1 FROM documents WHERE source_url=?", (source_url,)
            ).fetchone()
            if existing:
                skipped_dupe += 1
                state["rows_consumed"] = rows_seen
                continue

            dest = pull_folder / f"bulk_{opinion_id}.txt"
            dest.write_text(text, encoding="utf-8")
            sidecar = pull_folder / f"bulk_{opinion_id}.txt.meta.json"
            sidecar.write_text(json.dumps({
                "source_url": source_url,
                "case_name": row.get("case_name") or row.get("caseName"),
                "court": row.get("court") or row.get("court_id"),
            }), encoding="utf-8")

            queued += 1
            state["rows_consumed"] = rows_seen
            if queued >= max_items:
                break

    except Exception as e:
        log.error(f"[BULK] Stream interrupted: {e}")

    state_file.write_text(json.dumps(state), encoding="utf-8")
    log.info(f"[BULK] Queued {queued} new cases ({skipped_dupe} already indexed, skipped).")
    return queued


def run_forever(batch_size: int = 100, pause_seconds: int = 3):
    """Main loop for standalone execution: top up pull_folder from the
    bulk dump, then immediately run the SAME processing pipeline the API
    crawler uses (extraction/tagging/cross-referencing/ref numbers/brief/
    delete) via watcher.scan_once(). Repeats until the dump is exhausted
    or the process is stopped.
    """
    cfg = cfgmod.load_config()
    db = DB(str(Path(cfg["index_folder"]) / "legal_sorter.db"))
    log.info("[BULK] Bulk ingestion engine starting.")

    while True:
        queued = run_bulk_batch(cfg, db, max_items=batch_size)

        # Process everything just queued through the full normal pipeline
        watchermod.scan_once(cfg, db)

        if queued == 0:
            log.info("[BULK] No new cases found this pass -- dataset may be exhausted. Sleeping 5 min.")
            time.sleep(300)
        else:
            time.sleep(pause_seconds)


if __name__ == "__main__":
    run_forever()
