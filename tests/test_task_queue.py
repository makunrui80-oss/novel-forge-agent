import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class TaskClaimTests(unittest.TestCase):
    def test_claim_is_atomic_and_serializes_tasks_per_project(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "novel.db"
            with patch.object(db, "DB_PATH", path):
                db.init()
                pid = db.execute(
                    "INSERT INTO projects(name, created_at, updated_at) VALUES(?,?,?)",
                    ("test", db.now(), db.now()),
                )
                first = db.create_task(pid, "analyze", "first", {"batch": 1})
                second = db.create_task(pid, "analyze", "second", {"batch": 2})
                claimed = db.claim_pending_task()
                self.assertEqual(claimed["id"], first)
                self.assertIsNone(db.claim_pending_task())
                db.update_task(first, status="done")
                claimed_next = db.claim_pending_task()
                self.assertEqual(claimed_next["id"], second)


if __name__ == "__main__":
    unittest.main()
