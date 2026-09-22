from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_frequency_snapshot.py"
SNAPSHOT = REPO_ROOT / "tutor" / "frequency-snapshot.json"


class FrequencySnapshotTest(unittest.TestCase):
    def test_snapshot_is_current_and_auditable(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual({"stable": 0.8, "recent": 0.2}, payload["weights"])
        self.assertRegex(payload["source_digest"], r"^[0-9a-f]{64}$")
        topic_ids = [item["topic_id"] for item in payload["topics"]]
        self.assertEqual(len(topic_ids), len(set(topic_ids)))
        for cohort in ("stable", "recent"):
            coverage = payload["coverage"][cohort]
            self.assertEqual(
                coverage["ready_questions"],
                coverage["mapped_questions"] + coverage["unmapped_questions"],
            )
            self.assertGreater(coverage["papers"], 0)
            self.assertGreaterEqual(coverage["ratio"], 0)
            self.assertLessEqual(coverage["ratio"], 1)

        thresholds = payload["activation_thresholds"]
        runtime_ready = (
            payload["coverage"]["stable"]["ratio"]
            >= thresholds["stable_coverage"]
            and payload["coverage"]["recent"]["ratio"]
            >= thresholds["recent_coverage"]
        )
        self.assertEqual(
            "runtime_ready" if runtime_ready else "report_only",
            payload["mode"],
        )


if __name__ == "__main__":
    unittest.main()
