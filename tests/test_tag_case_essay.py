"""Tests for :mod:`scripts.tag_case_essay`.

案例/论文标签要落在真实卷面上，所以契约是：各种标题写法都能识别、
散文行不会被误判成标题、每道题都紧跟一条标签、标签表覆盖全部考期。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "tag_case_essay.py"
TAG_MAP_PATH = REPO_ROOT / "scripts" / "case_essay_tags.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("tag_case_essay", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tag_case_essay"] = module
    spec.loader.exec_module(module)
    return module


tagging = _load_module()


class HeadingParsingTests(unittest.TestCase):
    def test_all_shipped_heading_styles_are_recognised(self) -> None:
        cases = {
            "## 试题一": ("一", ""),
            "### 2.1. 试题一：质量属性": ("一", "质量属性"),
            "# 试题1": ("一", ""),
            "试题二 论软件设计模式及其应用。": ("二", "论软件设计模式及其应用"),
            "## 试题一（必答题）：软件架构设计与评估": ("一", "（必答题）：软件架构设计与评估"),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(tagging.parse_heading(line), expected)

    def test_prose_lines_are_not_headings(self) -> None:
        for line in ("试题一是必答题，每题15分。", "试题由项目组讨论确定。"):
            with self.subTest(line=line):
                self.assertIsNone(tagging.parse_heading(line))

    def test_split_questions_returns_numeral_title_and_body(self) -> None:
        text = "\n".join(["## 试题一", "", "题干一", "", "## 试题二：标题二", "", "题干二"])
        questions = tagging.split_questions(text)
        self.assertEqual([q[0] for q in questions], ["一", "二"])
        self.assertEqual(questions[1][1], "标题二")
        self.assertIn("题干一", questions[0][2])


class ApplyTests(unittest.TestCase):
    def test_tag_goes_right_below_the_heading(self) -> None:
        text = "## 试题一\n\n题干一\n"
        out = tagging.apply_tags("case", text, [{"tag": "案例 01", "label": "架构评估（ATAM）"}])
        lines = out.splitlines()
        self.assertEqual(lines[0], "## 试题一")
        self.assertEqual(lines[1], "> **题型**：案例 01 · 架构评估（ATAM）")

    def test_bare_heading_is_promoted(self) -> None:
        text = "试题二 论软件设计模式及其应用。\n\n题干二\n"
        out = tagging.apply_tags("essay", text, [{"tag": "论文 10", "label": "设计模式在架构中的应用"}])
        self.assertIn("## 试题二：论软件设计模式及其应用", out)
        self.assertIn("> **主题**：论文 10 · 设计模式在架构中的应用", out)


class ShippedTagMapTests(unittest.TestCase):
    def test_tag_map_covers_every_paper(self) -> None:
        tag_map = json.loads(TAG_MAP_PATH.read_text(encoding="utf-8"))
        self.assertEqual(tagging.validate_tag_map(tag_map), [])

    def test_every_question_carries_a_tag_line(self) -> None:
        for kind in ("case", "essay"):
            for path in tagging.iter_papers(kind):
                text = path.read_text(encoding="utf-8")
                lines = text.splitlines()
                headings = [index for index, line in enumerate(lines) if tagging.parse_heading(line)]
                tagged = [
                    index
                    for index in headings
                    if index + 1 < len(lines)
                    and re.match(r"^>\s*\*\*(?:题型|主题)\*\*[:：]", lines[index + 1])
                ]
                with self.subTest(paper=f"{kind}/{path.stem}"):
                    self.assertTrue(headings, "试卷应至少有一道题")
                    self.assertEqual(len(headings), len(tagged), "每道题都应紧跟标签")


if __name__ == "__main__":
    unittest.main()
