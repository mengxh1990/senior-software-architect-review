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
REGISTRY_PATH = REPO_ROOT / "scripts" / "question_registry.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sanitize_bank", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize_bank = _load_module()


def _load_registry():
    spec = importlib.util.spec_from_file_location("question_registry", REGISTRY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["question_registry"] = module
    spec.loader.exec_module(module)
    return module


question_registry = _load_registry()


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

    def test_compact_options_keep_full_width_spaces_and_literal_star(self) -> None:
        options = sanitize_bank._parse_options(
            ["(5) A. 甲　B. 乙", "C. 丙 D. 丁"]
        )
        self.assertEqual(
            [(option["label"], option["text"]) for option in options],
            [("A", "甲"), ("B", "乙"), ("C", "丙"), ("D", "丁")],
        )

        symbols = sanitize_bank._parse_options(["A. +  B. *  C. ○  D. ⊕"])
        self.assertEqual(
            [(option["label"], option["text"]) for option in symbols],
            [("A", "+"), ("B", "*"), ("C", "○"), ("D", "⊕")],
        )


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

    def test_curated_answer_match_does_not_consume_following_explanation(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "### 1. 【题干】",
                    "测试题干。",
                    "A. 甲",
                    "B. 乙",
                    "C. 丙",
                    "D. 丁",
                    "**答案：C**  |  **考点**：§4",
                    "**解析**：这是解析正文。",
                ]
            ),
            "2024上",
        )
        item = sanitize_bank.parse_paper(path)[0]
        self.assertEqual(item["tag"], "§4")
        self.assertEqual(item["tag_label"], "")
        self.assertEqual(item["explanation"], "这是解析正文。")

    def test_shipped_papers_never_put_explanation_in_tag_label(self) -> None:
        polluted = []
        for path in sorted(sanitize_bank.PAPER_DIR.glob("*.md")):
            for item in sanitize_bank.parse_paper(path):
                if str(item.get("tag_label") or "").lstrip().startswith("**解析**"):
                    polluted.append(item["id"])
        self.assertEqual([], polluted)

    def test_explanation_cleaning_drops_next_question_and_asset_paths(self) -> None:
        polluted = (
            "本题考查存储管理。\n\n"
            "![p1_001.png](../assets/2013下/p1_001.webp)\n\n"
            "【答案】C\n\n"
            "### 5. 数据库设计的需求分析阶段\n"
            "【解析】下一题的解析不该出现在这里。"
        )
        cleaned = sanitize_bank.clean_explanation(polluted)
        self.assertIsNotNone(cleaned)
        self.assertIn("存储管理", cleaned)
        for leak in ("###", "【答案】", "【解析】", "](", "下一题"):
            self.assertNotIn(leak, cleaned)

    def test_quality_gate_accepts_a_clean_question(self) -> None:
        verdict = sanitize_bank.assess_quality(
            {
                "id": "exam-bank/x.md#1",
                "stem": "瀑布模型的主要缺点是（1）。",
                "options": [
                    {"label": "A", "text": "需求变更成本高"},
                    {"label": "B", "text": "迭代成本较高"},
                    {"label": "C", "text": "阶段边界清晰"},
                    {"label": "D", "text": "交付物明确"},
                ],
                "correct": ["A"],
                "explanation": "瀑布模型按阶段顺序推进。",
            }
        )
        self.assertEqual("ready", verdict["quality_status"])
        self.assertEqual([], verdict["quality_issues"])

    def test_quality_gate_flags_missing_figure_table_and_answer_key(self) -> None:
        verdict = sanitize_bank.assess_quality(
            {
                "id": "past-papers/x.md#1",
                "stem": "下图给出了进程页表结构，逻辑地址 1111 存放在（1）号物理页；各工序费用如下表所示。",
                "options": [
                    {"label": "A", "text": "9"},
                    {"label": "B", "text": "2"},
                ],
                "correct": ["C"],
                "explanation": None,
            }
        )
        self.assertEqual("invalid", verdict["quality_status"])
        self.assertIn("missing_required_figure", verdict["quality_issues"])
        self.assertIn("missing_required_table", verdict["quality_issues"])
        self.assertIn("answer_not_in_options", verdict["quality_issues"])
        self.assertTrue(verdict["requires_figure"])

    def test_quality_gate_requires_complete_option_set_and_context(self) -> None:
        incomplete = sanitize_bank.assess_quality(
            {
                "id": "past-papers/x.md#incomplete",
                "stem": "数据流图中，选择正确符号。",
                "options": [
                    {"label": "A", "text": "+"},
                    {"label": "C", "text": "○"},
                    {"label": "D", "text": "⊕"},
                ],
                "correct": ["D"],
                "explanation": None,
            }
        )
        self.assertIn("incomplete_option_set", incomplete["quality_issues"])

        missing_context = sanitize_bank.assess_quality(
            {
                "id": "exam-bank/x.md#context",
                "stem": "(1)",
                "options": [
                    {"label": "A", "text": "a"},
                    {"label": "B", "text": "b"},
                    {"label": "C", "text": "c"},
                    {"label": "D", "text": "d"},
                ],
                "correct": ["A"],
                "explanation": None,
            }
        )
        self.assertIn("missing_required_context", missing_context["quality_issues"])
        with_context = sanitize_bank.assess_quality(
            {
                **{
                    "id": "exam-bank/x.md#context",
                    "stem": "(1)",
                    "options": [
                        {"label": "A", "text": "a"},
                        {"label": "B", "text": "b"},
                        {"label": "C", "text": "c"},
                        {"label": "D", "text": "d"},
                    ],
                    "correct": ["A"],
                    "explanation": None,
                },
                "context": "A shared passage gives the missing context.",
            }
        )
        self.assertEqual("ready", with_context["quality_status"])

    def test_quality_gate_blocks_bare_noun_cloze_but_keeps_predicate_cloze(self) -> None:
        item = {
            "id": "past-papers/x.md#bare-cloze",
            "stem": "静态测试（ ）",
            "options": [
                {"label": "A", "text": "静态测试"},
                {"label": "B", "text": "动态测试"},
                {"label": "C", "text": "黑盒测试"},
                {"label": "D", "text": "白盒测试"},
            ],
            "correct": ["A"],
            "explanation": "被测程序不上机运行。",
        }
        blocked = sanitize_bank.assess_quality(item)
        self.assertEqual("invalid", blocked["quality_status"])
        self.assertIn("incomplete_stem", blocked["quality_issues"])

        complete = {
            **item,
            "id": "past-papers/x.md#predicate-cloze",
            "stem": "性能量度包括（ ）",
            "options": [
                {"label": "A", "text": "单位时间处理的事件个数"},
                {"label": "B", "text": "系统故障率"},
                {"label": "C", "text": "系统的访问权限"},
                {"label": "D", "text": "代码行数"},
            ],
        }
        self.assertEqual("ready", sanitize_bank.assess_quality(complete)["quality_status"])

    def test_quality_gate_blocks_truncated_cmmi_prompt(self) -> None:
        verdict = sanitize_bank.assess_quality(
            {
                "id": "past-papers/x.md#17",
                "stem": "CMMI 该企业已达到（ ）",
                "options": [
                    {"label": "A", "text": "可重复级"},
                    {"label": "B", "text": "已定义级"},
                    {"label": "C", "text": "量化级"},
                    {"label": "D", "text": "优化级"},
                ],
                "correct": ["B"],
                "explanation": "企业达到已定义级。",
            }
        )
        self.assertEqual("invalid", verdict["quality_status"])
        self.assertIn("incomplete_stem", verdict["quality_issues"])

    def test_quality_gate_blocks_repository_image_links_until_renderable(self) -> None:
        verdict = sanitize_bank.assess_quality(
            {
                "id": "past-papers/x.md#figure",
                "stem": "根据下图选择正确答案。![流程图](https://example.com/flow.png)",
                "options": [
                    {"label": "A", "text": "第一项"},
                    {"label": "B", "text": "第二项"},
                ],
                "correct": ["A"],
                "explanation": None,
            }
        )
        self.assertEqual("invalid", verdict["quality_status"])
        self.assertIn("figure_not_renderable", verdict["quality_issues"])

    def test_compound_figure_terms_are_not_treated_as_missing_figures(self) -> None:
        for stem in (
            "在 UML 用例图中，参与者之间存在（ ）关系。",
            "4+1 视图中，描述并发与同步特征的是（ ）。",
            "数据流图中，可以产生 b 数据和 c 数据的符号是（ ）。",
            "在 UML 状态图中，（）表示瞬时行为。",
        ):
            self.assertFalse(sanitize_bank.references_figure(stem), stem)
        self.assertTrue(sanitize_bank.references_figure("查询过程如下图所示。"))

    def test_answer_marker_in_stem_is_never_ready(self) -> None:
        verdict = sanitize_bank.assess_quality(
            {
                "id": "past-papers/x.md#2",
                "stem": "前趋图正确的前趋关系描述 A/B/C/D（略） 答案：C | 考点：§1.3",
                "options": [
                    {"label": "A", "text": "第一组"},
                    {"label": "B", "text": "第二组"},
                ],
                "correct": ["A"],
                "explanation": None,
            }
        )
        self.assertEqual("invalid", verdict["quality_status"])
        self.assertIn("answer_marker_leak", verdict["quality_issues"])

    def test_maintainer_exclusion_always_wins(self) -> None:
        item = {
            "id": "past-papers/x.md#3",
            "stem": "页面大小 4K，逻辑地址 1B1AH 变换后的物理地址是（3）。",
            "options": [
                {"label": "A", "text": "1B1AH"},
                {"label": "B", "text": "6B1AH"},
                {"label": "C", "text": "3B1AH"},
                {"label": "D", "text": "8B1AH"},
            ],
            "correct": ["B"],
            "explanation": "页内 12 位=B1A，页号 1→物理块号 6。",
        }
        self.assertEqual(
            "ready", sanitize_bank.assess_quality(item)["quality_status"]
        )
        excluded = sanitize_bank.assess_quality(
            item, exclusions={item["id"]: "missing_required_table"}
        )
        self.assertEqual("invalid", excluded["quality_status"])
        self.assertIn("excluded:missing_required_table", excluded["quality_issues"])

    def test_shipped_corpus_separates_ready_from_incomplete_questions(self) -> None:
        items = []
        for path in sorted(sanitize_bank.PAPER_DIR.glob("*.md")):
            items.extend(sanitize_bank.parse_paper(path))
        usable = [item for item in items if item.get("options") and item.get("correct")]
        ready = [item for item in usable if item["quality_status"] == "ready"]
        blocked = [item for item in usable if item["quality_status"] != "ready"]
        self.assertGreater(len(ready), 800, "quality gate must leave a broad safe bank")
        self.assertGreater(len(blocked), 0, "known incomplete questions must be blocked")
        for item in ready:
            self.assertNotIn("✅", item["stem"])
            self.assertNotRegex(item["stem"], r"答案\s*[:：]")
            self.assertNotIn("](", item["stem"])
            self.assertFalse(sanitize_bank.is_incomplete_stem(item["stem"]))
            self.assertEqual(
                ["A", "B", "C", "D"],
                [option["label"] for option in item["options"]],
            )
            self.assertFalse(item.get("question_count", 1) > 1)
        by_id = {item["id"]: item for item in usable}
        excluded = by_id["past-papers/comprehensive-by-year/2021.md#1"]
        self.assertEqual("invalid", excluded["quality_status"])
        self.assertIn("excluded:missing_required_table", excluded["quality_issues"])

    def test_known_answer_explanation_conflicts_are_excluded(self) -> None:
        items = {}
        for path in sorted(sanitize_bank.PAPER_DIR.glob("*.md")):
            items.update({item["id"]: item for item in sanitize_bank.parse_paper(path)})

        expected = {
            "past-papers/comprehensive-by-year/2010下.md#51-51": "ambiguous_answer_key",
            "past-papers/comprehensive-by-year/2014下.md#1-2": "explanation_conflict",
            "past-papers/comprehensive-by-year/2015下.md#20-20": "answer_explanation_conflict",
            "past-papers/comprehensive-by-year/2016下.md#52-52": "ambiguous_answer_key",
            "past-papers/comprehensive-by-year/2022.md#4": "explanation_conflict",
            "past-papers/comprehensive-by-year/2026上.md#57": "ambiguous_answer_key",
        }
        for item_id, reason in expected.items():
            with self.subTest(item_id=item_id):
                self.assertIn(item_id, items)
                self.assertEqual("invalid", items[item_id]["quality_status"])
                self.assertIn(f"excluded:{reason}", items[item_id]["quality_issues"])

    def test_shipped_cmmi_question_has_complete_stem(self) -> None:
        items = {
            item["id"]: item
            for item in sanitize_bank.parse_paper(
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2021.md"
            )
        }
        item = items["past-papers/comprehensive-by-year/2021.md#17"]
        self.assertEqual("ready", item["quality_status"])
        self.assertIn("某软件企业在项目开发过程中目标明确", item["stem"])
        self.assertIn("符合企业管理体系与流程制度", item["stem"])
        self.assertEqual(["B"], item["correct"])

    def test_shipped_orphaned_transcript_stems_are_recovered_or_blocked(self) -> None:
        expected = {
            "past-papers/comprehensive-by-year/2010下.md#16-16": (
                "ready",
                "假设单个 CPU 的性能为 1",
            ),
            "past-papers/comprehensive-by-year/2010下.md#52-52": (
                "ready",
                "某公司欲开发一个语音识别系统",
            ),
            "past-papers/comprehensive-by-year/2011下.md#24-24": (
                "invalid",
                "利用需求跟踪能力链",
            ),
            "past-papers/comprehensive-by-year/2011下.md#68-68": (
                "ready",
                "M 公司的程序员在不影响本职工作的情况下",
            ),
            "past-papers/comprehensive-by-year/2013下.md#43-43": (
                "ready",
                "软件架构风格是描述某一特定应用领域",
            ),
            "past-papers/comprehensive-by-year/2016下.md#9-9": (
                "ready",
                "给定关系模式 R(A, B, C, D, E)",
            ),
            "past-papers/comprehensive-by-year/2016下.md#51-51": (
                "ready",
                "某公司拟开发一个扫地机器人",
            ),
            "past-papers/comprehensive-by-year/2017下.md#6-6": (
                "invalid",
                "前驱图(Precedence Graph)",
            ),
            "past-papers/comprehensive-by-year/2022.md#17": (
                "ready",
                "系统可靠性的常用度量指标主要有",
            ),
        }
        items = {}
        for path in sorted(sanitize_bank.PAPER_DIR.glob("*.md")):
            items.update({item["id"]: item for item in sanitize_bank.parse_paper(path)})
        for item_id, (status, marker) in expected.items():
            with self.subTest(item_id=item_id):
                self.assertEqual(status, items[item_id]["quality_status"])
                self.assertIn(marker, items[item_id]["stem"])
        self.assertIn(
            "figure_not_renderable",
            items["past-papers/comprehensive-by-year/2011下.md#24-24"]["quality_issues"],
        )
        self.assertIn(
            "figure_not_renderable",
            items["past-papers/comprehensive-by-year/2017下.md#6-6"]["quality_issues"],
        )

    def test_transcript_recovery_collects_fragment_before_question_heading(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "某系统需要先完成总体分析的",
                    "",
                    "## 第 1 题：[§4 示例]",
                    "其中条件判断并进行下一步处理，题目要求选择（1）。",
                    "",
                    "(1) A. 选项 A",
                    "B. 选项 B",
                    "C. 选项 C",
                    "D. 选项 D",
                    "",
                    "【答案】A",
                    "",
                    "**考点**：§4 示例",
                    "",
                    "【解析】测试题。",
                ]
            ),
            "2013下",
        )
        item = sanitize_bank.parse_paper(path)[0]
        self.assertEqual("ready", item["quality_status"])
        self.assertTrue(item["stem"].startswith("某系统需要先完成总体分析的"))
        self.assertIn("其中条件判断并进行下一步处理", item["stem"])

    def test_transcript_repairs_table_before_question_heading(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "上一题解析结束。",
                    "",
                    "<table><tr><td>输入数据</td></tr></table>",
                    "",
                    "## 第 2 题：[§12 示例]",
                    "根据下表数据，最优结果是（2）。",
                    "",
                    "(2) A. 1 B. 2 C. 3 D. 4",
                    "",
                    "【答案】A",
                    "",
                    "**考点**：§12 示例",
                    "",
                    "【解析】测试题。",
                ]
            ),
            "2013下",
        )
        item = sanitize_bank.parse_paper(path)[0]
        self.assertEqual("ready", item["quality_status"])
        self.assertNotIn("missing_required_table", item["quality_issues"])
        self.assertIn("| 输入数据 |", item["stem"])

    def test_transcript_blocks_image_before_question_heading(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "![流程图](../assets/example.webp)",
                    "",
                    "## 第 2 题：[§4 示例]",
                    "如下图所示，正确的是（2）。",
                    "",
                    "(2) A. a B. b C. c D. d",
                    "",
                    "【答案】A",
                    "",
                    "**考点**：§4 示例",
                    "",
                    "【解析】测试题。",
                ]
            ),
            "2013下",
        )
        item = sanitize_bank.parse_paper(path)[0]
        self.assertEqual("invalid", item["quality_status"])
        self.assertIn("figure_not_renderable", item["quality_issues"])

    def test_previous_explanation_table_does_not_block_next_question(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "前一题。",
                    "",
                    "(1) A. a B. b C. c D. d",
                    "",
                    "【答案】A",
                    "",
                    "**考点**：§4 示例",
                    "",
                    "【解析】说明如下。",
                    "",
                    "<table><tr><td>仅用于解析</td></tr></table>",
                    "",
                    "因此上一题选 A。",
                    "",
                    "## 第 2 题：[§4 示例]",
                    "独立问题是（2）。",
                    "",
                    "(2) A. a B. b C. c D. d",
                    "",
                    "【答案】A",
                    "",
                    "**考点**：§4 示例",
                    "",
                    "【解析】测试题。",
                ]
            ),
            "2013下",
        )
        items = sanitize_bank.parse_paper(path)
        self.assertEqual("ready", items[1]["quality_status"])
        self.assertNotIn("missing_required_table", items[1]["quality_issues"])

    def test_shipped_preheading_table_is_repaired_into_question(self) -> None:
        items = {
            item["id"]: item
            for path in (REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2011下.md",)
            for item in sanitize_bank.parse_paper(path)
        }
        item = items["past-papers/comprehensive-by-year/2011下.md#70-70"]
        self.assertEqual("ready", item["quality_status"])
        self.assertIn("| 子公司 \\ 材料 | 1 吨 | 2 吨 | 3 吨 | 4 吨 |", item["stem"])
        self.assertIn("| 丙 | 4 | 6 | 11 | 14 |", item["stem"])

    def test_shipped_2013_product_schedule_embeds_required_table(self) -> None:
        items = {
            item["id"]: item
            for item in sanitize_bank.parse_paper(
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2013下.md"
            )
        }
        item = items["past-papers/comprehensive-by-year/2013下.md#69-69"]
        self.assertEqual("ready", item["quality_status"])
        self.assertTrue(item["requires_table"])
        self.assertNotIn("missing_required_table", item["quality_issues"])
        self.assertEqual(["A"], item["correct"])
        self.assertIn("| 产品 | 设计（天） | 制造（天） | 检验（天） |", item["stem"])
        self.assertIn("| 丁 | 8 | 10 | 15 |", item["stem"])

    def test_html_and_markdown_table_questions_are_renderable(self) -> None:
        items = {
            item["id"]: item
            for path in (
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2015下.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2016下.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2017下.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2024下.md",
            )
            for item in sanitize_bank.parse_paper(path)
        }
        for item_id in (
            "past-papers/comprehensive-by-year/2015下.md#69-69",
            "past-papers/comprehensive-by-year/2016下.md#69-69",
            "past-papers/comprehensive-by-year/2017下.md#11-11",
            "past-papers/comprehensive-by-year/2024下.md#21",
        ):
            with self.subTest(item_id=item_id):
                item = items[item_id]
                self.assertEqual("ready", item["quality_status"])
                self.assertNotIn("<table", item["stem"])
                self.assertIn("|", item["stem"])

    def test_unrelated_preheading_table_does_not_block_question(self) -> None:
        items = {
            item["id"]: item
            for path in (
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2016下.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2017下.md",
            )
            for item in sanitize_bank.parse_paper(path)
        }
        for item_id in (
            "past-papers/comprehensive-by-year/2016下.md#70-70",
            "past-papers/comprehensive-by-year/2017下.md#5-5",
        ):
            with self.subTest(item_id=item_id):
                self.assertEqual("ready", items[item_id]["quality_status"])

    def test_shipped_testing_stems_are_repaired_from_original_question(self) -> None:
        items = {
            item["id"]: item
            for item in sanitize_bank.parse_paper(
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2021.md"
            )
        }
        for item_id in (
            "past-papers/comprehensive-by-year/2021.md#33",
            "past-papers/comprehensive-by-year/2021.md#34",
        ):
            with self.subTest(item_id=item_id):
                self.assertEqual("ready", items[item_id]["quality_status"])
                self.assertNotIn("incomplete_stem", items[item_id]["quality_issues"])
        self.assertIn("不在机器上运行", items["past-papers/comprehensive-by-year/2021.md#33"]["stem"])
        self.assertIn("功能测试也称为", items["past-papers/comprehensive-by-year/2021.md#34"]["stem"])

    def test_transcript_boundary_keeps_next_question_out_of_explanation(self) -> None:
        items = {
            item["id"]: item
            for item in sanitize_bank.parse_paper(
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2016下.md"
            )
        }
        q68 = items["past-papers/comprehensive-by-year/2016下.md#68-68"]
        q69 = items["past-papers/comprehensive-by-year/2016下.md#69-69"]
        q70 = items["past-papers/comprehensive-by-year/2016下.md#70-70"]
        q71 = items["past-papers/comprehensive-by-year/2016下.md#71-75"]

        self.assertNotIn("某公司有4百万元", q68["explanation"])
        self.assertIn("某公司有4百万元", q69["stem"])
        self.assertNotIn("The objective of (71)", q70["explanation"])
        self.assertIn("The objective of (71)", q71["stem"])

    def test_english_passage_questions_keep_shared_context_without_duplication(self) -> None:
        path = REPO_ROOT / "exam-bank" / "23-english-reading.md"
        items = sanitize_bank.parse_exam_bank(path)
        first = next(item for item in items if item["id"].endswith("#1"))
        self.assertEqual("(1)", first["stem"])
        self.assertEqual("Passage 1 — Cloud Computing & Service Models", first["context_title"])
        self.assertIn("Cloud computing", first["context"])
        self.assertEqual("ready", first["quality_status"])
        self.assertTrue(first["requires_context"])

    def test_curated_followup_strategy_question_gets_previous_context(self) -> None:
        path = REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2018下.md"
        items = {item["id"]: item for item in sanitize_bank.parse_paper(path)}
        followup = items["past-papers/comprehensive-by-year/2018下.md#59"]
        self.assertEqual("ready", followup["quality_status"])
        self.assertEqual("关联题干", followup["context_title"])
        self.assertIn("断电后 15 秒", followup["context"])
        untrusted = items["past-papers/comprehensive-by-year/2018下.md#3"]
        self.assertEqual("invalid", untrusted["quality_status"])
        self.assertIn("missing_required_context", untrusted["quality_issues"])

    def test_transcript_multi_question_group_is_filtered_until_subquestions_exist(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "## 第 1-2 题：[§4 示例]",
                    "(1) 与 (2) 分别选择正确答案。",
                    "(1) A. a B. b C. c D. d",
                    "(2) A. a B. b C. c D. d",
                    "【答案】B C",
                    "**考点**：§4 示例",
                    "【解析】两个空分别对应不同选项。",
                ]
            ),
            "2013下",
        )
        item = sanitize_bank.parse_paper(path)[0]
        self.assertEqual(2, item["question_count"])
        self.assertEqual("invalid", item["quality_status"])
        self.assertIn("multi_question_group", item["quality_issues"])

    def test_shipped_repairs_restore_options_and_clean_explanations(self) -> None:
        repaired = {
            item["id"]: item
            for path in (
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2014下.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2025上.md",
                REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2017下.md",
            )
            for item in sanitize_bank.parse_paper(path)
        }
        for item_id in (
            "past-papers/comprehensive-by-year/2014下.md#53-53",
            "past-papers/comprehensive-by-year/2025上.md#30",
            "past-papers/comprehensive-by-year/2017下.md#29-29",
            "past-papers/comprehensive-by-year/2017下.md#52-52",
        ):
            item = repaired[item_id]
            self.assertEqual("ready", item["quality_status"])
            self.assertEqual(
                ["A", "B", "C", "D"],
                [option["label"] for option in item["options"]],
            )
            self.assertNotIn("【解析】", item["stem"])
        self.assertEqual(
            "*",
            repaired["past-papers/comprehensive-by-year/2025上.md#30"]["options"][1]["text"],
        )
        explanation = repaired["past-papers/comprehensive-by-year/2025上.md#30"][
            "explanation"
        ]
        self.assertNotIn("第 31", explanation)
        self.assertNotIn("---", explanation)

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

    def test_domain_level_labels_narrow_to_the_intended_tutor_topic(self) -> None:
        topic_tags = sanitize_bank.load_topic_tags()
        self.assertEqual(
            sanitize_bank.candidate_topics(
                "§4", topic_tags, label="软件工程—UML用例关系"
            ),
            ["K03.SOFTWARE_DESIGN_UML"],
        )
        self.assertEqual(
            sanitize_bank.candidate_topics(
                "§4", topic_tags, label="软件工程—需求变更控制"
            ),
            ["K16.REQUIREMENTS_MANAGEMENT"],
        )
        self.assertEqual(
            sanitize_bank.candidate_topics(
                "§6", topic_tags, label="系统架构—SOA与ESB"
            ),
            ["K12.PATTERNS_SOA_MICROSERVICES"],
        )

    def test_shipped_domain_level_question_uses_detailed_label_mapping(self) -> None:
        items = sanitize_bank.parse_paper(
            REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2009下.md"
        )
        by_id = {item["id"]: item for item in items}
        self.assertEqual(
            by_id["past-papers/comprehensive-by-year/2009下.md#32-32"]["candidate_topics"],
            ["K03.SOFTWARE_DESIGN_UML"],
        )
        self.assertEqual(
            by_id["past-papers/comprehensive-by-year/2009下.md#38-38"]["candidate_topics"],
            ["K11.COMPONENTS_4PLUS1"],
        )

    def test_repaired_2016_mapping_keeps_file_system_and_ip_questions_distinct(self) -> None:
        items = sanitize_bank.parse_paper(
            REPO_ROOT / "past-papers" / "comprehensive-by-year" / "2016下.md"
        )
        by_id = {item["id"]: item for item in items}
        self.assertEqual(
            by_id["past-papers/comprehensive-by-year/2016下.md#7-8"]["candidate_topics"],
            ["K14.OS_SCHEDULING_FILES"],
        )
        self.assertEqual(
            by_id["past-papers/comprehensive-by-year/2016下.md#68-68"]["candidate_topics"],
            ["K17.IP_COPYRIGHT"],
        )

    def test_cross_year_ip_variants_share_a_family_for_quiz_deduplication(self) -> None:
        ids = (
            "past-papers/comprehensive-by-year/2016下.md#68-68",
            "past-papers/comprehensive-by-year/2022.md#68",
        )
        metadata = [
            question_registry.default_metadata(item_id, "K17.IP_COPYRIGHT")
            for item_id in ids
        ]
        self.assertEqual(
            {item["concept_id"] for item in metadata}, {"K17.IP_COPYRIGHT"}
        )
        self.assertEqual(
            {item["question_family_id"] for item in metadata}, {"K17.IP_COPYRIGHT"}
        )

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
