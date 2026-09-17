"""Tests for :mod:`scripts.sanitize_bank`.

These tests pin the answer-stripping contract that the pass-first coach
depends on: learners must never see ``✅`` markers, bold correct-option
wrappers, ``**答案**`` lines, or ``**解析**`` sections in the questions we
present. If any of those leaks back into ``options[].text`` or ``stem``,
we've regressed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "sanitize_bank.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sanitize_bank", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize_bank = _load_module()


class SanitizeBankRealBankTests(unittest.TestCase):
    """Run the sanitizer against a real exam-bank file shipped with the repo."""

    def test_software_engineering_first_three_questions(self) -> None:
        target = REPO_ROOT / "exam-bank" / "07-software-engineering.md"
        self.assertTrue(target.exists(), "sample exam-bank file must exist")
        text = target.read_text(encoding="utf-8")
        blocks = sanitize_bank.split_blocks(text)
        for num in ("1", "4", "6"):
            with self.subTest(question=num):
                self.assertIn(num, blocks, f"question {num} missing from bank")
                item = sanitize_bank.parse_block(blocks[num])
                # Stem must be non-empty and free of any answer markers.
                self.assertTrue(item["stem"])
                self.assertNotIn("✅", item["stem"])
                self.assertNotIn("答案", item["stem"])
                self.assertNotIn("解析", item["stem"])
                # Exactly four options, labels A-D, no marker leakage.
                self.assertEqual(len(item["options"]), 4)
                labels = [opt["label"] for opt in item["options"]]
                self.assertEqual(labels, ["A", "B", "C", "D"])
                for opt in item["options"]:
                    self.assertNotIn("✅", opt["text"])
                    self.assertFalse(
                        opt["text"].startswith("**"),
                        f"option text leaks bold wrapper: {opt}",
                    )
                    self.assertFalse(
                        opt["text"].endswith("**"),
                        f"option text leaks bold wrapper: {opt}",
                    )
                # A correct answer must be detected exactly once.
                self.assertEqual(len(item["correct"]), 1)
                self.assertIn(item["correct"][0], set(labels))


class SanitizeBankSyntheticTests(unittest.TestCase):
    """Cover formatting variants that have burned us in real coaching sessions."""

    def test_inline_check_before_bold_wrapped_option(self) -> None:
        sample = (
            "### 1. Which is correct?\n\n"
            "A. wrong\n\n"
            "B. wrong\n\n"
            "C. wrong\n\n"
            "✅ **D. right answer**\n\n"
            "**答案**：D\n"
            "**解析**：because.\n---\n"
        )
        item = sanitize_bank.parse_block(sample)
        self.assertEqual(item["correct"], ["D"])
        self.assertEqual(len(item["options"]), 4)
        self.assertEqual(item["options"][3]["text"], "right answer")
        self.assertNotIn("答案", item["stem"])

    def test_bulleted_option_with_bold_marker(self) -> None:
        sample = (
            "### 2. RUP 顺序是：\n"
            "- A. 启动 → 构建 → 精化 → 移交\n"
            "- **B. 初始 → 精化 → 构建 → 移交**\n"
            "- C. 需求 → 设计 → 实现 → 测试\n"
            "- D. 计划 → 分析 → 部署 → 维护\n"
        )
        item = sanitize_bank.parse_block(sample)
        self.assertEqual(item["correct"], ["B"])
        self.assertEqual(
            [opt["text"] for opt in item["options"]],
            [
                "启动 → 构建 → 精化 → 移交",
                "初始 → 精化 → 构建 → 移交",
                "需求 → 设计 → 实现 → 测试",
                "计划 → 分析 → 部署 → 维护",
            ],
        )

    def test_explicit_answer_line_beats_marker(self) -> None:
        # Coach convention: an explicit **答案** always wins.
        sample = (
            "### 3. stem\n"
            "A. a\n"
            "✅ B. b\n"
            "C. c\n"
            "D. d\n"
            "**答案**: C\n"
            "**解析**: coach explanation.\n"
        )
        item = sanitize_bank.parse_block(sample)
        self.assertEqual(item["correct"], ["C"])
        self.assertEqual(item["explanation"], "coach explanation.")

    def test_full_width_colon_variants(self) -> None:
        # Real bank uses ``**答案**：`` with a full-width colon.
        sample = "### 4. stem\nA. a\nB. b\nC. c\nD. d\n**答案**：A\n**解析**：ok\n"
        item = sanitize_bank.parse_block(sample)
        self.assertEqual(item["correct"], ["A"])
        self.assertEqual(item["explanation"], "ok")


class SanitizeBankCliTests(unittest.TestCase):
    """The CLI path is what the coach actually invokes."""

    def test_cli_prints_missing_question_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "bank.md"
            sample.write_text(
                "### 1. stem\nA. a\nB. b\nC. c\nD. d\n**答案**: A\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(sample), "1", "99"],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["correct"], ["A"])
            self.assertIn("99", result.stderr)

    def test_cli_missing_file_errors(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "no-such-file.md", "1"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file not found", result.stderr)


if __name__ == "__main__":
    unittest.main()


class PastPaperParsingTests(unittest.TestCase):
    """真题（past-papers）走同一套脱敏契约，必须同样不泄答案。"""

    TRANSCRIPT = "\n".join(
        [
            "# 2013 年下半年 系统架构设计师 · 综合知识真题（75 题）",
            "",
            "某操作系统采用分页存储管理方式，进程 A 逻辑地址 1111 的变量存放在 (1) 号物理页。",
            "",
            "(1) A. 9",
            "B. 2",
            "C. 4",
            "D. 6",
            "",
            "【答案】C",
            "",
            "**考点**：§1 操作系统—存储管理",
            "",
            "【解析】本题考查操作系统存储管理方面的基础知识。",
            "",
            "数据库设计的需求分析阶段应完成包括 (5) 在内的文档。",
            "",
            "(5) A. E-R 图  B. 关系模式  C. 数据字典和数据流图  D. 任务书和设计方案",
            "",
            "【答案】C",
            "",
            "**考点**：§5 数据库—需求分析文档",
            "",
            "【解析】本题考查数据库设计方面的相关知识。",
        ]
    )

    CURATED = "\n".join(
        [
            "# 2018 年下半年 系统架构设计师 · 综合知识真题（75 题）",
            "",
            "### 5. 【题干】",
            "关系 R(A,B,C,D,E) 与 S(A,B,C,F,G)，等价 SQL 的 SELECT 列表为（5）。",
            "A. R.A,R.B,R.E,S.C,G  B. R.A,R.B,D,F,G  C. R.A,R.B,R.D,S.C,F  D. R.A,R.B,R.D,S.C,G",
            "**答案：B**  |  **考点**：§5.2 关系代数投影与 SQL 转换",
            "**解析**：投影列由题目图给出的表达式决定。",
        ]
    )

    def _write(self, text: str, name: str):
        directory = Path(tempfile.mkdtemp())
        path = directory / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_transcript_layout_yields_usable_items(self) -> None:
        path = self._write(self.TRANSCRIPT, "2013下")
        items = sanitize_bank.parse_paper(path)
        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first["range"], [1, 1])
        self.assertEqual([option["label"] for option in first["options"]], ["A", "B", "C", "D"])
        self.assertEqual(first["correct"], ["C"])
        self.assertEqual(first["tag"], "§1")
        self.assertIn("存储管理", first["explanation"])
        self.assertEqual(second["range"], [5, 5])
        self.assertEqual(len(second["options"]), 4)

    def test_curated_layout_yields_usable_items(self) -> None:
        path = self._write(self.CURATED, "2018下")
        items = sanitize_bank.parse_paper(path)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["range"], [5, 5])
        self.assertEqual(item["tag"], "§5.2")
        self.assertEqual(item["correct"], ["B"])
        self.assertEqual(len(item["options"]), 4)
        self.assertIn("投影列", item["explanation"])

    def test_answers_never_leak_into_stem_or_options(self) -> None:
        for name, text in (("2013下", self.TRANSCRIPT), ("2018下", self.CURATED)):
            path = self._write(text, name)
            for item in sanitize_bank.parse_paper(path):
                with self.subTest(year=name, item=item["id"]):
                    haystack = item["stem"] + " ".join(o["text"] for o in item["options"])
                    self.assertNotIn("答案", haystack)
                    self.assertNotIn("解析", haystack)
                    self.assertNotIn("考点", haystack)
                    self.assertNotIn("✅", haystack)

    def test_candidate_topics_map_from_curriculum(self) -> None:
        topic_tags = sanitize_bank.load_topic_tags()
        self.assertTrue(topic_tags, "curriculum.json 应提供 § 标签映射")
        self.assertIn("K10.DATABASE_MODELING", sanitize_bank.candidate_topics("§5", topic_tags))
        self.assertIn("K10.DATABASE_MODELING", sanitize_bank.candidate_topics("§5.2", topic_tags))
        self.assertIn("K01.OS_MEMORY_KERNEL", sanitize_bank.candidate_topics("§1", topic_tags))

    def test_optional_images_do_not_break_option_parsing(self) -> None:
        text = self.TRANSCRIPT.replace(
            "【答案】C", "![p1_000.png](../assets/2013下/p1_000.webp)\n\n【答案】C", 1
        )
        path = self._write(text, "2013下")
        first = sanitize_bank.parse_paper(path)[0]
        self.assertEqual(len(first["options"]), 4)

    def test_real_paper_files_parse_with_expected_coverage(self) -> None:
        """仓库内真题必须能被脱敏器读出题块，且核心考期可用题数达标。"""
        expectations = {"2013下": 30, "2016下": 40, "2017下": 40, "2024下": 70, "2025下": 70}
        for year, minimum in expectations.items():
            path = REPO_ROOT / "past-papers" / "comprehensive-by-year" / f"{year}.md"
            with self.subTest(year=year):
                items = sanitize_bank.parse_paper(path)
                usable = [i for i in items if i["options"] and i["correct"]]
                self.assertGreaterEqual(len(usable), minimum, f"{year} 可用题块过少")
                self.assertTrue(all(i["tag"] for i in usable), f"{year} 存在无标签题块")
