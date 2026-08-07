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
        db = DB(str(self.db_path))
        db.conn.close()
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


if __name__ == "__main__":
    unittest.main()
