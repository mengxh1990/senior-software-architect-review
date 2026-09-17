"""Tests for :mod:`scripts.import_las_papers`.

The importer is the only path that turns purchased scan transcripts into repo
content, so the contract pinned here is: watermark boilerplate never reaches a
committed file, the three exam dimensions are split correctly for both LAS
layouts, and advertiser/stamp images are dropped while figures survive.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "import_las_papers.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("import_las_papers", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_las_papers"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


importer = _load_module()


class BoilerplateTests(unittest.TestCase):
    def test_watermark_lines_are_recognised(self) -> None:
        for line in (
            "软考达人",
            "软考达人 - 高效提分的软考题库",
            "ruankaodaren.com",
            "微信搜一搜",
            "第3页,共33页",
            "2013年下半年 系统架构设计师 上午试卷 第13页（共13页）",
            "扫描全能王 创建",
            '<p class="header">软考达人 - 高效提分的软考题库</p>',
        ):
            with self.subTest(line=line):
                self.assertTrue(importer.is_watermark_line(line))

    def test_exam_content_is_never_treated_as_watermark(self) -> None:
        for line in (
            "（1）A. 操作系统、应用软件和其他系统软件",
            "【答案】B",
            "【解析】本题考查计算机系统中软件方面的基本知识。",
            "系统架构设计师考试科目的设置包括综合知识。",
        ):
            with self.subTest(line=line):
                self.assertFalse(importer.is_watermark_line(line))

    def test_strip_boilerplate_keeps_content_and_collapses_blanks(self) -> None:
        text = "\n".join(
            [
                "软考达人",
                "题干第一行",
                "",
                "",
                "第1页,共10页",
                "答案：A",
            ]
        )
        cleaned = importer.strip_boilerplate(text)
        self.assertIn("题干第一行", cleaned)
        self.assertIn("答案：A", cleaned)
        self.assertNotIn("软考达人", cleaned)
        self.assertNotIn("第1页", cleaned)
        self.assertNotIn("\n\n\n", cleaned)


class SectionSplitTests(unittest.TestCase):
    def test_answer_book_layout_splits_three_dimensions(self) -> None:
        text = "\n".join(
            [
                "某系统采用三层结构，(1) 表示。",
                "【答案】A",
                "【解析】本题考查基础知识。",
                "",
                "试题一",
                "",
                "某公司要建在线交易平台。",
                "【问题1】（9分）",
                "请列举质量属性。",
                "",
                "试题二",
                "【问题1】略。",
                "",
                "# 试题一 论基于 DSSA 的软件架构设计与应用",
                "",
                "请围绕该论题论述。",
            ]
        )
        sections = importer.split_sections(text)
        self.assertIn("三层结构", sections.comprehensive)
        self.assertIn("在线交易平台", sections.case)
        self.assertIn("论基于 DSSA", sections.essay)
        self.assertNotIn("试题一", sections.comprehensive)
        self.assertEqual(sections.markers["case"], "bare-试题")

    def test_recall_layout_splits_on_headings(self) -> None:
        text = "\n".join(
            [
                "# 综合知识",
                "",
                "1. 题干 A B C D",
                "答案：A",
                "",
                "# 案例分析",
                "",
                "## 试题一",
                "案例题干",
                "",
                "# 论文写作",
                "",
                "## 试题一：论软件测试方法及应用",
                "论述要点",
            ]
        )
        sections = importer.split_sections(text)
        self.assertIn("题干 A B C D", sections.comprehensive)
        self.assertIn("案例题干", sections.case)
        self.assertIn("论软件测试方法", sections.essay)
        self.assertNotIn("论软件测试方法", sections.case)

    def test_case_only_document_has_no_comprehensive_section(self) -> None:
        text = "\n".join(["# 案例分析", "", "## 试题一", "案例题干"])
        sections = importer.split_sections(text)
        self.assertEqual(sections.comprehensive, "")
        self.assertIn("案例题干", sections.case)

    def test_essay_starts_at_second_first_question_marker(self) -> None:
        """Answer books restart numbering, so 论文 begins at the 2nd 试题一."""
        text = "\n".join(
            [
                "综合题干",
                "【答案】A",
                "试题一",
                "案例一",
                "试题二",
                "案例二",
                "试题三 数据库 Redis",
                "案例三",
                "试题四：",
                "案例四",
                "试题五",
                "案例五",
                "试题一 论模型驱动架构在系统开发中的应用",
                "论文一",
                "试题二",
                "论文二",
            ]
        )
        sections = importer.split_sections(text)
        self.assertIn("案例五", sections.case)
        self.assertNotIn("论文一", sections.case)
        self.assertIn("论模型驱动架构", sections.essay)
        self.assertIn("论文二", sections.essay)

    def test_prose_lines_are_not_headings(self) -> None:
        self.assertIsNone(importer.parse_heading("试题一是必答题，每题15分。"))
        self.assertIsNone(importer.parse_heading("试题由项目组讨论确定。"))
        self.assertEqual(importer.parse_heading("试题三 数据库 Redis"), ("三", "数据库 Redis"))
        self.assertEqual(importer.parse_heading("## 试题一：论软件测试方法及应用"), ("一", "论软件测试方法及应用"))

    def test_numbered_case_document_is_split_as_case(self) -> None:
        """Some case papers number questions as ``1、阅读以下...``."""
        text = "\n".join(
            [
                "2019 年系统架构师考试科目二：案例分析",
                "",
                "1、阅读以下关于软件架构设计与评估的叙述，回答问题1和问题2。",
                "题干一",
                "【问题1】(13 分)",
                "【问题1解析】答案一",
                "",
                "2、阅读下列说明，回答问题1至问题3。",
                "题干二",
                "【问题1】(8分)",
                "【问题1解析】答案二",
            ]
        )
        sections = importer.split_sections(text)
        self.assertEqual(sections.comprehensive, "2019 年系统架构师考试科目二：案例分析")
        self.assertIn("题干一", sections.case)
        normalized = importer.normalize_section_headings(sections.case, "case")
        self.assertIn("## 试题一", normalized)
        self.assertIn("## 试题二", normalized)
        self.assertIn("#### 【问题1解析】", normalized)

    def test_score_suffix_stays_in_case_heading(self) -> None:
        normalized = importer.normalize_section_headings("## 试题一（共 25 分）\n\n题干", "case")
        self.assertIn("## 试题一（共 25 分）", normalized)

    def test_whole_document_body_drops_cover_preamble(self) -> None:
        text = "\n".join(
            [
                "全国计算机技术与软件专业技术资格（水平）考试",
                "2022 年 系统架构设计师 下午试卷 II",
                "请按下述要求正确填写答题纸",
                "试题一 论基于构件的软件开发方法及其应用",
                "题干一",
            ]
        )
        body = importer.whole_document_body(text)
        self.assertTrue(body.startswith("试题一 论基于构件的软件开发方法及其应用"))
        self.assertNotIn("请按下述要求正确填写答题纸", body)


class ImageDecisionTests(unittest.TestCase):
    def test_advertiser_banner_is_dropped(self) -> None:
        decision = importer.classify_image(293, 88)
        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "advertiser-banner")

    def test_exam_figure_is_kept(self) -> None:
        for width, height in ((955, 249), (566, 533), (287, 133), (1000, 523)):
            with self.subTest(size=(width, height)):
                self.assertTrue(importer.classify_image(width, height).keep)

    def test_repeated_small_stamp_is_dropped(self) -> None:
        self.assertFalse(importer.classify_image(200, 60, duplicate_count=5).keep)
        self.assertTrue(importer.classify_image(600, 400, duplicate_count=5).keep)


class LinkRewriteTests(unittest.TestCase):
    def test_dropped_images_disappear_and_kept_images_are_relative(self) -> None:
        body = "题干\n\n![](images/p1_000.png)\n\n![](images/p6_004.png)\n"
        rewritten = importer.rewrite_image_links(body, {"p1_000.png": "../assets/2009下/p1_000.png"})
        self.assertIn("../assets/2009下/p1_000.png", rewritten)
        self.assertNotIn("p6_004.png", rewritten)

    def test_extract_image_names_ignores_remote_urls(self) -> None:
        body = "![](images/a.png)\n![](https://example.com/b.png)\n![](images/a.png)"
        self.assertEqual(importer.extract_image_names(body), ["a.png"])

    def test_reviewed_removal_leaves_a_visible_note(self) -> None:
        body = "题干\n\n![](images/p1_002.png)\n"
        noted = {"p1_002.png": "*（原图含机构广告或水印，已移除）*"}
        rewritten = importer.rewrite_image_links(body, {}, noted)
        self.assertIn("原图含机构广告或水印，已移除", rewritten)
        self.assertNotIn("p1_002.png", rewritten)


class ReviewedRemovalTests(unittest.TestCase):
    def test_manifest_removals_are_marked_for_deletion(self) -> None:
        from pathlib import Path

        decisions = importer.plan_images([], extra_drops={"p1_002.png": "机构广告"})
        self.assertFalse(decisions["p1_002.png"].keep)
        self.assertTrue(decisions["p1_002.png"].reason.startswith("reviewed-removal"))

    def test_shipped_manifest_has_no_leftover_advert_images(self) -> None:
        """Every reviewed removal must be recorded, and assets must not contain them."""
        import json

        manifest = json.loads((REPO_ROOT / "scripts" / "las_import_manifest.json").read_text(encoding="utf-8"))
        drops = {
            (document["label"], Path(name).stem)
            for document in manifest["documents"]
            for name in (document.get("drop_images") or {})
        }
        self.assertGreaterEqual(len(drops), 30, "广告/水印剔除清单不应为空")
        for label, stem in drops:
            with self.subTest(asset=f"{label}/{stem}"):
                self.assertFalse(
                    (REPO_ROOT / "past-papers" / "assets" / label / f"{stem}.webp").exists(),
                    f"{label}/{stem} 属于广告或水印图，必须已从仓库删除",
                )


class HeadingNormalizationTests(unittest.TestCase):
    def test_case_headings_become_markdown(self) -> None:
        body = "试题一\n\n题干\n\n【问题1】（9分）\n\n参考答案"
        normalized = importer.normalize_section_headings(body, "case")
        self.assertIn("## 试题一", normalized)
        self.assertIn("### 【问题1】 （9分）", normalized)

    def test_essay_headings_become_markdown(self) -> None:
        body = "# 试题二 论信息系统建模方法\n\n题干"
        normalized = importer.normalize_section_headings(body, "essay")
        self.assertIn("## 试题二：论信息系统建模方法", normalized)
        self.assertNotIn("# 试题二 论信息系统建模方法", normalized)

    def test_comprehensive_body_is_left_untouched(self) -> None:
        body = "1. 题干\nA. 甲 B. 乙\n\n【答案】A"
        self.assertEqual(importer.normalize_section_headings(body, "comprehensive"), body)

    def test_summarize_counts_markers(self) -> None:
        body = "# 题\n\n【答案】A\n\n【解析】略\n\n## 试题一\n\n【问题1】（9分）"
        stats = importer.summarize(body, "case")
        self.assertEqual(stats["answers"], 1)
        self.assertEqual(stats["questions"], 1)
        self.assertEqual(stats["cases"], 1)


class WatermarkSuppressionTests(unittest.TestCase):
    def _image(self, colored: bool):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (300, 200), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 280, 180), outline=(40, 40, 40), width=2)
        draw.text((40, 90), "exam content", fill=(20, 20, 20))
        if colored:
            draw.rectangle((40, 40, 120, 70), fill=(60, 120, 200))
            draw.rectangle((150, 40, 230, 70), fill=(200, 60, 60))
            draw.rectangle((40, 120, 280, 160), fill=(80, 160, 90))
        # faint pink watermark stroke
        draw.line((0, 200, 300, 0), fill=(238, 190, 198), width=12)
        return image

    def test_pink_watermark_on_grey_figure_is_whitened(self) -> None:
        image = self._image(colored=False)
        cleaned = importer.suppress_print_watermark(image)
        before = image.convert("RGB").getpixel((150, 100))
        after = cleaned.getpixel((150, 100))
        self.assertGreater(before[0] - before[2], 10)
        self.assertEqual(after, (255, 255, 255))
        # dark content survives
        self.assertLess(sum(cleaned.getpixel((20, 100))), 255 * 3)

    def test_coloured_figure_is_returned_untouched(self) -> None:
        image = self._image(colored=True)
        cleaned = importer.suppress_print_watermark(image)
        self.assertEqual(cleaned.tobytes(), image.convert("RGB").tobytes())


if __name__ == "__main__":
    unittest.main()
