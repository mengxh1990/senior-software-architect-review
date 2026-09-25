"""History curation must remove training entries without corrupting retained IDs."""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import paper_practice
import tag_case_essay
import tutor


class HistoricalCurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = json.loads((ROOT / "scripts/historical_subjective_review.json").read_text())
        cls.tags = tag_case_essay.load_tag_map()

    def test_old_comprehensive_sources_are_absent_from_files_imports_and_pool(self):
        directory = ROOT / "past-papers/comprehensive-by-year"
        for year in range(2009, 2018):
            self.assertFalse((directory / f"{year}下.md").exists())
        manifest = json.loads((ROOT / "scripts/las_import_manifest.json").read_text())
        self.assertFalse(any(re.match(r"20(?:09|1[0-7])", d["label"]) for d in manifest["documents"]))
        pool = tutor.load_quiz_question_pool(tutor.load_curriculum())
        for question in pool:
            self.assertNotRegex(question["id"], r"comprehensive-by-year/20(?:09|1[0-7])")

    def test_review_covers_all_original_questions_and_retained_ids(self):
        for kind, original, retained in (("case", 45, 30), ("essay", 36, 28)):
            rows = [r for group in self.review[kind].values() for r in group]
            self.assertEqual(original, len(rows))
            self.assertEqual(retained, sum(r["keep"] for r in rows))
            self.assertTrue(all(r["reason"] for r in rows))
        self.assertEqual([], tag_case_essay.validate_tag_map(self.tags))
        self.assertEqual([], tag_case_essay.validate_historical_review(self.tags))

    def test_mislabeling_and_reintroducing_removed_questions_fail_validation(self):
        wrong = copy.deepcopy(self.tags)
        wrong["case"]["2015下"][1]["tag"] = "案例 08"
        self.assertTrue(tag_case_essay.validate_historical_review(wrong))
        wrong = copy.deepcopy(self.tags)
        wrong["case"]["2012下"].insert(1, {"number": "二", "tag": "案例 05", "label": "微服务"})
        self.assertTrue(tag_case_essay.validate_historical_review(wrong))
        wrong = copy.deepcopy(self.tags)
        wrong["case"]["2010下"][0]["number"] = "二"
        self.assertTrue(tag_case_essay.validate_tag_map(wrong))

    def test_apply_preflight_allows_untagged_source_but_check_rejects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "case").mkdir()
            (base / "essay").mkdir()
            (base / "case" / "2022.md").write_text("## 试题一\n\n题干\n")
            tags = {"case": {"2022": [{"number": "一", "tag": "案例 01", "label": "质量属性"}]}, "essay": {}}
            with patch.object(tag_case_essay, "PAPERS", {"case": base / "case", "essay": base / "essay"}):
                self.assertEqual([], tag_case_essay.validate_tag_map(tags, check_displayed=False))
                self.assertTrue(tag_case_essay.validate_tag_map(tags))

    def test_retained_source_and_answer_ids_and_images_match(self):
        items = paper_practice.build_case_items()
        by_id = {item["id"]: item for item in items}
        for year, rows in self.review["case"].items():
            for row in rows:
                source = f'past-papers/case-by-year/{year}-原卷.md#试题{row["number"]}'
                answer = f'past-papers/case-by-year/{year}.md#试题{row["number"]}'
                if not row["keep"]:
                    self.assertNotIn(source, by_id)
                    self.assertNotIn(answer, by_id)
                    continue
                item = by_id[source]
                self.assertEqual("blind", item["practice_mode"])
                self.assertEqual(row["tag"], item["tag"])
                self.assertEqual(answer, item["answer_source"])
                self.assertTrue(by_id[answer]["answer"] or by_id[answer]["stem"])
                self.assertTrue(item["selection_only"])
                for image in item["figures"]:
                    self.assertTrue((ROOT / "past-papers/assets" / year / image).is_file())

    def test_classic_essays_have_unique_ids_and_do_not_expose_reference_answers(self):
        items = [i for i in paper_practice.build_essay_items() if i["selection_only"]]
        self.assertEqual(28, len(items))
        self.assertEqual(len(items), len({i["id"] for i in items}))
        for item in items:
            self.assertEqual("blind", item["practice_mode"])
            self.assertTrue(item["answer"], item["id"])
            self.assertNotRegex(item["stem"], r"参考答案|写作要点|一、应结合自己")
            self.assertRegex(item["stem"], "请围绕|请以")


if __name__ == "__main__":
    unittest.main()
