import unittest

from core.splitter import split_chapters_with_diagnostics


class SplitterTests(unittest.TestCase):
    def test_volume_headers_are_not_empty_chapters_or_body_text(self):
        text = """第一卷：团子初醒
第1章 丧尸俯首
这是第一章的完整正文内容，确保卷标题不会被带进正文或独立成为空章节。
第二卷：余火潮
第2章 不回原来的座位
这是第二章的完整正文内容，确保章节编号与实际正文继续保持正确对应。
"""
        chapters, _ = split_chapters_with_diagnostics(text)
        self.assertEqual([c["title"] for c in chapters], ["第1章 丧尸俯首", "第2章 不回原来的座位"])
        self.assertTrue(all("第一卷：团子初醒" not in c["content"] and
                            "第二卷：余火潮" not in c["content"] for c in chapters))

    def test_directory_headings_are_removed_when_real_text_repeats(self):
        text = """书名：测试书
作者：作者
简介：简介内容

第1章 开始
第2章 线索
第3章 有关系（修订版）
第4章 追踪
第5章 反转

第1章 开始
真正的第一章正文，包含足够多的内容用于识别和验证章节分割的正确性。

第2章 线索
真正的第二章正文，包含足够多的内容用于识别和验证章节分割的正确性。

第3章 有关系（修订版）
这是真正的第三章正文，不能被目录中的空标题覆盖，且必须保留在第三章下面。

第4章 追踪
真正的第四章正文，包含足够多的内容用于识别和验证章节分割的正确性。

第5章 反转
真正的第五章正文，包含足够多的内容用于识别和验证章节分割的正确性。
"""
        chapters, info = split_chapters_with_diagnostics(text)
        self.assertEqual(info["toc_headings_removed"], 5)
        self.assertEqual([c["title"] for c in chapters], [
            "第1章 开始", "第2章 线索", "第3章 有关系（修订版）",
            "第4章 追踪", "第5章 反转",
        ])
        self.assertIn("真正的第三章正文", chapters[2]["content"])
        self.assertTrue(all(c["content"].strip() for c in chapters))


if __name__ == "__main__":
    unittest.main()
