"""Regression coverage for the paper-normalization lossless verifier."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_paper_normalization.py"


class VerifyPaperNormalizationTests(unittest.TestCase):
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
