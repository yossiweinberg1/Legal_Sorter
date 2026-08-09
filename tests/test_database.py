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

    def test_rebuild_citation_relationships_tracks_subsequent_history(self):
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
            text=(
                "Brown v. Board Follow-On, 600 U.S. 21 (2024). "
                "The court distinguished 410 U.S. 113 (1973) on narrower facts."
            ),
            source_url="demo://later",
            virtual_folder="Jurisdiction_CA/Constitutional",
        )
        self.db.assign_ref_no(later)
        self.db.set_barcode(later, "LS-CA-CA9-CON-2024-000002", confidence=1.0)

        stats = self.db.rebuild_citation_relationships()
        self.assertEqual(stats["relationships"], 1)

        outgoing = self.db.get_cross_references(later)
        self.assertEqual(outgoing[0]["doc_id"], earlier)

        history = self.db.get_subsequent_history(earlier)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["doc_id"], later)
        self.assertEqual(history[0]["treatment"], "distinguished")
        self.assertEqual(history[0]["year"], "2024")


if __name__ == "__main__":
    unittest.main()
