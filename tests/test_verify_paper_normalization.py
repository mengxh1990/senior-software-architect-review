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
    def test_only_the_exact_invalid_baseline_additions_are_allowlisted(self) -> None:
        additions = verify_paper_normalization.BASELINE_UNPARSEABLE_ADDITIONS
        self.assertEqual(
            additions["past-papers/comprehensive-by-year/2012下.md"],
            {(69, 69): "baseline_missing_required_options"},
        )
        self.assertEqual(
            additions["past-papers/comprehensive-by-year/2013下.md"],
            {(47, 51): "baseline_html_table_option_set_unparseable"},
        )
        self.assertEqual(
            verify_paper_normalization.BASELINE_TRAILING_POLLUTION_FIELDS[
                "past-papers/comprehensive-by-year/2013下.md"
            ],
            {(45, 46): {"explanation": "baseline_consumed_following_question_blocks"}},
        )
        self.assertEqual(
            verify_paper_normalization.BASELINE_RAW_ASSET_FIELD_EXCEPTIONS[
                "past-papers/comprehensive-by-year/2011下.md"
            ],
            {
                (2, 4): {"stem": "baseline_embedded_raw_figure_path"},
                (69, 69): {"stem": "baseline_embedded_raw_figure_path"},
            },
        )
        self.assertEqual(
            verify_paper_normalization.BASELINE_TRAILING_POLLUTION_FIELDS[
                "past-papers/comprehensive-by-year/2011下.md"
            ],
            {
                (1, 1): {"explanation": "baseline_consumed_following_figure_intro"},
                (70, 70): {"explanation": "baseline_consumed_following_english_passage"},
            },
        )

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


if __name__ == "__main__":
    unittest.main()
