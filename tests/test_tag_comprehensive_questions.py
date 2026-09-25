"""Tests for :mod:`scripts.tag_comprehensive_questions`.

Tagging writes into committed study material, so the contract pinned here is:
blocks are detected with the right question ranges, the tag map must cover
questions 1-75 without gaps, and the applied output puts the group header above
the stem and the 考点 line right after the answer.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "tag_comprehensive_questions.py"
TAG_MAP_PATH = REPO_ROOT / "scripts" / "comprehensive_topic_tags.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("tag_comprehensive_questions", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tag_comprehensive_questions"] = module
    spec.loader.exec_module(module)
    return module


tagging = _load_module()

SAMPLE = "\n".join(
    [
        "# 真题",
        "",
        "第一题题干。",
        "",
        "(1) A. 甲",
        "B. 乙",
        "",
        "【答案】A",
        "",
        "【解析】本题考查操作系统进程管理。",
        "",
        "第二题题干，接上题。",
        "",
        "(2) A. 甲",
        "B. 乙",
        "",
        "(3) A. 甲",
        "B. 乙",
        "",
        "【答案】B C",
        "",
        "【解析】本题考查数据库设计。",
    ]
)


class BlockParsingTests(unittest.TestCase):
    def test_ranges_follow_the_option_anchors(self) -> None:
        blocks = tagging.parse_blocks(SAMPLE)
        self.assertEqual([block.range for block in blocks], [[1, 1], [2, 3]])
        self.assertEqual(blocks[1].hint, "本题考查数据库设计。")

    def test_stem_is_the_paragraph_above_the_options(self) -> None:
        blocks = tagging.parse_blocks(SAMPLE)
        self.assertIn("第一题题干", blocks[0].stem)
        self.assertIn("第二题题干", blocks[1].stem)


class TagMapValidationTests(unittest.TestCase):
    def test_gap_is_reported(self) -> None:
        problems = tagging.validate_tag_map({"2020": [{"range": [1, 10], "tag": "§1"}]})
        self.assertTrue(problems)

    def test_full_coverage_passes(self) -> None:
        tag_map = {"2020": [{"range": [1, 40], "tag": "§1"}, {"range": [41, 75], "tag": "§5"}]}
        self.assertEqual(tagging.validate_tag_map(tag_map), [])

    def test_missing_tag_is_reported(self) -> None:
        problems = tagging.validate_tag_map({"2020": [{"range": [1, 75], "tag": "1"}]})
        self.assertTrue(problems)


class ApplyTagsTests(unittest.TestCase):
    def test_header_goes_above_stem_and_tag_after_answer(self) -> None:
        groups = [{"range": [1, 1], "tag": "§1", "label": "操作系统"}, {"range": [2, 3], "tag": "§5", "label": "数据库"}]
        output = tagging.apply_tags(SAMPLE, groups)
        lines = output.splitlines()
        self.assertIn("## 第 1 题：[§1 操作系统]", lines)
        self.assertLess(lines.index("## 第 1 题：[§1 操作系统]"), lines.index("第一题题干。"))
        answer_index = lines.index("【答案】A")
        self.assertEqual(lines[answer_index + 2], "**考点**：§1 操作系统")
        self.assertIn("## 第 2-3 题：[§5 数据库]", lines)

    def test_missing_option_anchor_does_not_shift_later_tags(self) -> None:
        text = "\n".join(
            [
                "第一题题干。",
                "",
                "(1) A. 甲",
                "B. 乙",
                "",
                "【答案】A",
                "",
                "第二题题干（选项见图）。",
                "",
                "![figure](figure.webp)",
                "",
                "【答案】B",
                "",
                "第三题题干。",
                "",
                "(3) A. 甲",
                "B. 乙",
                "",
                "【答案】A",
            ]
        )
        output = tagging.apply_tags(
            text,
            [
                {"range": [1, 1], "tag": "§1", "label": "计算机系统"},
                {"range": [2, 2], "tag": "§5", "label": "数据库"},
                {"range": [3, 3], "tag": "§11", "label": "知识产权"},
            ],
        )
        self.assertIn("**考点**：§1 计算机系统", output)
        self.assertIn("**考点**：§11 知识产权", output)
        self.assertNotIn("**考点**：§5 数据库", output)


class RealTagMapTests(unittest.TestCase):
    def test_shipped_tag_map_is_complete(self) -> None:
        self.assertTrue(TAG_MAP_PATH.exists(), "标签表必须随仓库提交")
        tag_map = json.loads(TAG_MAP_PATH.read_text(encoding="utf-8"))
        self.assertEqual(tagging.validate_tag_map(tag_map), [])
        self.assertEqual(tag_map, {}, "旧题组映射已退出训练")

    def test_tagged_papers_carry_headers_and_tags(self) -> None:
        for year in ("2018下", "2019下", "2022"):
            text = (REPO_ROOT / "past-papers" / "comprehensive-by-year" / f"{year}.md").read_text(encoding="utf-8")
            with self.subTest(year=year):
                self.assertIn("## 第 1", text)
                self.assertIn("**考点**：§", text)
                self.assertNotIn("待人工标注", text)


if __name__ == "__main__":
    unittest.main()
