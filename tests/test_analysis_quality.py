import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, pipeline


class AnalysisQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "test.db"
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_preflight_detects_fallback_and_short_chunks(self):
        report = pipeline.build_preflight("原文", [
            {"title": "第1节", "content": "短"},
            {"title": "第2节", "content": "内容"},
        ], 5)
        self.assertTrue(report["fallback_split"])
        self.assertEqual(report["empty_chapters"], 2)
        self.assertEqual(report["batch_count"], 1)
        self.assertGreaterEqual(len(report["warnings"]), 2)

    def test_create_project_uses_diagnostic_splitter(self):
        source = "\n\n".join(
            f"第{i}章 标题{i}\n" + (f"这是第{i}章的实际正文内容，用于验证导入时章节没有丢失。" * 2)
            for i in range(1, 4)
        )
        pid = pipeline.create_project("测试书", source, "测试书.txt")
        rows = db.q("SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (pid,))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["content"].strip() for row in rows))
        report = json.loads(db.get_asset(pid, "preflight"))
        self.assertEqual(report["chapter_count"], 3)

    def test_quality_report_counts_only_nonempty_expected_chapters(self):
        pid = db.execute(
            "INSERT INTO projects(name, meta) VALUES(?,?)", ("test", "{}"))
        for idx in (1, 2):
            db.execute("INSERT INTO chapters(project_id,idx,title,content,batch_idx) VALUES(?,?,?,?,0)",
                       (pid, idx, f"第{idx}章", "文本"))
        db.set_batch(pid, "detail", 0, "done")
        db.upsert_asset(pid, "detail_0", json.dumps({"chapters": [
            {"idx": 1, "summary": "有效摘要"}, {"idx": 2, "summary": ""}
        ]}))
        report = pipeline.quality_report(pid)
        self.assertEqual(report["covered_chapters"], 1)
        self.assertEqual(report["coverage_percent"], 50)
        self.assertEqual(report["invalid_detail_batches"], [])
        self.assertEqual(report["coverage_gap_batches"], [0])

    def test_usable_detail_with_mismatched_chapter_numbers_is_reusable(self):
        pid = db.execute("INSERT INTO projects(name, meta) VALUES(?,?)", ("test", "{}"))
        db.execute("INSERT INTO chapters(project_id,idx,title,content,batch_idx) VALUES(?,1,?,?,0)",
                   (pid, "第1章", "文本"))
        db.upsert_asset(pid, "detail_0", json.dumps({"chapters": [
            {"idx": 999, "summary": "虽然编号对不上，但分析有内容"}
        ]}))
        self.assertTrue(pipeline.detail_is_complete(pid, 0))


if __name__ == "__main__":
    unittest.main()
