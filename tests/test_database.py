import tempfile
import unittest
from pathlib import Path

from src.database import DB


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "legal_sorter.db"
        self.db = DB(str(self.db_path))

    def tearDown(self):
        try:
            self.db.conn.close()
        except Exception:
            pass

    def test_insert_assign_ref_and_search(self):
        doc_id = "doc-123"
        self.db.insert_document(
            doc_id=doc_id,
            source_path="/tmp/example.pdf",
            file_type="pdf",
            entities={"RULING_LOGIC": "summary"},
            citations=["123 U.S. 456"],
            keywords=["contract", "liability"],
            text="This case discusses contract liability in detail.",
            source_url="https://example.com/case",
            virtual_folder="Jurisdiction_CA/ContractLaw",
        )
        ref_no = self.db.assign_ref_no(doc_id)
        self.assertTrue(ref_no.startswith("LC-"))

        results = self.db.fts_search("contract liability", limit=5)
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["id"], doc_id)

    def test_ingestion_job_lifecycle(self):
        self.db.upsert_ingestion_job(
            job_id="job-1",
            source_path="/tmp/file.pdf",
            source_url="https://example.com/file",
            state="queued",
            source_fingerprint="fingerprint-1",
        )
        self.db.upsert_ingestion_job(
            job_id="job-1",
            source_path="/tmp/file.pdf",
            source_url="https://example.com/file",
            state="processing",
            source_fingerprint="fingerprint-1",
            increment_attempt=True,
        )
        job = self.db.get_ingestion_job("job-1")
        self.assertEqual(job["state"], "processing")
        self.assertEqual(job["attempts"], 1)

        jobs = self.db.list_ingestion_jobs(limit=5)
        self.assertEqual(jobs[0]["job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
