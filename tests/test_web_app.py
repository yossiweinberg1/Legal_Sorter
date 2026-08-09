import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.database import DB
import web_app


class WebAppAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "legal_sorter.db"
        self.db = DB(str(self.db_path))
        self.addCleanup(self.db.conn.close)
        self.cfg = {
            "auth": {
                "enabled": True,
                "api_keys": [
                    {"role": "reader", "key": "reader-key"},
                    {"role": "admin", "key": "admin-key"},
                ],
            },
            "production": {
                "audit_log_path": str(Path(self.tmp.name) / "logs" / "audit.log"),
            },
        }
        self.client = TestClient(web_app.app)

    def test_reader_auth_required(self):
        with patch.object(web_app, "_cfg", return_value=self.cfg), patch.object(web_app, "_db_path", return_value=str(self.db_path)):
            resp = self.client.get("/api/stats")
            self.assertEqual(resp.status_code, 401)

            resp = self.client.get("/api/stats", headers={"X-API-Key": "reader-key"})
            self.assertEqual(resp.status_code, 200)

    def test_admin_endpoint_requires_admin_role(self):
        with patch.object(web_app, "_cfg", return_value=self.cfg), patch.object(web_app, "_db_path", return_value=str(self.db_path)):
            resp = self.client.get("/api/admin/jobs", headers={"X-API-Key": "reader-key"})
            self.assertEqual(resp.status_code, 403)

            resp = self.client.get("/api/admin/jobs", headers={"X-API-Key": "admin-key"})
            self.assertEqual(resp.status_code, 200)

    def test_case_endpoint_includes_subsequent_history(self):
        earlier = "doc-earlier"
        later = "doc-later"
        self.db.insert_document(
            doc_id=earlier,
            source_path="/tmp/earlier.txt",
            file_type="txt",
            entities={"DATE": ["January 1, 2020"]},
            citations=["410 U.S. 113 (1973)"],
            keywords=["constitutional"],
            text="Smith v. Jones, 410 U.S. 113 (1973). Decided January 1, 2020 by the California Supreme Court.",
            source_url="demo://earlier",
            virtual_folder="Jurisdiction_CA/Constitutional",
        )
        self.db.assign_ref_no(earlier)
        self.db.set_barcode(earlier, "LS-SC-US-CON-2020-000001", confidence=1.0)
        self.db.insert_document(
            doc_id=later,
            source_path="/tmp/later.txt",
            file_type="txt",
            entities={"DATE": ["February 2, 2024"]},
            citations=["600 U.S. 21 (2024)", "410 U.S. 113 (1973)"],
            keywords=["constitutional"],
            text="Brown v. Board Follow-On, 600 U.S. 21 (2024). The court overruled 410 U.S. 113 (1973).",
            source_url="demo://later",
            virtual_folder="Jurisdiction_CA/Constitutional",
        )
        self.db.assign_ref_no(later)
        self.db.set_barcode(later, "LS-CA-CA9-CON-2024-000002", confidence=1.0)
        self.db.rebuild_citation_relationships()

        with patch.object(web_app, "_cfg", return_value=self.cfg), patch.object(web_app, "_db_path", return_value=str(self.db_path)):
            resp = self.client.get(f"/api/case/{earlier}", headers={"X-API-Key": "reader-key"})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["subsequent_history"][0]["doc_id"], later)
            self.assertEqual(body["subsequent_history"][0]["treatment"], "overruled")

            hist = self.client.get(f"/api/case/{earlier}/subsequent_history", headers={"X-API-Key": "reader-key"})
            self.assertEqual(hist.status_code, 200)
            self.assertEqual(hist.json()["results"][0]["doc_id"], later)


if __name__ == "__main__":
    unittest.main()
