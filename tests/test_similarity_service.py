import os
import tempfile
import unittest
from pathlib import Path

import similarity_service as ss
from src.database import DB


class SimilarityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, self.old_cwd)

        self.pull_dir = self.base / "pull"
        self.index_dir = self.base / "index"
        self.pending_dir = self.base / "pending"
        self.pull_dir.mkdir()
        self.index_dir.mkdir()
        self.pending_dir.mkdir()
        (self.base / "config.yaml").write_text(
            "\n".join(
                [
                    f'pull_folder: "{self.pull_dir}"',
                    f'index_folder: "{self.index_dir}"',
                    f'pending_folder: "{self.pending_dir}"',
                    "courtlistener:",
                    '  base_url: "https://example.com"',
                    "  api_tokens: []",
                ]
            ),
            encoding="utf-8",
        )

        self.db = DB(str(self.index_dir / "legal_sorter.db"))
        self.addCleanup(self.db.conn.close)

        self.old_index_file = ss.INDEX_FILE
        self.old_vectorizer_file = ss.VECTORIZER_FILE
        self.old_paths_file = ss.PATHS_FILE
        ss.INDEX_FILE = self.base / "similarity_matrix.joblib"
        ss.VECTORIZER_FILE = self.base / "vectorizer.joblib"
        ss.PATHS_FILE = self.base / "indexed_paths.joblib"
        self.addCleanup(self._restore_cache_paths)

    def _restore_cache_paths(self):
        ss.INDEX_FILE = self.old_index_file
        ss.VECTORIZER_FILE = self.old_vectorizer_file
        ss.PATHS_FILE = self.old_paths_file

    def _insert_case(self, doc_id: str, opinion_id: int, text: str) -> str:
        path = self.pull_dir / f"bulk_{opinion_id}.txt"
        path.write_text(text, encoding="utf-8")
        self.db.insert_document(
            doc_id=doc_id,
            source_path=str(path),
            file_type="txt",
            entities={"court": "example"},
            citations=[],
            keywords=["contract"],
            text=text,
            source_url=f"bulk://opinions-2026-06-30.csv.bz2#{opinion_id}",
            virtual_folder="Contracts",
        )
        return self.db.assign_ref_no(doc_id)

    def test_similarity_lookup_accepts_bulk_filename_and_ref_no(self):
        target_ref = self._insert_case("doc-1", 4338119, "contract liability damages indemnity")
        other_ref = self._insert_case("doc-2", 4338120, "contract liability damages and warranty dispute")

        ok, message = ss.build_and_cache_index()
        self.assertTrue(ok, message)

        by_filename = ss.get_similar_cases("bulk_4338119.txt", top_n=3)
        self.assertIsNone(by_filename["error"])
        self.assertEqual(by_filename["matches"][0]["ref_no"], other_ref)

        by_ref = ss.get_similar_cases(target_ref, top_n=3)
        self.assertIsNone(by_ref["error"])
        self.assertEqual(by_ref["matches"][0]["ref_no"], other_ref)

    def test_similarity_lookup_rebuilds_stale_cache(self):
        self._insert_case("doc-1", 4338119, "contract liability damages indemnity")
        self._insert_case("doc-2", 4338120, "contract liability damages and warranty dispute")
        ok, message = ss.build_and_cache_index()
        self.assertTrue(ok, message)

        new_ref = self._insert_case("doc-3", 4338121, "contract liability damages from indemnity and warranty issues")

        result = ss.get_similar_cases(new_ref, top_n=3)
        self.assertIsNone(result["error"])
        self.assertGreaterEqual(len(result["matches"]), 1)


if __name__ == "__main__":
    unittest.main()
