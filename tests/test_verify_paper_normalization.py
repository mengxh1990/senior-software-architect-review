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

    def test_shipped_papers_match_the_normalization_baseline(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("内容校验通过", result.stdout)


if __name__ == "__main__":
    unittest.main()
