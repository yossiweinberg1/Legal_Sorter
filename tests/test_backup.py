import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from src.backup import create_backup


class BackupTests(unittest.TestCase):
    def test_create_backup_writes_zip_with_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = base / "index"
            backups = base / "backups"
            index.mkdir()
            db_path = index / "legal_sorter.db"
            db_path.write_text("sqlite-placeholder", encoding="utf-8")

            cfg = {
                "index_folder": str(index),
                "production": {"backup_folder": str(backups)},
            }
            out = create_backup(cfg)
            self.assertTrue(out.exists())
            with ZipFile(out, "r") as zf:
                names = set(zf.namelist())
            self.assertIn("index/legal_sorter.db", names)


if __name__ == "__main__":
    unittest.main()

