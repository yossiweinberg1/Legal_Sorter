import json
import requests
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class CourtListenerClient:
    def __init__(self, api_token: str, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {api_token}"})

    def search_opinions(self, query: str, court: str = None, page_size: int = 20) -> list[dict]:
        import time
        params = {"q": query, "type": "o", "page_size": page_size}
        if court:
            params["court"] = court

        resp = self.session.get(f"{self.base_url}/search/", params=params)

        if resp.status_code == 429:
            log.warning(" [!] Hit CourtListener Rate Limit (HTTP 429). Cooldown activated...")
            log.warning("     Sleeping for 60 seconds to let the API window reset...")
            time.sleep(60)
            resp = self.session.get(f"{self.base_url}/search/", params=params)

        resp.raise_for_status()
        results = resp.json().get("results", [])
        log.info(f"[DEBUG] Search for '{query}' returned {len(results)} results.")
        return results

    def download_opinion_pdf(self, result: dict, dest_folder: str) -> str | None:
        """Downloads a PDF or fetches full plain text for ONE flat search
        result object. CourtListener's search API (type=o) does NOT nest an
        'opinions' list -- each result already IS the opinion/cluster record,
        with fields like id/cluster_id, download_url, absolute_url directly
        on it. Reading it as a flat record (instead of expecting a nested
        'opinions' array) is what fixes the 100%-skip bug.
        """
        Path(dest_folder).mkdir(parents=True, exist_ok=True)

        # id/cluster_id naming differs slightly between v3 and v4 -- try both
        opinion_id = result.get("id") or result.get("cluster_id")
        download_url = result.get("download_url")
        abs_url = result.get("absolute_url")
        source_url = f"https://www.courtlistener.com{abs_url}" if abs_url else download_url

        dest = None

        # 1. Try a direct PDF first, if the search result carries one
        if download_url:
            try:
                r = self.session.get(download_url)
                if r.ok and r.headers.get("content-type", "").startswith("application/pdf"):
                    dest = Path(dest_folder) / f"cl_{opinion_id}.pdf"
                    dest.write_bytes(r.content)
                else:
                    log.warning(f"[DEBUG] PDF unavailable for {opinion_id}. Status: {r.status_code}")
            except Exception as e:
                log.error(f"[DEBUG] Connection Exception during PDF pull: {e}")

        # 2. Fall back to the opinion detail endpoint for full text
        if dest is None and opinion_id:
            try:
                detail_resp = self.session.get(f"{self.base_url}/opinions/{opinion_id}/")
                if detail_resp.ok:
                    detail_data = detail_resp.json()
                    text = (
                        detail_data.get("plain_text")
                        or detail_data.get("html_with_citations")
                        or detail_data.get("html")
                        or detail_data.get("html_lawbox")
                        or detail_data.get("html_columbia")
                        or result.get("snippet")
                    )
                    if text:
                        dest = Path(dest_folder) / f"cl_{opinion_id}.txt"
                        dest.write_text(text, encoding="utf-8")
                else:
                    log.warning(f"[DEBUG] Opinion detail fetch failed ({detail_resp.status_code}) for {opinion_id}")
            except Exception as e:
                log.error(f"[DEBUG] Failed to fetch opinion detail for {opinion_id}: {e}")

        # 3. Absolute last resort: use the search snippet itself
        if dest is None:
            text = result.get("snippet")
            if text:
                dest = Path(dest_folder) / f"cl_{opinion_id or 'unknown'}.txt"
                dest.write_text(text, encoding="utf-8")

        if dest:
            sidecar = Path(str(dest) + ".meta.json")
            sidecar.write_text(json.dumps({
                "source_url": source_url,
                "case_name": result.get("caseName"),
                "court": result.get("court"),
            }), encoding="utf-8")
            return str(dest)

        return None

    def pull_into_folder(self, query: str, pull_folder: str, court: str = None,
                          max_results: int = 20, db=None):
        """Searches opinions and downloads the first new (non-duplicate) result."""
        results = self.search_opinions(query, court=court, page_size=max_results)
        if not results:
            log.warning(f"[DEBUG] No results found for query: {query}")
            return []

        saved = []
        for result in results:
            opinion_id = result.get("id") or result.get("cluster_id")
            log.info(f"Evaluating result ID: {opinion_id}  ({result.get('caseName', 'unknown case')})")

            abs_url = result.get("absolute_url")
            download_url = result.get("download_url")
            source_url = f"https://www.courtlistener.com{abs_url}" if abs_url else download_url

            if db and source_url:
                cursor = db.conn.execute("SELECT 1 FROM documents WHERE source_url = ?", (source_url,))
                if cursor.fetchone():
                    log.info(f"  [-] Skipping: {opinion_id} already indexed.")
                    continue

            path = self.download_opinion_pdf(result, pull_folder)
            if path:
                saved.append(path)
                break  # one new case per call; remove this to grab all results

        return saved