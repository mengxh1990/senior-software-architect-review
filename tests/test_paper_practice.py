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


class RemovedFigureNoteTests(unittest.TestCase):
    """广告/推广图被剔除的标注不再等同于"整题缺图"。"""

    def test_note_with_a_surviving_figure_is_not_missing(self) -> None:
        body = "题干\n\n*（原图含机构广告或水印，已移除）*\n\n![](../assets/2022/p3_001.webp)\n"
        _, figures, missing = practice.normalise_body(body)
        self.assertEqual(figures, ["p3_001.webp"])
        self.assertFalse(missing)

    def test_note_without_any_figure_still_counts_as_missing(self) -> None:
        body = "题干\n\n*（原图含机构广告或水印，已移除）*\n"
        _, figures, missing = practice.normalise_body(body)
        self.assertEqual(figures, [])
        self.assertTrue(missing)


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
        """缺图不影响出题：出题时说明缺失，由教练按权威原卷补充图意。"""
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

    def test_multi_batch_numeral_is_rejected_before_filtering(self) -> None:
        """多批次考期按题号取题：含混判定看过滤前的全部匹配题。

        批次1 的试题是 read_only、批次2 是 blind 时，也不能靠 blind_only
        过滤静默消歧，必须要求显式指定批次。
        """
        items = [
            self._item(id="b1", batch="1", practice_mode="read_only"),
            self._item(id="b2", batch="2"),
        ]
        with self.assertRaises(ValueError):
            practice.select(items, tag=None, year="2013下", numeral="一", blind_only=True, skip_missing_figures=False)

    def test_multi_batch_numeral_with_explicit_batch_is_served(self) -> None:
        items = [
            self._item(id="b1", batch="1", practice_mode="read_only"),
            self._item(id="b2", batch="2"),
        ]
        chosen = practice.select(
            items, tag=None, year="2013下", numeral="一", batch="2", blind_only=True, skip_missing_figures=False
        )
        self.assertEqual([i["id"] for i in chosen], ["b2"])


class ShippedPaperSafetyTests(unittest.TestCase):
    def test_shipped_gate_blocks_the_declared_incomplete_item(self) -> None:
        items = {item["id"]: item for item in practice.build_case_items()}
        self.assertTrue(items["past-papers/case-by-year/2019下.md#试题五"]["declared_incomplete"])
        self.assertTrue(items["past-papers/case-by-year/2025下.md#批次2-试题一"]["recall_outline"])

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


class MaterialGateTests(unittest.TestCase):
    """裁切插图与压缩回忆版题面必须在出题前被拦下，而不是靠人肉看图。"""

    def _item(self, **overrides):
        base = {
            "id": "fixture:gate",
            "subject": "case",
            "year": "2013下",
            "numeral": "一",
            "tag": "案例 01",
            "practice_mode": "blind",
            "missing_figure": False,
            "figures": [],
            "stem": "阅读以下叙述。\n\n【说明】\n" + "系统背景与设计要求。" * 30,
        }
        base.update(overrides)
        return base

    def test_declared_incomplete_material_is_blocked_without_a_shape_rule(self) -> None:
        items = {item["id"]: item for item in practice.build_case_items()}
        item = items["past-papers/case-by-year/2019下.md#试题五"]
        # 它没有任何结构性质量问题，唯一依据是登记表对内容的核对结论
        self.assertEqual([], item["quality_issues"])
        self.assertTrue(item["declared_incomplete"])
        self.assertFalse(item["complete"])
        self.assertFalse(practice.eligible(item))

    def test_figure_shape_never_decides_eligibility(self) -> None:
        # 运行时不读像素尺寸：竖版、超高的插图只要内容没问题就照常出题
        self.assertFalse(hasattr(practice, "figure_size"))
        self.assertFalse(hasattr(practice, "CROPPED_FIGURE_RATIO"))
        items = {item["id"]: item for item in practice.build_case_items()}
        for item_id in (
            "past-papers/case-by-year/2023下.md#试题一",
            "past-papers/case-by-year/2015下-原卷.md#试题二",
        ):
            with self.subTest(item=item_id):
                self.assertTrue(practice.eligible(items[item_id]))

    def test_outline_only_recall_stem_is_marked_but_still_practiceable(self) -> None:
        assessed = practice.assess_materials(
            self._item(
                stem="### 题目描述\n\n某电商系统缓存击穿。\n\n### 问题1（13分）\n说明两种方案的实现原理。\n\n### 问题2（12分）\n对比两种方案。"
            )
        )
        # 保真度分级，不是材料缺陷：不进 quality_issues，仍可作单题练习
        self.assertTrue(assessed["recall_outline"])
        self.assertNotIn("recall_outline_stem", assessed["quality_issues"])
        self.assertNotIn("incomplete_scoring_material", assessed["quality_issues"])

    def test_shipped_recall_outline_item_stays_eligible_for_single_case_practice(self) -> None:
        items = {item["id"]: item for item in practice.build_case_items()}
        item = items["past-papers/case-by-year/2025下.md#批次2-试题二"]
        self.assertTrue(item["recall_outline"])
        # 微服务赛道就靠这道题维持单题训练；只有整卷组卷会排除它
        self.assertTrue(practice.eligible(item))

    def test_stem_with_intro_section_is_never_flagged_as_outline(self) -> None:
        assessed = practice.assess_materials(self._item(stem="【说明】\n" + "背景。" * 5))
        self.assertFalse(assessed["recall_outline"])


if __name__ == "__main__":
    unittest.main()
