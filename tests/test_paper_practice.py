"""Tests for :mod:`scripts.paper_practice`.

案例真题的题干与参考答案经常混排，所以这里最重要的契约是**不许泄题**：
切不开的题必须落到 read_only，盲练题的题干里不能出现答案标记。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "paper_practice.py"


sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_module():
    spec = importlib.util.spec_from_file_location("paper_practice", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["paper_practice"] = module
    spec.loader.exec_module(module)
    return module


practice = _load_module()

MARKED = "\n".join(
    [
        "### 【问题1】（9分）",
        "请说明该系统应采用什么架构风格。",
        "### 【问题1解析】",
        "参考答案：应采用管道-过滤器风格，因为处理过程可分解为独立阶段。",
    ]
)

INTERLEAVED = "\n".join(
    [
        "### 【问题1】（9分）",
        "请列举六种质量属性并解释含义。",
        "常见的质量属性有性能、可用性、可靠性、安全性和可修改性等。",
        "【解析】本题考查质量属性。",
    ]
)

ANSWER_KEY = "### 【问题1】\n参考答案：（1）安全性 （2）可修改性\n答案解析：略"


class ClassificationTests(unittest.TestCase):
    def test_marked_body_splits_into_stem_and_answer(self) -> None:
        mode, stem, answer = practice.classify(MARKED)
        self.assertEqual(mode, "blind")
        self.assertIn("请说明该系统", stem)
        self.assertNotIn("参考答案", stem)
        self.assertIn("管道-过滤器", answer)

    def test_interleaved_body_is_read_only(self) -> None:
        """问题→答案→【解析】 的版式切不干净，绝不能当盲练题。"""
        mode, stem, answer = practice.classify(INTERLEAVED)
        self.assertEqual(mode, "read_only")
        self.assertIsNone(answer)

    def test_answer_key_block_is_not_a_question(self) -> None:
        mode, stem, answer = practice.classify(ANSWER_KEY)
        self.assertEqual(mode, "answer_key")
        self.assertIsNone(stem)
        self.assertIn("安全性", answer)

    def test_partial_split_falls_back_to_read_only(self) -> None:
        body = "\n".join(
            [
                "### 【问题1】",
                "问题一题干。",
                "### 【问题1解析】",
                "问题一答案内容足够长以便通过长度检查。",
                "### 【问题2】",
                "问题二题干。",
                "问题二的答案直接跟在后面且没有标记。",
            ]
        )
        mode, _, answer = practice.classify(body)
        self.assertEqual(mode, "read_only")
        self.assertIsNone(answer)


class SelectionTests(unittest.TestCase):
    def _item(self, **overrides):
        base = {
            "id": "x",
            "subject": "case",
            "year": "2013下",
            "numeral": "一",
            "tag": "案例 01",
            "practice_mode": "blind",
            "missing_figure": False,
            "stem": "题干",
        }
        base.update(overrides)
        return base

    def test_missing_figure_case_is_still_served_by_default(self) -> None:
        """缺图不影响出题：教练用文字描述图意即可。"""
        items = [self._item(missing_figure=True)]
        chosen = practice.select(items, tag=None, year=None, numeral=None, blind_only=True, skip_missing_figures=False)
        self.assertEqual(len(chosen), 1)

    def test_skip_missing_figures_is_opt_in(self) -> None:
        items = [self._item(missing_figure=True), self._item(id="y")]
        chosen = practice.select(items, tag=None, year=None, numeral=None, blind_only=True, skip_missing_figures=True)
        self.assertEqual([i["id"] for i in chosen], ["y"])

    def test_read_only_is_excluded_from_blind_practice(self) -> None:
        items = [self._item(practice_mode="read_only")]
        chosen = practice.select(items, tag=None, year=None, numeral=None, blind_only=True, skip_missing_figures=False)
        self.assertEqual(chosen, [])


class ShippedPaperSafetyTests(unittest.TestCase):
    def test_blind_case_stems_never_contain_answer_markers(self) -> None:
        leaks = []
        for item in practice.build_case_items():
            if item["practice_mode"] != "blind":
                continue
            stem = item["stem"]
            # 「备选答案」是填空题的候选项，属于题干本身，不算泄题
            probe = stem.replace("备选答案", "")
            if re.search(r"参考答案|答案解析|本题考查|答案[:：]", probe):
                leaks.append(item["id"])
        self.assertEqual(leaks, [], f"这些盲练题的题干疑似含答案：{leaks}")

    def test_answer_keys_are_not_served_as_questions(self) -> None:
        served = {item["id"] for item in practice.practice_items(practice.build_case_items())}
        for item in practice.build_case_items():
            if item["practice_mode"] == "answer_key":
                with self.subTest(item=item["id"]):
                    self.assertNotIn(item["id"], served)

    def test_essay_items_all_have_stems_and_tags(self) -> None:
        items = practice.build_essay_items()
        self.assertGreaterEqual(len(items), 60)
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertTrue(item["stem"].strip())
                self.assertTrue(item["tag"].startswith("论文"))


if __name__ == "__main__":
    unittest.main()
