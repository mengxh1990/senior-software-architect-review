"""Regression coverage for the paper-normalization lossless verifier."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_paper_normalization.py"


def _load_verifier():
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_paper_normalization", VERIFY_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_paper_normalization = _load_verifier()


class VerifyPaperNormalizationTests(unittest.TestCase):
    def test_normalization_exceptions_only_reference_retained_papers(self) -> None:
        for name in ("BASELINE_UNPARSEABLE_ADDITIONS", "BASELINE_TRAILING_POLLUTION_FIELDS",
                     "BASELINE_RAW_ASSET_FIELD_EXCEPTIONS", "REVIEWED_GROUP_SPLITS",
                     "REVIEWED_CONTENT_FIXES", "REVIEWED_ITEM_DIGESTS"):
            for relative in getattr(verify_paper_normalization, name):
                with self.subTest(name=name, path=relative):
                    self.assertTrue((REPO_ROOT / relative).is_file())

    def test_shipped_papers_match_the_normalization_baseline(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("内容校验通过", result.stdout)

    def test_reviewed_content_fixes_match_the_shipped_paper_exactly(self) -> None:
        import importlib.util

        sanitizer_path = REPO_ROOT / "scripts" / "sanitize_bank.py"
        spec = importlib.util.spec_from_file_location("sanitize_bank", sanitizer_path)
        assert spec and spec.loader
        sanitizer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sanitizer)

        for relative, fixes in verify_paper_normalization.REVIEWED_CONTENT_FIXES.items():
            items = {
                (item["range"][0], item["range"][1]): item
                for item in sanitizer.parse_paper(REPO_ROOT / relative)
            }
            for key, fields in fixes.items():
                for field, expected in fields.items():
                    with self.subTest(path=relative, key=key, field=field):
                        self.assertEqual(expected, items[key][field])

    def test_reviewed_pdf_repairs_are_pinned_to_exact_content(self) -> None:
        import importlib.util

        sanitizer_path = REPO_ROOT / "scripts" / "sanitize_bank.py"
        spec = importlib.util.spec_from_file_location("sanitize_bank", sanitizer_path)
        assert spec and spec.loader
        sanitizer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sanitizer)

        for relative, digests in verify_paper_normalization.REVIEWED_ITEM_DIGESTS.items():
            items = {
                (item["range"][0], item["range"][1]): item
                for item in sanitizer.parse_paper(REPO_ROOT / relative)
            }
            for key, expected in digests.items():
                with self.subTest(path=relative, key=key):
                    self.assertEqual(expected, verify_paper_normalization.content_digest(items[key]))
                    altered = {**items[key], "stem": items[key]["stem"] + "变动"}
                    self.assertNotEqual(expected, verify_paper_normalization.content_digest(altered))

    def test_reviewed_group_splits_expose_one_usable_item_per_blank(self) -> None:
        import importlib.util

        sanitizer_path = REPO_ROOT / "scripts" / "sanitize_bank.py"
        spec = importlib.util.spec_from_file_location("sanitize_bank", sanitizer_path)
        assert spec and spec.loader
        sanitizer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sanitizer)

        shipped = 0
        for relative, groups in verify_paper_normalization.REVIEWED_GROUP_SPLITS.items():
            items = {
                (item["range"][0], item["range"][1]): item
                for item in sanitizer.parse_paper(REPO_ROOT / relative)
            }
            for start, end in sorted(groups):
                carrier = items[(start, end)]
                with self.subTest(path=relative, group=f"{start}-{end}"):
                    self.assertNotEqual("ready", carrier["quality_status"])
                for number in range(start, end + 1):
                    child = items.get((number, number))
                    with self.subTest(path=relative, child=number):
                        self.assertIsNotNone(child)
                        # A blank may be deliberately retired through the
                        # maintainer deny-list; otherwise it must be servable.
                        self.assertTrue(
                            child["quality_status"] == "ready"
                            or verify_paper_normalization.retired_by_maintainer(child),
                            child.get("quality_issues"),
                        )
                        self.assertEqual(1, sanitizer.question_span(child))
                        self.assertEqual(1, len(child["correct"]))
                        self.assertTrue(child.get("context"), "子题必须带上共享题干")
                        shipped += 1
        expected = sum(end - start + 1 for groups in verify_paper_normalization.REVIEWED_GROUP_SPLITS.values() for start, end in groups)
        self.assertGreater(expected, 0)
        self.assertEqual(shipped, expected)


if __name__ == "__main__":
    unittest.main()
