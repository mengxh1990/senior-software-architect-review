"""Black-box acceptance tests for the private exam tutor.

Run with::

    python3 -m unittest discover -s tests -v

The tests intentionally exercise ``scripts/tutor.py`` through its command line.
That keeps the learner-state format free to evolve while protecting the product
rules that matter: safe private progress, evidence-based mastery, and pass-first
recommendations.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
TUTOR_SCRIPT = REPO_ROOT / "scripts" / "tutor.py"
CURRICULUM_PATH = REPO_ROOT / "tutor" / "curriculum.json"
SUBJECTS = {"comprehensive", "case", "essay"}
PASS_READY_STATES = {"mastered", "pass_ready", "pass-ready", "ready"}


def _run_cli(
    data_dir: Path,
    *arguments: str,
    expected_returncode: int | None = 0,
) -> subprocess.CompletedProcess[str]:
    """Run the public CLI and include useful diagnostics on failure."""

    resolved_arguments = list(arguments)
    # Most tests care about higher-level progress behavior. Give every record a
    # deterministic independent item ID unless the scenario explicitly tests
    # repeated-item behavior with its own --item-id.
    if resolved_arguments[:1] == ["record"] and "--item-id" not in resolved_arguments:
        attempt_index = resolved_arguments.index("--attempt-id") + 1
        resolved_arguments.extend(
            ["--item-id", f"test-item:{resolved_arguments[attempt_index]}"]
        )
    command = [
        sys.executable,
        str(TUTOR_SCRIPT),
        "--data-dir",
        str(data_dir),
        *resolved_arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if expected_returncode is not None and completed.returncode != expected_returncode:
        raise AssertionError(
            f"command returned {completed.returncode}, expected "
            f"{expected_returncode}: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def _json_output(completed: subprocess.CompletedProcess[str]) -> Any:
    """Decode a JSON CLI response, accepting harmless surrounding whitespace."""

    output = completed.stdout.strip()
    if not output:
        raise AssertionError(f"expected JSON output; stderr was:\n{completed.stderr}")
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise AssertionError(f"CLI output is not JSON:\n{output}") from error


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _values_for_key(value: Any, wanted_key: str) -> list[Any]:
    values: list[Any] = []
    for node in _walk(value):
        if isinstance(node, dict) and wanted_key in node:
            values.append(node[wanted_key])
    return values


def _find_topic_record(status: Any, topic_id: str) -> dict[str, Any]:
    """Find a topic whether the status uses a mapping or a list schema."""

    for node in _walk(status):
        if not isinstance(node, dict):
            continue
        if topic_id in node and isinstance(node[topic_id], dict):
            return node[topic_id]
        if node.get("topic_id") == topic_id or node.get("id") == topic_id:
            return node
    raise AssertionError(f"status does not expose progress for topic {topic_id}")


def _status_label(topic_record: dict[str, Any], skill: str = "recognition") -> str:
    """Read the visible learning-state label without fixing the whole schema."""

    for key in ("status", "state", "stage", "mastery_status"):
        value = topic_record.get(key)
        if isinstance(value, str):
            return value.lower()

    skill_record = topic_record.get(skill)
    if isinstance(skill_record, dict):
        for key in ("status", "state", "stage", "mastery_status"):
            value = skill_record.get(key)
            if isinstance(value, str):
                return value.lower()

    mastery = topic_record.get("mastery")
    if isinstance(mastery, dict):
        skill_record = mastery.get(skill)
        if isinstance(skill_record, dict):
            for key in ("status", "state", "stage", "mastery_status"):
                value = skill_record.get(key)
                if isinstance(value, str):
                    return value.lower()

    raise AssertionError("topic progress must expose a status/state/stage label")


def _snapshot_files(directory: Path) -> dict[str, bytes]:
    """Return an exact persisted-data snapshot after a CLI command finishes."""

    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _recommendation_items(payload: Any) -> list[dict[str, Any]]:
    """Extract the ordered task list from the recommendation response."""

    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict):
        for key in ("recommendations", "items", "tasks", "plan"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    raise AssertionError("recommend --json must expose an ordered recommendation list")


def _recommendation_topic_id(item: dict[str, Any]) -> str:
    for key in ("topic_id", "id", "topic"):
        value = item.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("id") or value.get("topic_id")
            if isinstance(nested, str):
                return nested
    raise AssertionError(f"recommendation has no topic id: {item!r}")


def _find_subject_allocation(payload: Any) -> dict[str, float]:
    for node in _walk(payload):
        if not isinstance(node, dict) or not SUBJECTS.issubset(node):
            continue
        if all(isinstance(node[subject], (int, float)) for subject in SUBJECTS):
            return {subject: float(node[subject]) for subject in SUBJECTS}
    raise AssertionError(
        "recommend --json must expose numeric comprehensive/case/essay allocation"
    )


def _append_mock_gap_session(
    data_dir: Path,
    mock_id: str,
    *,
    at: str = "2026-08-10T10:00:00+08:00",
    wrong_items: Iterable[tuple[str, str, str | None]] = (),
) -> None:
    """Append one whole-mock event plus its wrong per-question events.

    Mirrors what the local exam terminal persists for a submitted paper so
    diagnose/recommend can be exercised without the web server.
    """

    events = [
        {
            "attempt_id": mock_id,
            "event_type": "mock",
            "topic_id": None,
            "item_id": f"paper-{mock_id}",
            "facet": None,
            "at": at,
            "subject": "comprehensive",
            "skill": "recognition",
            "mode": "full_mock",
            "score": 40,
            "max_score": 75,
            "duration_seconds": 5400,
            "word_count": None,
            "complete": True,
            "confidence": "sure",
            "wrong_reasons": [],
            "source_type": "simulation",
            "source": f"paper-{mock_id}",
            "feedback_seen": True,
        }
    ]
    for number, (topic_id, item_id, facet) in enumerate(wrong_items, 1):
        events.append(
            {
                "attempt_id": f"{mock_id}-q-{number:02d}",
                "event_type": "practice",
                "topic_id": topic_id,
                "item_id": item_id,
                "facet": facet,
                "at": at,
                "subject": "comprehensive",
                "skill": "recognition",
                "mode": "mock",
                "score": 0,
                "max_score": 1,
                "duration_seconds": 60,
                "word_count": None,
                "complete": False,
                "confidence": "sure",
                "wrong_reasons": ["knowledge_gap"],
                "source_type": "simulation",
                "source": item_id,
                "feedback_seen": True,
            }
        )
    with (data_dir / "attempts.jsonl").open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _primary_state_file(data_dir: Path) -> Path:
    """Locate the mutable state document while ignoring backups."""

    preferred_names = ("state.json", "progress.json", "profile.json")
    json_files = [
        path
        for path in data_dir.rglob("*.json")
        if "backup" not in {part.lower() for part in path.parts}
        and not path.name.endswith((".bak.json", ".backup.json"))
    ]
    for name in preferred_names:
        for path in json_files:
            if path.name == name:
                return path
    if len(json_files) == 1:
        return json_files[0]
    raise AssertionError(
        "could not identify the primary learner state JSON; found "
        + ", ".join(str(path.relative_to(data_dir)) for path in json_files)
    )


class TutorAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not TUTOR_SCRIPT.is_file():
            raise AssertionError(f"missing {TUTOR_SCRIPT}")
        if not CURRICULUM_PATH.is_file():
            raise AssertionError(f"missing {CURRICULUM_PATH}")
        cls.curriculum = json.loads(CURRICULUM_PATH.read_text(encoding="utf-8"))
        cls.topics = cls.curriculum.get("topics", [])
        if not isinstance(cls.topics, list):
            raise AssertionError("curriculum topics must be a list")
        if not cls.topics:
            raise AssertionError("curriculum topics must not be empty")

    def _init(self, data_dir: Path) -> None:
        _run_cli(
            data_dir,
            "init",
            "--exam-date",
            "2026-11-07",
            "--daily-minutes",
            "45",
            "--background",
            "backend",
        )

    def _status(self, data_dir: Path) -> Any:
        return _json_output(_run_cli(data_dir, "status", "--json"))

    def _recognition_topic(self) -> dict[str, Any]:
        candidates = [
            topic
            for topic in self.topics
            if "recognition" in topic.get("skills", [])
            and "comprehensive" in topic.get("subjects", [])
        ]
        self.assertTrue(candidates, "curriculum needs a comprehensive recognition topic")
        return max(
            candidates,
            key=lambda topic: (
                float(topic.get("frequency_count", 0)),
                float(topic.get("priority_weight", 0)),
                str(topic["id"]),
            ),
        )

    def test_curriculum_has_unique_stable_ids_and_existing_resources(self) -> None:
        self.assertIsInstance(self.curriculum.get("schema_version"), int)
        self.assertIn("strategy", self.curriculum)

        ids: list[str] = []
        required = {
            "id",
            "name",
            "subjects",
            "skills",
            "frequency_count",
            "priority_weight",
            "quick_win",
            "cross_subject_value",
            "estimated_minutes",
            "resources",
        }
        for topic in self.topics:
            with self.subTest(topic=topic.get("id", topic.get("name"))):
                self.assertTrue(required.issubset(topic), required - set(topic))
                topic_id = topic["id"]
                self.assertIsInstance(topic_id, str)
                self.assertRegex(topic_id, r"^[KCP]\d{2}(?:[._-][A-Z0-9]+)*$")
                ids.append(topic_id)

                self.assertTrue(topic["subjects"])
                self.assertTrue(set(topic["subjects"]).issubset(SUBJECTS))
                self.assertTrue(topic["skills"])
                self.assertTrue(topic["resources"])
                for resource in topic["resources"]:
                    self.assertIsInstance(resource, str)
                    local_path = resource.split("#", 1)[0]
                    self.assertTrue(local_path, "resource must name a local file")
                    resolved = (REPO_ROOT / local_path).resolve()
                    self.assertTrue(
                        resolved.is_relative_to(REPO_ROOT),
                        f"resource escapes repository: {resource}",
                    )
                    self.assertTrue(resolved.exists(), f"missing resource: {resource}")

        self.assertEqual(len(ids), len(set(ids)), "curriculum topic IDs must be unique")

    def test_high_value_gap_topics_are_in_cold_start_diagnostics(self) -> None:
        expected = {
            "K23.PROJECT_MANAGEMENT_METRICS",
            "K24.INFORMATION_SYSTEMS",
            "K25.RELIABILITY_ENGINEERING",
            "K26.ARCH_EVOLUTION",
            "K27.EMERGING_TECH",
            "K28.MATH_OPERATIONS",
        }
        topics = {topic["id"]: topic for topic in self.topics}
        grouped = {
            topic_id
            for group in self.curriculum["strategy"]["comprehensive_cold_start_groups"]
            for topic_id in group
        }

        self.assertTrue(expected.issubset(topics))
        self.assertTrue(expected.issubset(grouped))
        for topic_id in expected:
            with self.subTest(topic=topic_id):
                self.assertIn("comprehensive", topics[topic_id]["subjects"])
                self.assertIn("recognition", topics[topic_id]["skills"])

    def test_init_persists_profile_and_status_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private-study"
            self._init(data_dir)
            self.assertTrue(data_dir.is_dir())
            self.assertTrue(any(data_dir.iterdir()), "init did not persist learner state")
            self.assertTrue((data_dir / "state.json.bak").is_file())

            status = self._status(data_dir)
            self.assertIn("2026-11-07", _values_for_key(status, "exam_date"))
            self.assertIn(45, _values_for_key(status, "daily_minutes"))
            self.assertIn("backend", _values_for_key(status, "background"))

    def test_configured_review_floor_does_not_delay_wrong_answers(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "breadth-floor-wrong",
                "--at",
                "2026-08-10T09:00:00+08:00",
                "--wrong-reason",
                "knowledge_gap",
            )
            before = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(
                before["mastery"]["recognition"]["next_review_at"],
                "2026-08-11",
            )

            _run_cli(
                data_dir,
                "configure",
                "--min-review-interval-days",
                "7",
            )
            configured = self._status(data_dir)
            self.assertIn(7, _values_for_key(configured, "min_review_interval_days"))
            after_configure = _find_topic_record(configured, topic_id)
            self.assertEqual(
                after_configure["mastery"]["recognition"]["next_review_at"],
                "2026-08-11",
            )

    def test_configured_review_floor_only_applies_to_pass_ready_maintenance(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "configure",
                "--min-review-interval-days",
                "21",
            )
            for number in range(6):
                day = 10 if number < 5 else 12
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"pass-ready-floor-{number}",
                    "--item-id",
                    f"pass-ready-item-{number}",
                    "--at",
                    f"2026-08-{day:02d}T09:{number:02d}:00+08:00",
                )
            status = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(status["mastery"]["recognition"]["status"], "pass_ready")
            self.assertEqual(
                status["mastery"]["recognition"]["next_review_at"],
                "2026-08-13",
                "达到 pass_ready 不能把已经临近的复习日推迟",
            )
            for suffix, at in (
                ("three-day", "2026-08-13T09:00:00+08:00"),
                ("seven-day", "2026-08-16T09:00:00+08:00"),
                ("maintenance", "2026-08-23T09:00:00+08:00"),
            ):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"pass-ready-floor-{suffix}",
                    "--item-id",
                    f"pass-ready-item-{suffix}",
                    "--at",
                    at,
                )
            maintenance = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(
                maintenance["mastery"]["recognition"]["next_review_at"],
                "2026-09-13",
                "进入 14 天维护阶段后才应用 21 天最小间隔",
            )

    def test_every_command_refuses_a_copied_unignored_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as source_temporary:
            source = Path(source_temporary)
            self._init(source)
            with tempfile.TemporaryDirectory(dir=REPO_ROOT) as unsafe_temporary:
                unsafe = Path(unsafe_temporary)
                for source_file in source.iterdir():
                    if source_file.is_file() and source_file.name != ".tutor.lock":
                        (unsafe / source_file.name).write_bytes(source_file.read_bytes())
                before = _snapshot_files(unsafe)
                for arguments in (
                    ("status", "--json"),
                    (
                        "record",
                        "--topic",
                        self._recognition_topic()["id"],
                        "--skill",
                        "recognition",
                        "--score",
                        "1",
                        "--max-score",
                        "1",
                        "--attempt-id",
                        "unsafe-write",
                    ),
                ):
                    rejected = _run_cli(
                        unsafe, *arguments, expected_returncode=None
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("Git", rejected.stderr)
                self.assertEqual(before, _snapshot_files(unsafe))
                self.assertFalse((unsafe / ".tutor.lock").exists())

    def test_record_keeps_right_and_wrong_evidence_and_is_idempotent(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "wrong-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
                "--wrong-reason",
                "knowledge_gap",
                "--source",
                "exam-bank",
            )
            after_wrong = _snapshot_files(data_dir)
            self.assertTrue(after_wrong)

            # Replaying an external event must be a no-op, not a second attempt.
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "wrong-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
                "--wrong-reason",
                "knowledge_gap",
                "--source",
                "exam-bank",
            )
            self.assertEqual(after_wrong, _snapshot_files(data_dir))

            conflict = _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "wrong-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
                "--source",
                "exam-bank",
                expected_returncode=None,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("冲突", conflict.stderr)
            self.assertEqual(after_wrong, _snapshot_files(data_dir))

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "right-001",
                "--at",
                "2026-08-10T09:10:00+08:00",
                "--source",
                "exam-bank",
            )
            status = self._status(data_dir)
            topic_record = _find_topic_record(status, topic_id)
            serialized = json.dumps(topic_record, ensure_ascii=False)
            self.assertIn("knowledge_gap", serialized)

            evidence_counts = [
                value
                for key in ("attempt_count", "attempts", "evidence_count")
                for value in _values_for_key(topic_record, key)
                if isinstance(value, (int, float))
            ]
            self.assertTrue(evidence_counts, "status must expose an evidence count")
            self.assertGreaterEqual(max(evidence_counts), 2)

    def test_one_correct_answer_is_not_mastery(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "single-lucky-answer",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )

            label = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertNotIn(label, PASS_READY_STATES)

    def test_record_requires_a_stable_item_id(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(TUTOR_SCRIPT),
                    "--data-dir",
                    str(data_dir),
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    "missing-item-id",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("item-id", rejected.stderr)
            self.assertEqual(self._status(data_dir)["topics"], {})

    def test_registered_question_supplies_concept_metadata_and_rejects_duplicate_content(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            registration = data_dir / "question.json"
            registration.write_text(
                json.dumps(
                    {
                        "item_id": "self-authored/registered-001",
                        "topic_id": topic_id,
                        "concept_id": f"{topic_id}.registered",
                        "question_family_id": f"{topic_id}.registered.variant",
                        "stem": "测试登记题",
                        "options": ["选项乙", "选项甲"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _run_cli(data_dir, "register-question", "--file", str(registration))
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "registered-attempt",
                "--item-id",
                "self-authored/registered-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            event = json.loads(
                (data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["concept_id"], f"{topic_id}.registered")
            self.assertEqual(
                event["question_family_id"], f"{topic_id}.registered.variant"
            )
            self.assertEqual(len(event["question_fingerprint"]), 64)

            duplicate = data_dir / "duplicate.json"
            duplicate.write_text(
                json.dumps(
                    {
                        "item_id": "self-authored/registered-duplicate",
                        "topic_id": topic_id,
                        "concept_id": f"{topic_id}.registered",
                        "question_family_id": f"{topic_id}.registered.variant",
                        "stem": "测试登记题",
                        "options": ["选项甲", "选项乙"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rejected = _run_cli(
                data_dir,
                "register-question",
                "--file",
                str(duplicate),
                expected_returncode=None,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("不能换 ID 重复计证据", rejected.stderr)

            same_family = data_dir / "same-family.json"
            same_family.write_text(
                json.dumps(
                    {
                        "item_id": "self-authored/registered-variant-002",
                        "topic_id": topic_id,
                        "concept_id": f"{topic_id}.registered",
                        "question_family_id": f"{topic_id}.registered.variant",
                        "variant_of": "self-authored/registered-001",
                        "stem": "同一考法的另一种题干",
                        "options": ["选项丙", "选项丁"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _run_cli(data_dir, "register-question", "--file", str(same_family))
            # Same-family same-day answers remain valid evidence: the cooldown
            # is a scheduling rule applied when recommendations are built,
            # never a reason to refuse recording an attempt.
            cooled = _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "same-family-same-day",
                "--item-id",
                "self-authored/registered-variant-002",
                "--at",
                "2026-08-10T10:00:00+08:00",
            )
            self.assertEqual(cooled.returncode, 0)
            recorded = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            same_day = next(
                event
                for event in recorded
                if event["attempt_id"] == "same-family-same-day"
            )
            self.assertEqual(
                same_day["question_family_id"], f"{topic_id}.registered.variant"
            )

    def test_quiz_prepare_hides_answers_and_persists_private_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                    "--today",
                    "2026-09-17",
                )
            )

            self.assertEqual(payload["count"], 5)
            self.assertEqual(len(payload["questions"]), 5)
            for question in payload["questions"]:
                self.assertNotIn("correct", question)
                self.assertNotIn("explanation", question)
                self.assertEqual(
                    [option["label"] for option in question["options"]],
                    sorted(option["label"] for option in question["options"]),
                )

            manifest_path = (
                data_dir / "quiz-sessions" / f"{payload['quiz_id']}.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "pending")
            self.assertEqual(len(manifest["questions"]), 5)
            self.assertTrue(all(question["correct"] for question in manifest["questions"]))
            self.assertGreater(
                len({"".join(question["correct"]) for question in manifest["questions"]}),
                1,
                "the prepared set should not expose a trivial single-answer pattern",
            )

    def test_case_prepare_uses_adaptive_route_and_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            before = _snapshot_files(data_dir)

            payload = _json_output(
                _run_cli(
                    data_dir,
                    "case-prepare",
                    "--today",
                    "2026-09-21",
                )
            )

            self.assertEqual(before, _snapshot_files(data_dir))
            self.assertEqual("case_start", payload["route_lock"]["mode"])
            self.assertEqual("case", payload["route_lock"]["subject"])
            self.assertEqual("blind", payload["item"]["practice_mode"])
            self.assertTrue(payload["item"]["figures_complete"])
            self.assertNotIn("answer", payload["item"])
            self.assertNotIn("参考答案", payload["item"]["stem"])
            for asset in payload["item"]["figure_assets"]:
                self.assertTrue(Path(asset).is_file())

            topic = next(
                item
                for item in self.topics
                if item["id"] == payload["selected_topic"]["topic_id"]
            )
            case_resource = next(
                resource
                for resource in topic["resources"]
                if resource.startswith("past-papers/case-types/")
            )
            expected_type = re.search(r"/(\d{2})-", case_resource).group(1)
            self.assertEqual(f"案例 {expected_type}", payload["case_type"])

    def test_progress_is_read_only_and_returns_three_subject_overview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "configure",
                "--subject-policy",
                "essay=manual_trigger",
                "--subject-policy-reason",
                "考生主动要求",
            )
            before = _snapshot_files(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "progress",
                    "--json",
                    "--limit",
                    "5",
                    "--today",
                    "2026-09-22",
                )
            )
            self.assertEqual(before, _snapshot_files(data_dir))
            self.assertEqual(set(payload["subjects"]), SUBJECTS)
            self.assertLessEqual(len(payload["weakpoints"]), 5)
            self.assertLessEqual(len(payload["due_reviews"]), 5)
            self.assertIn("next_action", payload)
            self.assertEqual(payload["subject_allocation"]["essay"], 0.0)
            self.assertAlmostEqual(
                sum(payload["subject_allocation"].values()), 1.0, places=3
            )
            self.assertEqual(
                [item["subject"] for item in payload["suppressed_subjects"]],
                ["essay"],
            )

    def test_configured_case_route_cannot_be_bypassed_by_k_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(data_dir, "configure", "--case-track", "C02.CASE_DATABASE")
            payload = _json_output(
                _run_cli(data_dir, "case-prepare", "--today", "2026-09-22")
            )
            self.assertEqual("C02.CASE_DATABASE", payload["route_lock"]["track_id"])
            self.assertEqual("C02.CASE_DATABASE", payload["route_lock"]["topic_id"])
            self.assertEqual("案例 02", payload["case_type"])
            self.assertEqual(
                ["K10.DATABASE_MODELING"],
                payload["route_lock"]["supporting_topic_ids"],
            )

    def test_case_prepare_explicit_topic_maps_through_curriculum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "case-prepare",
                    "--topic",
                    "K25.RELIABILITY_ENGINEERING",
                    "--today",
                    "2026-09-21",
                )
            )
            self.assertEqual("K25.RELIABILITY_ENGINEERING", payload["route_lock"]["topic_id"])
            self.assertIsNone(payload["route_lock"]["track_id"])
            self.assertEqual("案例 13", payload["case_type"])
            self.assertEqual("case", payload["record"]["subject"])
            self.assertEqual("application", payload["record"]["skill"])

    def test_quiz_grade_records_one_atomic_idempotent_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--limit",
                    "5",
                    "--today",
                    "2026-09-17",
                )
            )
            manifest_path = (
                data_dir / "quiz-sessions" / f"{prepared['quiz_id']}.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            answers = ",".join(
                "".join(question["correct"])
                for question in manifest["questions"]
            )
            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    answers,
                    "--at",
                    "2026-09-17T21:30:00+08:00",
                )
            )
            self.assertEqual(graded["score"], 5)
            self.assertEqual(graded["max_score"], 5)
            self.assertEqual(graded["recorded_attempts"], 5)
            self.assertFalse(graded["idempotent"])
            self.assertEqual(
                len((data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()),
                5,
            )

            after_first_grade = _snapshot_files(data_dir)
            replayed = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    answers,
                    "--at",
                    "2026-09-17T21:30:00+08:00",
                )
            )
            self.assertEqual(replayed["score"], graded["score"])
            self.assertEqual(replayed["results"], graded["results"])
            self.assertEqual(replayed["recorded_attempts"], 0)
            self.assertTrue(replayed["idempotent"])
            self.assertEqual(after_first_grade, _snapshot_files(data_dir))

    def test_quiz_grade_rejects_incomplete_answers_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--limit",
                    "5",
                    "--today",
                    "2026-09-17",
                )
            )
            before = _snapshot_files(data_dir)
            rejected = _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                prepared["quiz_id"],
                "--answers",
                "A",
                expected_returncode=None,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("答案数量", rejected.stderr)
            self.assertEqual(before, _snapshot_files(data_dir))

    def test_recognition_mastery_requires_enough_cross_day_evidence(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)

            for number in range(5):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"same-day-{number}",
                    "--at",
                    f"2026-08-10T09:{number:02d}:00+08:00",
                )

            same_day_label = _status_label(
                _find_topic_record(self._status(data_dir), topic_id)
            )
            self.assertNotIn(same_day_label, PASS_READY_STATES)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "cross-day-006",
                "--at",
                "2026-08-12T09:00:00+08:00",
            )
            cross_day_label = _status_label(
                _find_topic_record(self._status(data_dir), topic_id)
            )
            self.assertIn(cross_day_label, PASS_READY_STATES)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "new-variant-failed",
                "--at",
                "2026-08-13T09:00:00+08:00",
                "--wrong-reason",
                "concept_confusion",
            )
            regressed = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(regressed["mastery"]["recognition"]["status"], "fragile")
            self.assertEqual(
                regressed["mastery"]["recognition"]["next_review_at"],
                "2026-08-14",
            )
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "one-minute-quick-fix",
                "--at",
                "2026-08-13T09:01:00+08:00",
            )
            still_fragile = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(
                still_fragile["mastery"]["recognition"]["status"], "fragile"
            )
            self.assertEqual(
                still_fragile["mastery"]["recognition"]["next_review_at"],
                "2026-08-14",
            )

    def test_repeating_one_item_never_counts_as_independent_mastery(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for number in range(6):
                day = 10 if number < 3 else 12
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"same-item-attempt-{number}",
                    "--item-id",
                    "one-question-only",
                    "--at",
                    f"2026-08-{day:02d}T09:{number:02d}:00+08:00",
                )
            label = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertNotIn(label, PASS_READY_STATES)

    def test_aggregate_topic_requires_coverage_of_declared_facets(self) -> None:
        topic_id = "K05.TEST_CMMI_PATTERNS"
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for number in range(6):
                day = 10 if number < 5 else 12
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--facet",
                    "cmmi",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"cmmi-only-{number}",
                    "--item-id",
                    f"cmmi-item-{number}",
                    "--at",
                    f"2026-08-{day:02d}T09:{number:02d}:00+08:00",
                )
            cmmi_only = _find_topic_record(self._status(data_dir), topic_id)
            self.assertNotIn(
                cmmi_only["mastery"]["recognition"]["status"], PASS_READY_STATES
            )

            for facet, day in (("testing", 13), ("design_patterns", 14)):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--facet",
                    facet,
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"facet-{facet}",
                    "--item-id",
                    f"facet-item-{facet}",
                    "--at",
                    f"2026-08-{day:02d}T09:00:00+08:00",
                )
            covered = _find_topic_record(self._status(data_dir), topic_id)
            self.assertIn(
                covered["mastery"]["recognition"]["status"], PASS_READY_STATES
            )

    def test_cross_subject_topic_exposes_unmeasured_dimensions(self) -> None:
        topic = next(
            item
            for item in self.topics
            if {"recognition", "application", "production"}.issubset(
                item.get("skills", [])
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for number in range(6):
                day = 10 if number < 5 else 12
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic["id"],
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"only-recognition-{number}",
                    "--item-id",
                    f"recognition-item-{number}",
                    "--at",
                    f"2026-08-{day:02d}T09:{number:02d}:00+08:00",
                )
            topic_record = _find_topic_record(self._status(data_dir), topic["id"])
            self.assertEqual(topic_record["mastery"]["recognition"]["status"], "pass_ready")
            self.assertNotIn(_status_label(topic_record), PASS_READY_STATES)
            self.assertNotIn("application", topic_record["mastery"])
            self.assertNotIn("production", topic_record["mastery"])

    def test_case_mastery_requires_two_independent_items_48_hours_apart(self) -> None:
        topic_id = "C01.CASE_ATAM"
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for attempt_id, item_id, at in (
                ("case-early-1", "case-item-1", "2026-08-10T23:30:00+08:00"),
                ("case-early-2", "case-item-2", "2026-08-12T00:30:00+08:00"),
            ):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "application",
                    "--score",
                    "15",
                    "--max-score",
                    "25",
                    "--attempt-id",
                    attempt_id,
                    "--item-id",
                    item_id,
                    "--at",
                    at,
                )
            early = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertNotIn(early, PASS_READY_STATES)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "application",
                "--score",
                "15",
                "--max-score",
                "25",
                "--attempt-id",
                "case-after-48h",
                "--item-id",
                "case-item-3",
                "--at",
                "2026-08-12T23:31:00+08:00",
            )
            ready = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertIn(ready, PASS_READY_STATES)

    def test_case_spacing_must_be_between_distinct_items(self) -> None:
        topic_id = "C01.CASE_ATAM"
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for attempt_id, item_id, at in (
                ("case-a-first", "case-a", "2026-08-10T09:00:00+08:00"),
                ("case-b-middle", "case-b", "2026-08-11T09:00:00+08:00"),
                ("case-a-repeat", "case-a", "2026-08-12T09:00:00+08:00"),
            ):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "application",
                    "--score",
                    "15",
                    "--max-score",
                    "25",
                    "--attempt-id",
                    attempt_id,
                    "--item-id",
                    item_id,
                    "--at",
                    at,
                )
            label = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertNotIn(label, PASS_READY_STATES)

    def test_essay_requires_a_full_timed_passing_essay_itself(self) -> None:
        topic_id = "P01.ESSAY_ARCHITECTURE"
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for attempt_id, item_id, score, maximum, mode in (
                ("great-outline", "outline-1", 10, 10, "practice"),
                ("failed-full-essay", "essay-1", 40, 75, "full_timed"),
            ):
                full_essay_evidence = (
                    ("--duration-seconds", "7200", "--word-count", "2600", "--complete")
                    if mode == "full_timed"
                    else ()
                )
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "production",
                    "--score",
                    str(score),
                    "--max-score",
                    str(maximum),
                    "--attempt-id",
                    attempt_id,
                    "--item-id",
                    item_id,
                    "--mode",
                    mode,
                    "--at",
                    "2026-08-10T10:00:00+08:00",
                    *full_essay_evidence,
                )
            failed = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertNotIn(failed, PASS_READY_STATES)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "production",
                "--score",
                "52",
                "--max-score",
                "75",
                "--attempt-id",
                "passing-full-essay",
                "--item-id",
                "essay-2",
                "--mode",
                "full_timed",
                "--duration-seconds",
                "7200",
                "--word-count",
                "2700",
                "--complete",
                "--at",
                "2026-08-12T10:00:00+08:00",
            )
            ready = _status_label(_find_topic_record(self._status(data_dir), topic_id))
            self.assertIn(ready, PASS_READY_STATES)

            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "production",
                "--score",
                "10",
                "--max-score",
                "10",
                "--attempt-id",
                "outline-after-passing-essay",
                "--item-id",
                "outline-after-pass",
                "--mode",
                "practice",
                "--at",
                "2026-08-13T10:00:00+08:00",
            )
            after_outline = _find_topic_record(self._status(data_dir), topic_id)
            self.assertIn(_status_label(after_outline), PASS_READY_STATES)
            self.assertEqual(
                after_outline["mastery"]["production"]["next_review_at"],
                "2026-08-15",
            )

    def test_cold_start_rotates_diagnostics_across_all_three_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)

            def target() -> str:
                payload = _json_output(
                    _run_cli(
                        data_dir,
                        "recommend",
                        "--json",
                        "--limit",
                        "3",
                        "--today",
                        "2026-08-10",
                    )
                )
                return payload["target_subject"]

            self.assertEqual(target(), "comprehensive")
            _run_cli(
                data_dir,
                "record",
                "--topic",
                "K22.ENGLISH_READING",
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "cold-comprehensive",
                "--item-id",
                "cold-comprehensive-item",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            self.assertEqual(target(), "case")
            _run_cli(
                data_dir,
                "record",
                "--topic",
                "C01.CASE_ATAM",
                "--skill",
                "application",
                "--score",
                "0",
                "--max-score",
                "25",
                "--attempt-id",
                "cold-case",
                "--item-id",
                "cold-case-item",
                "--at",
                "2026-08-10T09:10:00+08:00",
            )
            self.assertEqual(target(), "essay")

    def test_first_comprehensive_diagnostic_starts_with_declared_core_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                    "--today",
                    "2026-08-10",
                )
            )
            first_group = set(
                self.curriculum["strategy"]["comprehensive_cold_start_groups"][0]
            )
            recommended = {
                _recommendation_topic_id(item)
                for item in _recommendation_items(payload)
            }
            self.assertTrue(recommended.issubset(first_group))

    def test_last_three_days_only_recommends_existing_or_survival_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "8",
                    "--today",
                    "2026-11-05",
                )
            )
            self.assertTrue(payload["crunch_mode"])
            self.assertEqual(payload["days_to_exam"], 2)
            items = _recommendation_items(payload)
            self.assertTrue(items)
            for item in items:
                self.assertTrue(
                    any(
                        "SURVIVAL.md" in resource
                        or resource.startswith("cheatsheets/")
                        for resource in item["resources"]
                    ),
                    item,
                )

    def test_weak_mock_subject_gets_non_equal_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for subject, score, minute in (
                ("comprehensive", 75, 0),
                ("case", 44, 1),
                ("essay", 75, 2),
            ):
                _run_cli(
                    data_dir,
                    "mock",
                    "--subject",
                    subject,
                    "--mock-id",
                    f"weak-subject-{subject}",
                    "--paper-id",
                    f"fixture-{subject}-001",
                    "--score",
                    str(score),
                    "--max-score",
                    "75",
                    "--duration-minutes",
                    "90",
                    "--complete",
                    "--at",
                    f"2026-08-10T10:0{minute}:00+08:00",
                )

            status = self._status(data_dir)
            status_text = json.dumps(status, ensure_ascii=False)
            self.assertIn("44", status_text)
            for subject in SUBJECTS:
                self.assertIn(subject, status_text)

            recommendation = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--limit",
                    "12",
                    "--today",
                    "2026-08-10",
                )
            )
            allocation = _find_subject_allocation(recommendation)
            self.assertGreater(allocation["case"], allocation["comprehensive"])
            self.assertGreater(allocation["case"], allocation["essay"])
            self.assertGreater(len(set(allocation.values())), 1, "subjects were averaged")

            items = _recommendation_items(recommendation)
            self.assertTrue(items)
            first = items[0]
            first_subject = first.get("subject")
            if first_subject is None:
                topic_id = _recommendation_topic_id(first)
                topic = next(topic for topic in self.topics if topic["id"] == topic_id)
                self.assertIn("case", topic["subjects"])
            else:
                self.assertEqual(first_subject, "case")

    def test_danger_subject_remains_primary_while_overdue_safe_subject_is_maintained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for subject, score, at in (
                ("case", 70, "2026-06-30T10:00:00+08:00"),
                ("essay", 70, "2026-08-09T10:00:00+08:00"),
                ("comprehensive", 44, "2026-08-10T10:00:00+08:00"),
            ):
                _run_cli(
                    data_dir,
                    "mock",
                    "--subject",
                    subject,
                    "--mock-id",
                    f"hard-gate-{subject}",
                    "--paper-id",
                    f"hard-gate-paper-{subject}",
                    "--score",
                    str(score),
                    "--max-score",
                    "75",
                    "--duration-minutes",
                    "90",
                    "--complete",
                    "--at",
                    at,
                )
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--limit",
                    "4",
                    "--today",
                    "2026-08-10",
                )
            )
            self.assertEqual(payload["target_subject"], "comprehensive")
            self.assertEqual(payload["maintenance_subject"], "case")
            self.assertGreater(payload["subject_allocation"]["comprehensive"], 0.5)
            self.assertGreater(
                payload["subject_allocation"]["comprehensive"],
                payload["subject_allocation"]["case"],
            )
            items = _recommendation_items(payload)
            self.assertEqual(items[0]["subject"], "comprehensive")
            self.assertTrue(any(item["subject"] == "case" for item in items[1:]))

    def test_diagnosis_merges_same_topic_mock_gaps_into_one_concept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _append_mock_gap_session(
                data_dir,
                "merge-mock-001",
                wrong_items=[
                    ("K08.SOFTWARE_PROCESS_MODELS", "exam-bank/07-software-engineering.md#1", None),
                    ("K08.SOFTWARE_PROCESS_MODELS", "exam-bank/07-software-engineering.md#2", None),
                    ("K12.PATTERNS_SOA_MICROSERVICES", "exam-bank/13-design-patterns.md#1", "design_patterns"),
                    ("K12.PATTERNS_SOA_MICROSERVICES", "exam-bank/15-microservice-cloud-native.md#1", "microservices"),
                ],
            )
            payload = _json_output(
                _run_cli(data_dir, "diagnose", "--subject", "comprehensive", "--json")
            )
            issues = {issue["concept_id"]: issue for issue in payload["issues"]}
            # Same-topic questions share one stable mergeable concept instead
            # of a unique synthetic concept per question; declared facets keep
            # their concepts apart.
            self.assertEqual(
                sorted(issues),
                [
                    "K08.SOFTWARE_PROCESS_MODELS",
                    "K12.PATTERNS_SOA_MICROSERVICES:design_patterns",
                    "K12.soa_microservices_governance",
                ],
            )
            self.assertEqual(
                sorted(issues["K08.SOFTWARE_PROCESS_MODELS"]["source_item_ids"]),
                [
                    "exam-bank/07-software-engineering.md#1",
                    "exam-bank/07-software-engineering.md#2",
                ],
            )
            # The study-item label is human-readable, not a machine id or a
            # "(N)" header fragment from the source bank.
            k08_name = next(
                topic["name"]
                for topic in self.topics
                if topic["id"] == "K08.SOFTWARE_PROCESS_MODELS"
            )
            self.assertEqual(
                issues["K08.SOFTWARE_PROCESS_MODELS"]["concept_label"], k08_name
            )
            for issue in issues.values():
                self.assertNotIn(":", issue["concept_label"])
                self.assertNotRegex(issue["concept_label"], r"^\(\d+\)$")

    def test_weakpoints_ranks_overdue_and_recent_topics_readonly(self) -> None:
        """The read-only weakpoint ranking must stay side-effect free."""

        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "weak-001",
                "--item-id",
                "test-item:weak-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
                "--wrong-reason",
                "knowledge_gap",
            )
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "weak-002",
                "--item-id",
                "test-item:weak-002",
                "--at",
                "2026-08-18T09:00:00+08:00",
                "--confidence",
                "guess",
            )

            before = _snapshot_files(data_dir)
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "weakpoints",
                    "--subject",
                    "comprehensive",
                    "--days",
                    "30",
                    "--today",
                    "2026-08-20",
                    "--json",
                )
            )
            self.assertEqual(
                before,
                _snapshot_files(data_dir),
                "weakpoints must stay read-only for learner data",
            )
            self.assertEqual("comprehensive", payload["subject"])
            self.assertEqual("2026-08-20", payload["today"])

            due = [row for row in payload["due"] if row["topic_id"] == topic_id]
            self.assertTrue(due, "an unanswered past-due topic must be ranked as due")
            self.assertLess(due[0]["next_review_at"], payload["today"])
            self.assertGreaterEqual(due[0]["overdue_days"], 1)

            recent = [row for row in payload["recent"] if row["topic_id"] == topic_id]
            self.assertTrue(recent, "recent answers inside the window must be ranked")
            self.assertEqual(2, recent[0]["recent_attempts"])
            self.assertEqual(0.5, recent[0]["recent_accuracy"])
            self.assertEqual(1, recent[0]["guess_correct"])
            self.assertNotIn(
                topic_id,
                [row["topic_id"] for row in payload["uncovered"]],
                "a practised topic must not be listed as uncovered",
            )
            for key in (
                "recent_accuracy",
                "recent_attempts",
                "guess_correct",
                "last_attempt_at",
                "overdue_days",
                "mastery",
                "action",
            ):
                self.assertIn(key, recent[0])

            # A shorter window keeps the ranking but drops the stale attempt.
            narrow = _json_output(
                _run_cli(
                    data_dir,
                    "weakpoints",
                    "--subject",
                    "comprehensive",
                    "--days",
                    "5",
                    "--today",
                    "2026-08-20",
                    "--json",
                )
            )
            narrow_recent = [
                row for row in narrow["recent"] if row["topic_id"] == topic_id
            ]
            self.assertEqual(1, narrow_recent[0]["recent_attempts"])
            self.assertEqual(1.0, narrow_recent[0]["recent_accuracy"])

    def _prepare_quiz(self, data_dir: Path, limit: int = 5) -> dict[str, Any]:
        return _json_output(
            _run_cli(
                data_dir,
                "quiz-prepare",
                "--subject",
                "comprehensive",
                "--limit",
                str(limit),
            )
        )

    def _quiz_manifest(self, data_dir: Path, quiz_id: str) -> dict[str, Any]:
        path = data_dir / "quiz-sessions" / f"{quiz_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _manifest_item_ids(
        self, data_dir: Path, quiz_id: str
    ) -> set[str]:
        return {
            question["item_id"]
            for question in self._quiz_manifest(data_dir, quiz_id)["questions"]
        }

    def _manifest_concept_ids(
        self, data_dir: Path, quiz_id: str
    ) -> set[str]:
        return {
            question["concept_id"]
            for question in self._quiz_manifest(data_dir, quiz_id)["questions"]
        }

    def _grade_wrong_answer_variant(
        self,
        data_dir: Path,
        concept_id: str = "K16.REQUIREMENTS_MANAGEMENT",
        at: str | None = None,
    ) -> dict[str, Any] | None:
        """Prepare one question pinned to ``concept_id`` and answer it wrong.

        Pinning the fine concept keeps the follow-up deterministic: the picker
        only ever offers a variant on that same concept.
        """

        prepared = self._prepare_quiz(data_dir, limit=1)
        manifest_path = data_dir / "quiz-sessions" / f"{prepared['quiz_id']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        question = manifest["questions"][0]
        question["topic_id"] = concept_id
        question["concept_id"] = concept_id
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        correct = "".join(question["correct"])
        wrong = next(letter for letter in "ABCD" if letter != correct)
        arguments = [
            "quiz-grade",
            "--quiz-id",
            prepared["quiz_id"],
            "--answers",
            wrong,
        ]
        if at is not None:
            arguments += ["--at", at]
        graded = _json_output(_run_cli(data_dir, *arguments))
        self.assertFalse(graded["results"][0]["is_correct"])
        return graded["results"][0]["variant_question"]

    def _grade_quiz_with_wrong_answers(
        self, data_dir: Path
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Grade a whole group with all-A answers and return the variants served."""

        prepared = self._prepare_quiz(data_dir)
        graded = _json_output(
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                prepared["quiz_id"],
                "--answers",
                "A,A,A,A,A",
            )
        )
        variants = [
            result["variant_question"]
            for result in graded["results"]
            if result.get("variant_question")
        ]
        self.assertTrue(variants, "全 A 作答必须至少产生一道变式题")
        return graded, variants

    def test_quiz_grade_treats_explicit_x_as_conceded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir)
            quiz_id = prepared["quiz_id"]

            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    quiz_id,
                    "--answers",
                    "X,X,X,X,X",
                    "--confidences",
                    "sure,sure,sure,sure,sure",
                )
            )
            self.assertEqual(0, graded["score"])
            self.assertEqual(5, graded["conceded_count"])
            first = graded["results"][0]
            self.assertEqual("conceded", first["response_state"])
            self.assertIsNone(first["selected"])
            self.assertEqual(["knowledge_gap"], first["wrong_reasons"])

            attempts = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            conceded = [event for event in attempts if event.get("response_state") == "conceded"]
            self.assertEqual(5, len(conceded))
            self.assertIsNone(conceded[0]["confidence"])
            self.assertIsNone(conceded[0]["selected_answer"])
            self.assertEqual(0, conceded[0]["score"])
            self.assertEqual(["knowledge_gap"], conceded[0]["wrong_reasons"])
            self.assertEqual("response_state", conceded[0]["wrong_reason_source"])

            # Replaying the same explicit "不会" stays idempotent, a different
            # answer set is rejected, and X may not be mixed with option letters.
            replay = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    quiz_id,
                    "--answers",
                    "X,X,X,X,X",
                    "--confidences",
                    "sure,sure,sure,sure,sure",
                )
            )
            self.assertTrue(replay["idempotent"])
            self.assertEqual(0, replay["recorded_attempts"])
            self.assertEqual(
                len(attempts),
                len(
                    [
                        line
                        for line in (data_dir / "attempts.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line.strip()
                    ]
                ),
            )
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                quiz_id,
                "--answers",
                "A,A,A,A,A",
                expected_returncode=2,
            )
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                quiz_id,
                "--answers",
                "AX,X,X,X,X",
                expected_returncode=2,
            )

    def test_quiz_grade_only_records_explicit_wrong_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir, limit=2)
            manifest = self._quiz_manifest(data_dir, prepared["quiz_id"])
            wrong_answers = [
                next(letter for letter in "ABCD" if letter not in question["correct"])
                for question in manifest["questions"]
            ]
            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    ",".join(wrong_answers),
                    "--wrong-reason",
                    "1=recall_failure",
                )
            )
            first, second = graded["results"]
            self.assertEqual(["recall_failure"], first["wrong_reasons"])
            self.assertEqual("learner", first["wrong_reason_source"])
            self.assertEqual("confirmed", first["wrong_reason_status"])
            self.assertEqual([], second["wrong_reasons"])
            self.assertIsNone(second["wrong_reason_source"])
            self.assertEqual("unclassified", second["wrong_reason_status"])

            attempts = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual("learner", attempts[-2]["wrong_reason_source"])
            self.assertEqual([], attempts[-1]["wrong_reasons"])

    def test_review_schedule_advances_one_three_seven_fourteen_thirty_days(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)

            def record(number: int, day: str, score: int) -> str:
                arguments = [
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    str(score),
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"review-ladder-{number}",
                    "--item-id",
                    f"review-ladder-item-{number}",
                    "--mode",
                    "review",
                    "--at",
                    f"{day}T09:00:00+08:00",
                ]
                if score == 0:
                    arguments.extend(["--wrong-reason", "recall_failure"])
                _run_cli(data_dir, *arguments)
                status = _find_topic_record(self._status(data_dir), topic_id)
                return status["mastery"]["recognition"]["next_review_at"]

            self.assertEqual("2026-08-11", record(1, "2026-08-10", 0))
            self.assertEqual(
                "2026-08-11",
                record(2, "2026-08-10", 1),
                "当天纠偏不得取消次日复测",
            )
            self.assertEqual("2026-08-14", record(3, "2026-08-11", 1))
            self.assertEqual("2026-08-21", record(4, "2026-08-14", 1))
            self.assertEqual("2026-09-04", record(5, "2026-08-21", 1))
            self.assertEqual("2026-10-04", record(6, "2026-09-04", 1))

    def test_quiz_grade_invalidates_broken_questions_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir)
            quiz_id = prepared["quiz_id"]

            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    quiz_id,
                    "--answers",
                    "A,A,X,A,A",
                    "--invalidate",
                    "3=missing_required_table",
                    "--audit",
                    "1=答案键疑似有误",
                )
            )
            self.assertEqual(1, graded["invalidated_count"])
            self.assertEqual(4, graded["counted_questions"])
            self.assertEqual(4, graded["max_score"])
            invalidated = graded["results"][2]
            self.assertEqual("invalidated", invalidated["response_state"])
            self.assertFalse(invalidated["counted"])
            self.assertIsNone(invalidated["selected"])
            self.assertIsNone(invalidated["correct"])
            self.assertEqual(
                "题目无效，本题不计分", invalidated["message"]
            )
            self.assertTrue(graded["results"][0]["needs_audit"])
            queue_lines = [
                json.loads(line)
                for line in (data_dir / "quiz-audit-queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(queue_lines))
            self.assertEqual("open", queue_lines[0]["status"])

            attempts = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(4, len(attempts), "an invalidated question writes no attempt")
            self.assertFalse(
                any(event["attempt_id"].endswith("-q-3") for event in attempts),
                "the invalidated question must not be recorded",
            )

            # Simulate a crash after the evidence/audit writes but before the
            # completed manifest replacement. Recovery must not duplicate the
            # maintenance queue entry.
            manifest_path = data_dir / "quiz-sessions" / f"{quiz_id}.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "pending"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            recovered = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    quiz_id,
                    "--answers",
                    "A,A,X,A,A",
                    "--invalidate",
                    "3=missing_required_table",
                    "--audit",
                    "1=答案键疑似有误",
                )
            )
            self.assertTrue(recovered["idempotent"])
            queue_lines = [
                line
                for line in (data_dir / "quiz-audit-queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(queue_lines))
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                quiz_id,
                "--answers",
                "A,A,X,A,A",
                "--invalidate",
                "3=bogus_reason",
                expected_returncode=2,
            )

    def test_quiz_grade_normalizes_common_invalidation_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir)
            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    "A,A,X,A,A",
                    "--invalidate",
                    "3=incomplete_stem",
                )
            )
            self.assertEqual("unclear_stem", graded["results"][2]["invalid_reason"])
            self.assertEqual("await_variants", graded["next_action"]["mode"])
            attempts = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertFalse(any(event["attempt_id"].endswith("-q-3") for event in attempts))

    def test_variant_question_does_not_fall_back_to_another_concept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir, limit=1)
            manifest_path = (
                data_dir / "quiz-sessions" / f"{prepared['quiz_id']}.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["questions"][0]["concept_id"] = "test.no_matching_concept"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            correct = "".join(manifest["questions"][0]["correct"])
            wrong = next(letter for letter in "ABCD" if letter != correct)
            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    wrong,
                )
            )
            self.assertFalse(graded["results"][0]["is_correct"])
            self.assertIsNone(
                graded["results"][0]["variant_question"],
                "a different concept in the same topic is not a valid variant",
            )

    def test_variant_question_does_not_repeat_within_the_cooldown_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            first = self._grade_wrong_answer_variant(data_dir)
            self.assertIsNotNone(
                first, "a wrong answer on a mapped concept must offer a variant"
            )
            second = self._grade_wrong_answer_variant(data_dir)
            self.assertIsNotNone(second)
            self.assertEqual(first["topic_id"], second["topic_id"])
            self.assertNotEqual(
                first["item_id"],
                second["item_id"],
                "变式题只在对话里口头作答、不写 attempts，"
                "因此必须靠会话清单把它排除出冷却窗口",
            )

    def test_variant_question_cooldown_reads_older_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            three_days_ago = (
                datetime.now().astimezone() - timedelta(days=3)
            ).isoformat(timespec="seconds")
            first = self._grade_wrong_answer_variant(data_dir, at=three_days_ago)
            self.assertIsNotNone(first)
            second = self._grade_wrong_answer_variant(data_dir)
            self.assertIsNotNone(second)
            self.assertNotEqual(
                first["item_id"],
                second["item_id"],
                "冷却窗口要覆盖前几天已经出过的变式题，而不只是当天",
            )

    def test_quiz_prepare_does_not_re_serve_a_banked_item_on_a_later_day(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            first = self._prepare_quiz(data_dir)
            manifest = self._quiz_manifest(data_dir, first["quiz_id"])
            answers = ",".join(
                "".join(question["correct"]) for question in manifest["questions"]
            )
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                first["quiz_id"],
                "--answers",
                answers,
                "--confidences",
                "sure,sure,sure,sure,sure",
            )
            later = (
                datetime.now().astimezone() + timedelta(days=3)
            ).date().isoformat()
            second = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                    "--today",
                    later,
                )
            )
            self.assertFalse(
                self._manifest_item_ids(data_dir, first["quiz_id"])
                & self._manifest_item_ids(data_dir, second["quiz_id"]),
                "做对且把握确定的原题不得在冷却窗口内重出",
            )

    def test_quiz_variant_grade_records_follow_up_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            graded, variants = self._grade_quiz_with_wrong_answers(data_dir)
            answers = ",".join(variant["answer"] for variant in variants)
            recorded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-variant-grade",
                    "--quiz-id",
                    graded["quiz_id"],
                    "--answers",
                    answers,
                )
            )
            self.assertEqual(len(variants), recorded["max_score"])
            self.assertEqual(
                len(variants), recorded["score"], "按正确答案作答应全部判对"
            )
            self.assertEqual(len(variants), recorded["recorded_attempts"])
            self.assertIn(
                recorded["next_action"]["mode"],
                {"quiz_prepare", "case_prepare", "essay_manual_flow"},
            )
            self.assertEqual(
                "existing_subject_allocator",
                recorded["next_action"]["decision_source"],
            )

            attempts = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            variant_events = [event for event in attempts if "-v-" in event["attempt_id"]]
            self.assertEqual(len(variants), len(variant_events))
            for event in variant_events:
                self.assertEqual("recognition", event["skill"])
                self.assertEqual("review", event["mode"])
                self.assertEqual(1, event["score"])
                self.assertTrue(event["variant_of"], "变式作答必须指回来源题")
                self.assertEqual(
                    "unsure",
                    event["confidence"],
                    "未声明把握度的变式不得冒充确定掌握",
                )

            replay = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-variant-grade",
                    "--quiz-id",
                    graded["quiz_id"],
                    "--answers",
                    answers,
                )
            )
            self.assertTrue(replay["idempotent"])
            self.assertEqual(0, replay["recorded_attempts"])

            conflicting = ",".join(
                next(letter for letter in "ABCD" if letter != variant["answer"])
                for variant in variants
            )
            _run_cli(
                data_dir,
                "quiz-variant-grade",
                "--quiz-id",
                graded["quiz_id"],
                "--answers",
                conflicting,
                expected_returncode=2,
            )

    def test_quiz_variant_grade_schedules_a_missed_variant_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            graded, variants = self._grade_quiz_with_wrong_answers(data_dir)
            answers = ["X"] + [
                next(letter for letter in "ABCD" if letter != variant["answer"])
                for variant in variants[1:]
            ]
            recorded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-variant-grade",
                    "--quiz-id",
                    graded["quiz_id"],
                    "--answers",
                    ",".join(answers),
                )
            )
            self.assertEqual(0, recorded["score"])
            conceded = recorded["results"][0]
            self.assertEqual("conceded", conceded["response_state"])
            self.assertIsNone(conceded["confidence"])
            self.assertEqual(["knowledge_gap"], conceded["wrong_reasons"])
            tomorrow = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
            for result in recorded["results"]:
                self.assertFalse(result["is_correct"])
                self.assertEqual(
                    tomorrow,
                    result["next_review_at"],
                    "答错或明确不会的变式题必须进入 1 天后的纠错复习",
                )
            for result in recorded["results"][1:]:
                self.assertEqual([], result["wrong_reasons"])
                self.assertEqual("unclassified", result["wrong_reason_status"])
            attempted_ids = {
                json.loads(line)["item_id"]
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            }
            self.assertTrue(
                {variant["item_id"] for variant in variants} <= attempted_ids,
                "变式题的作答现在也是正式证据，必须进入去重集合",
            )

    def test_quiz_grade_audit_failure_does_not_commit_learner_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir, limit=1)
            (data_dir / "quiz-audit-queue.jsonl").write_text(
                "not-json\n", encoding="utf-8"
            )
            before = _snapshot_files(data_dir)
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                prepared["quiz_id"],
                "--answers",
                "A",
                "--audit",
                "1=答案键疑似有误",
                expected_returncode=2,
            )
            self.assertEqual(
                before,
                _snapshot_files(data_dir),
                "a maintenance-queue failure must happen before learner evidence commits",
            )

    def test_quiz_grade_returns_a_self_contained_teaching_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir)
            graded = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-grade",
                    "--quiz-id",
                    prepared["quiz_id"],
                    "--answers",
                    "A,A,A,A,A",
                )
            )
            attempted_ids = {
                json.loads(line)["item_id"]
                for line in (data_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            wrong = [result for result in graded["results"] if result["is_correct"] is False]
            self.assertTrue(wrong, "an all-A answer set must produce wrong answers")
            for result in graded["results"]:
                for key in ("response_state", "wrong_reasons", "next_review_at"):
                    self.assertIn(key, result)
                self.assertIn("variant_question", result)
                self.assertIn("memory_hook", result)
                variant = result["variant_question"]
                if result["is_correct"] is False:
                    if variant is not None:
                        self.assertNotIn(variant["item_id"], attempted_ids)
                        self.assertTrue(variant["stem"])
                        self.assertTrue(variant["answer"])
                else:
                    self.assertIsNone(variant)
                explanation = result.get("explanation")
                if explanation:
                    self.assertNotIn("✅", explanation)
                    self.assertNotIn("](", explanation)

    def test_quiz_prepare_returns_objective_and_evidence_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            prepared = self._prepare_quiz(data_dir)
            self.assertEqual("comprehensive", prepared["selected_subject"])
            self.assertTrue(prepared["objective"])
            self.assertIsInstance(prepared["evidence_summary"], list)
            self.assertTrue(prepared["evidence_summary"])
            self.assertIsInstance(prepared["days_left"], int)
            self.assertGreater(prepared["days_left"], 0)
            self.assertEqual(45, prepared["daily_minutes"])

    def test_quiz_prepare_does_not_repeat_items_from_an_ungraded_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            first = self._prepare_quiz(data_dir)
            second = self._prepare_quiz(data_dir)
            first_items = self._manifest_item_ids(data_dir, first["quiz_id"])
            second_items = self._manifest_item_ids(data_dir, second["quiz_id"])
            self.assertEqual(5, len(first_items))
            self.assertFalse(
                first_items & second_items,
                "未判分的题组已经把题目展示给考生，同一天不得再次出同样的题",
            )

    def test_quiz_prepare_cools_concepts_already_tested_today(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            first = self._prepare_quiz(data_dir)
            manifest = self._quiz_manifest(data_dir, first["quiz_id"])
            answers = ",".join(
                "".join(question["correct"]) for question in manifest["questions"]
            )
            _run_cli(
                data_dir,
                "quiz-grade",
                "--quiz-id",
                first["quiz_id"],
                "--answers",
                answers,
                "--confidences",
                "sure,sure,sure,sure,sure",
            )
            second = self._prepare_quiz(data_dir)
            self.assertFalse(
                self._manifest_concept_ids(data_dir, first["quiz_id"])
                & self._manifest_concept_ids(data_dir, second["quiz_id"]),
                "同一细考点当天已经考过，不应在同一天的下一组再次排课",
            )

    def test_quiz_prepare_announces_a_concept_it_can_actually_serve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _append_mock_gap_session(
                data_dir,
                "mock-uml",
                at="2026-08-10T10:00:00+08:00",
                wrong_items=[
                    ("K03.SOFTWARE_DESIGN_UML", "exam-bank/05-uml.md#1", None)
                ],
            )
            registration = data_dir / "uml-variant.json"
            registration.write_text(
                json.dumps(
                    {
                        "item_id": "self-authored/uml-diagram-count-variant",
                        "topic_id": "K03.SOFTWARE_DESIGN_UML",
                        "concept_id": "K03.uml_diagram_count",
                        "question_family_id": "K03.uml_diagram_count.variant",
                        "variant_of": "exam-bank/05-uml.md#1",
                        "stem": "UML 2.x 图分类变式：结构与行为图各有多少种？",
                        "options": ["7 与 7", "9 与 5", "13 与 4", "17 与 0"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _run_cli(data_dir, "register-question", "--file", str(registration))
            _run_cli(
                data_dir,
                "record",
                "--topic",
                "K03.SOFTWARE_DESIGN_UML",
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--confidence",
                "sure",
                "--attempt-id",
                "uml-remedy",
                "--item-id",
                "self-authored/uml-diagram-count-variant",
                "--at",
                "2026-08-10T15:00:00+08:00",
            )
            prepared = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                    "--today",
                    "2026-08-20",
                )
            )
            # 该细考点的两道题都在 avoid 列表里，本组一道都出不了它；
            # 开场白不得宣称复测这个细考点。
            self.assertNotIn("UML 2.x 图分类与数量", prepared["objective"])

    def test_quiz_prepare_serves_a_gap_with_its_own_concept_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _append_mock_gap_session(
                data_dir,
                "mock-uml-relations",
                at="2026-08-10T10:00:00+08:00",
                wrong_items=[
                    ("K03.SOFTWARE_DESIGN_UML", "exam-bank/05-uml.md#4", None)
                ],
            )
            registration = data_dir / "uml-relations-variant.json"
            registration.write_text(
                json.dumps(
                    {
                        "item_id": "self-authored/uml-relations-variant",
                        "topic_id": "K03.SOFTWARE_DESIGN_UML",
                        "concept_id": "K03.uml_relationships",
                        "question_family_id": "K03.uml_relationships.variant",
                        "variant_of": "exam-bank/05-uml.md#4",
                        "stem": "UML 关系变式：实现与依赖分别用什么线型表示？",
                        "options": ["实线空心三角与虚线箭头", "实线实心菱形与虚线", "虚线箭头与实线", "实线关联与虚线关联"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _run_cli(data_dir, "register-question", "--file", str(registration))
            _run_cli(
                data_dir,
                "record",
                "--topic",
                "K03.SOFTWARE_DESIGN_UML",
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--confidence",
                "sure",
                "--attempt-id",
                "uml-relations-remedy",
                "--item-id",
                "self-authored/uml-relations-variant",
                "--at",
                "2026-08-10T15:00:00+08:00",
            )
            prepared = _json_output(
                _run_cli(
                    data_dir,
                    "quiz-prepare",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                    "--today",
                    "2026-08-20",
                )
            )
            concepts = {
                question["concept_id"]
                for question in self._quiz_manifest(data_dir, prepared["quiz_id"])[
                    "questions"
                ]
            }
            self.assertIn("K03.uml_relationships", concepts)
            self.assertIn("UML 泛化、实现与依赖关系", prepared["objective"])

    def test_subject_policy_blocks_automatic_recommendation_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for subject in ("comprehensive", "case"):
                _run_cli(
                    data_dir,
                    "mock",
                    "--subject",
                    subject,
                    "--mock-id",
                    f"m-{subject}",
                    "--paper-id",
                    f"p-{subject}",
                    "--score",
                    "60",
                    "--max-score",
                    "75",
                    "--duration-minutes",
                    "120",
                    "--complete",
                )
            unpaused = _json_output(
                _run_cli(data_dir, "recommend", "--json", "--limit", "10")
            )
            self.assertIn(
                "essay",
                {item["subject"] for item in _recommendation_items(unpaused)},
                "the unmeasured essay subject should be the automatic target",
            )
            _run_cli(
                data_dir,
                "configure",
                "--subject-policy",
                "essay=manual_trigger",
                "--subject-policy-reason",
                "考生要求主动触发",
            )
            paused = _json_output(
                _run_cli(data_dir, "recommend", "--json", "--limit", "10")
            )
            self.assertNotIn(
                "essay",
                {item["subject"] for item in _recommendation_items(paused)},
                "a manual_trigger subject must not be auto-selected",
            )
            explicit = _json_output(
                _run_cli(
                    data_dir, "recommend", "--json", "--limit", "5", "--subject", "essay"
                )
            )
            self.assertIn(
                "essay",
                {item["subject"] for item in _recommendation_items(explicit)},
                "an explicit --subject request still trains the paused subject",
            )
            _run_cli(data_dir, "configure", "--subject-policy", "essay=active")
            reactivated = _json_output(
                _run_cli(data_dir, "recommend", "--json", "--limit", "10")
            )
            self.assertIn(
                "essay",
                {item["subject"] for item in _recommendation_items(reactivated)},
            )

    def test_doctor_reports_the_question_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            payload = _json_output(_run_cli(data_dir, "doctor", "--json"))
            bank = [
                check for check in payload["checks"] if check["name"] == "question-bank"
            ]
            self.assertEqual(1, len(bank))
            self.assertTrue(bank[0]["healthy"])
            self.assertIn("可出题", bank[0]["message"])
            self.assertIn("质量门禁拦下", bank[0]["message"])

    def test_diagnostic_gaps_respect_strategic_skips(self) -> None:
        topic_id = "K08.SOFTWARE_PROCESS_MODELS"
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _append_mock_gap_session(
                data_dir,
                "skip-mock-001",
                wrong_items=[(topic_id, "exam-bank/07-software-engineering.md#1", None)],
            )
            baseline = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                )
            )
            self.assertIn(
                topic_id,
                {_recommendation_topic_id(item) for item in _recommendation_items(baseline)},
            )

            _run_cli(data_dir, "configure", "--skip-topic", f"{topic_id}=低频战略放弃")
            skipped = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                )
            )
            self.assertNotIn(
                topic_id,
                {_recommendation_topic_id(item) for item in _recommendation_items(skipped)},
            )

    def test_maintenance_never_evicts_uncorrected_mock_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for subject, score, at in (
                ("case", 70, "2026-06-30T10:00:00+08:00"),
                ("essay", 70, "2026-08-09T10:00:00+08:00"),
            ):
                _run_cli(
                    data_dir,
                    "mock",
                    "--subject",
                    subject,
                    "--mock-id",
                    f"evict-{subject}",
                    "--paper-id",
                    f"evict-paper-{subject}",
                    "--score",
                    str(score),
                    "--max-score",
                    "75",
                    "--duration-minutes",
                    "90",
                    "--complete",
                    "--at",
                    at,
                )
            _append_mock_gap_session(
                data_dir,
                "evict-comprehensive",
                wrong_items=[
                    ("K08.SOFTWARE_PROCESS_MODELS", "exam-bank/07-software-engineering.md#1", None),
                    ("K08.SOFTWARE_PROCESS_MODELS", "exam-bank/07-software-engineering.md#2", None),
                    ("K12.PATTERNS_SOA_MICROSERVICES", "exam-bank/13-design-patterns.md#1", "design_patterns"),
                    ("K09.QUALITY_SCENARIOS", "exam-bank/11-quality-attributes.md#1", None),
                ],
            )
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--limit",
                    "3",
                    "--today",
                    "2026-08-10",
                )
            )
            self.assertEqual(payload["target_subject"], "comprehensive")
            self.assertEqual(payload["maintenance_subject"], "case")
            items = _recommendation_items(payload)
            # All three slots are uncorrected mock gaps; the best-effort
            # maintenance item must not replace the last one.
            self.assertEqual(len(items), 3)
            self.assertTrue(all(item.get("diagnostic_status") for item in items))
            self.assertTrue(all(item["subject"] == "comprehensive" for item in items))

    def test_recommend_survives_corrupt_postmortems_with_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _append_mock_gap_session(
                data_dir,
                "corrupt-postmortem-001",
                wrong_items=[
                    ("K08.SOFTWARE_PROCESS_MODELS", "exam-bank/07-software-engineering.md#1", None)
                ],
            )
            (data_dir / "postmortems.jsonl").write_text(
                "{not json}\n", encoding="utf-8"
            )
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    "5",
                )
            )
            self.assertTrue(_recommendation_items(payload))
            self.assertTrue(payload.get("diagnosis_error"))
            self.assertEqual(payload.get("diagnosis", {}).get("issues"), [])

    def test_record_replay_stays_idempotent_when_enrichment_keys_appear(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            common = (
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--item-id",
                "exam-bank/07-software-engineering.md#9",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            _run_cli(
                data_dir,
                *common,
                "--attempt-id",
                "legacy-replay-001",
            )
            # A replay that now carries the derived metadata (as if recorded
            # by the newer terminal) is the same answer, not a conflict.
            enriched = _run_cli(
                data_dir,
                *common,
                "--attempt-id",
                "legacy-replay-001",
                "--concept-id",
                f"{topic_id}.legacy",
                "--question-family-id",
                f"{topic_id}.legacy",
            )
            self.assertIn("幂等跳过", enriched.stdout)

            # The reverse direction: a stored enriched event replayed without
            # the optional flags must also stay idempotent.
            _run_cli(
                data_dir,
                *common,
                "--attempt-id",
                "enriched-replay-001",
                "--concept-id",
                f"{topic_id}.enriched",
            )
            plain = _run_cli(
                data_dir,
                *common,
                "--attempt-id",
                "enriched-replay-001",
            )
            self.assertIn("幂等跳过", plain.stdout)

    def test_mock_is_complete_75_point_evidence_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            arguments = (
                "mock",
                "--subject",
                "comprehensive",
                "--mock-id",
                "mock-idempotent-001",
                "--paper-id",
                "fixture-comprehensive-001",
                "--score",
                "50",
                "--max-score",
                "75",
                "--duration-minutes",
                "120",
                "--complete",
                "--at",
                "2026-08-10T10:00:00+08:00",
            )
            _run_cli(data_dir, *arguments)
            first = self._status(data_dir)
            _run_cli(data_dir, *arguments)
            second = self._status(data_dir)
            self.assertEqual(first, second)
            comprehensive = second["subjects"]["comprehensive"]
            self.assertEqual(comprehensive["evidence_level"], "low")
            self.assertEqual(len(comprehensive["mock_scores"]), 1)

            rejected = _run_cli(
                data_dir,
                "mock",
                "--subject",
                "comprehensive",
                "--mock-id",
                "fake-one-point-mock",
                "--paper-id",
                "one-question",
                "--score",
                "1",
                "--max-score",
                "1",
                "--duration-minutes",
                "1",
                "--complete",
                expected_returncode=None,
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_high_frequency_weak_topic_precedes_low_frequency_weak_topic(self) -> None:
        comparable_group = set(
            self.curriculum["strategy"]["comprehensive_cold_start_groups"][0]
        )
        candidates = [
            topic
            for topic in self.topics
            if "comprehensive" in topic.get("subjects", [])
            and "recognition" in topic.get("skills", [])
            and topic["id"] in comparable_group
        ]
        self.assertGreaterEqual(len(candidates), 2)
        comparable_pairs = [
            (left, right)
            for left in candidates
            for right in candidates
            if left["estimated_minutes"] == right["estimated_minutes"]
            and float(left["frequency_count"]) > float(right["frequency_count"])
        ]
        self.assertTrue(comparable_pairs, "curriculum needs a cost-comparable frequency pair")
        high, low = max(
            comparable_pairs,
            key=lambda pair: float(pair[0]["frequency_count"])
            - float(pair[1]["frequency_count"]),
        )

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for topic, suffix in ((high, "high"), (low, "low")):
                record_arguments = [
                    "record",
                    "--topic",
                    topic["id"],
                    "--skill",
                    "recognition",
                    "--score",
                    "0",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"weak-{suffix}",
                    "--at",
                    "2026-08-10T11:00:00+08:00",
                    "--wrong-reason",
                    "knowledge_gap",
                ]
                if topic.get("facets"):
                    record_arguments.extend(["--facet", topic["facets"][0]])
                _run_cli(
                    data_dir,
                    *record_arguments,
                )

            arguments = (
                "recommend",
                "--json",
                "--subject",
                "comprehensive",
                "--limit",
                str(len(candidates)),
                "--today",
                "2026-08-10",
            )
            first_payload = _json_output(_run_cli(data_dir, *arguments))
            second_payload = _json_output(_run_cli(data_dir, *arguments))
            first_ids = [
                _recommendation_topic_id(item)
                for item in _recommendation_items(first_payload)
            ]
            second_ids = [
                _recommendation_topic_id(item)
                for item in _recommendation_items(second_payload)
            ]
            self.assertEqual(first_ids, second_ids, "first choice must be deterministic")
            self.assertIn(high["id"], first_ids)
            self.assertIn(low["id"], first_ids)
            self.assertLess(first_ids.index(high["id"]), first_ids.index(low["id"]))

    def test_mastered_case_strategy_track_yields_to_an_unseen_weak_track(self) -> None:
        primary = next(topic for topic in self.topics if topic["id"] == "C01.CASE_ATAM")
        fallback = next(topic for topic in self.topics if topic["id"] == "C02.CASE_DATABASE")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for number, at in enumerate(
                ("2026-08-10T09:00:00+08:00", "2026-08-12T09:00:00+08:00"),
                1,
            ):
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    primary["id"],
                    "--skill",
                    "application",
                    "--score",
                    "15",
                    "--max-score",
                    "25",
                    "--attempt-id",
                    f"atam-pass-{number}",
                    "--item-id",
                    f"atam-case-{number}",
                    "--at",
                    at,
                )

            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "case",
                    "--limit",
                    str(len(self.topics)),
                    "--today",
                    "2026-08-13",
                )
            )
            ids = [
                _recommendation_topic_id(item)
                for item in _recommendation_items(payload)
            ]
            self.assertLess(ids.index(fallback["id"]), ids.index(primary["id"]))

    def test_configured_case_and_essay_routes_filter_unselected_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "configure",
                "--case-track",
                "C02.CASE_DATABASE",
                "--essay-theme",
                "P03.ESSAY_RELIABILITY",
                "--skip-topic",
                "K07.REALTIME_EMBEDDED=当前低收益",
            )
            status = self._status(data_dir)
            self.assertEqual(status["strategy"]["case_tracks"], ["C02.CASE_DATABASE"])
            self.assertEqual(
                status["strategy"]["essay_themes"], ["P03.ESSAY_RELIABILITY"]
            )
            self.assertIn("K07.REALTIME_EMBEDDED", status["strategy"]["strategic_skips"])
            for subject, expected_prefix, expected_id in (
                ("case", "C", "C02.CASE_DATABASE"),
                ("essay", "P", "P03.ESSAY_RELIABILITY"),
            ):
                payload = _json_output(
                    _run_cli(
                        data_dir,
                        "recommend",
                        "--json",
                        "--subject",
                        subject,
                        "--limit",
                        str(len(self.topics)),
                        "--today",
                        "2026-08-10",
                    )
                )
                canonical_ids = [
                    _recommendation_topic_id(item)
                    for item in _recommendation_items(payload)
                    if _recommendation_topic_id(item).startswith(expected_prefix)
                ]
                self.assertEqual(canonical_ids, [expected_id])

            comprehensive = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "comprehensive",
                    "--limit",
                    str(len(self.topics)),
                    "--today",
                    "2026-08-10",
                )
            )
            comprehensive_ids = {
                _recommendation_topic_id(item)
                for item in _recommendation_items(comprehensive)
            }
            self.assertNotIn("K07.REALTIME_EMBEDDED", comprehensive_ids)

    def test_mastery_is_not_shared_across_subject_skills(self) -> None:
        topic = next(item for item in self.topics if item["id"] == "K09.QUALITY_SCENARIOS")
        route = next(item for item in self.topics if item["id"] == "C01.CASE_ATAM")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            for number in range(6):
                day = 10 if number < 5 else 12
                _run_cli(
                    data_dir,
                    "record",
                    "--topic",
                    topic["id"],
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"cross-skill-{number}",
                    "--at",
                    f"2026-08-{day:02d}T09:{number:02d}:00+08:00",
                )

            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "case",
                    "--limit",
                    str(len(self.topics)),
                    "--today",
                    "2026-08-12",
                )
            )
            recommendation = next(
                item
                for item in _recommendation_items(payload)
                if _recommendation_topic_id(item) == route["id"]
            )
            self.assertEqual(
                recommendation.get("mastery"),
                0,
                "recognition mastery must not reduce case/application priority",
            )

    def test_recognition_review_date_does_not_mark_application_due(self) -> None:
        topic = next(item for item in self.topics if item["id"] == "K09.QUALITY_SCENARIOS")
        route = next(item for item in self.topics if item["id"] == "C01.CASE_ATAM")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic["id"],
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "recognition-due-only",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            payload = _json_output(
                _run_cli(
                    data_dir,
                    "recommend",
                    "--json",
                    "--subject",
                    "case",
                    "--limit",
                    str(len(self.topics)),
                    "--today",
                    "2026-08-12",
                )
            )
            application_item = next(
                item
                for item in _recommendation_items(payload)
                if _recommendation_topic_id(item) == route["id"]
            )
            self.assertFalse(application_item["review_due"])

    def test_pending_wal_event_replays_without_double_count(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            initial_state = (data_dir / "state.json").read_bytes()
            initial_backup = (data_dir / "state.json.bak").read_bytes()
            arguments = (
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "transaction-retry-001",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            _run_cli(data_dir, *arguments)
            # Simulate a stop after the write-ahead event committed but before
            # its derived state replacement. Reading status must replay it.
            (data_dir / "state.json").write_bytes(initial_state)
            (data_dir / "state.json.bak").write_bytes(initial_backup)

            topic = _find_topic_record(self._status(data_dir), topic_id)
            attempt_counts = [
                value
                for value in _values_for_key(topic, "attempt_count")
                if isinstance(value, (int, float))
            ]
            self.assertIn(1, attempt_counts)
            events = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [event["attempt_id"] for event in events], ["transaction-retry-001"]
            )

    def test_missing_log_evidence_is_detected_and_repair_uses_the_log_as_truth(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "ghost-state-evidence",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            (data_dir / "attempts.jsonl").write_text("", encoding="utf-8")
            doctor = _run_cli(data_dir, "doctor", expected_returncode=None)
            self.assertNotEqual(doctor.returncode, 0)
            self.assertIn("不存在", doctor.stdout)

            _run_cli(data_dir, "repair")
            repaired = self._status(data_dir)
            self.assertEqual(repaired["topics"], {})
            _run_cli(data_dir, "doctor")

    def test_repair_preserves_subject_policies(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "repair-policy-evidence",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            _run_cli(
                data_dir,
                "configure",
                "--subject-policy",
                "essay=manual_trigger",
                "--subject-policy-reason",
                "考生仅在主动要求时练论文",
            )
            state_path = data_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["applied_attempt_ids"] = []
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            _run_cli(data_dir, "repair")
            repaired = self._status(data_dir)
            self.assertEqual(
                repaired["strategy"]["subject_policies"]["essay"]["mode"],
                "manual_trigger",
            )
            self.assertEqual(
                repaired["strategy"]["subject_policies"]["essay"]["reason"],
                "考生仅在主动要求时练论文",
            )

    def test_repair_can_recompute_valid_derived_state_without_changing_attempts(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "recompute-derived-001",
                "--item-id",
                "recompute-derived-item-001",
                "--wrong-reason",
                "recall_failure",
                "--at",
                "2026-08-10T09:00:00+08:00",
            )
            attempts_before = (data_dir / "attempts.jsonl").read_bytes()
            result = _run_cli(data_dir, "repair", "--recompute-derived")
            self.assertIn("已依据事件日志重建", result.stdout)
            self.assertEqual(attempts_before, (data_dir / "attempts.jsonl").read_bytes())
            self.assertTrue(list(data_dir.glob("state.json.pre-recompute.*")))
            _run_cli(data_dir, "doctor")

    def test_concurrent_records_are_serialized_without_lost_progress(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            processes: list[subprocess.Popen[str]] = []
            for number in range(12):
                command = [
                    sys.executable,
                    str(TUTOR_SCRIPT),
                    "--data-dir",
                    str(data_dir),
                    "record",
                    "--topic",
                    topic_id,
                    "--skill",
                    "recognition",
                    "--score",
                    "1",
                    "--max-score",
                    "1",
                    "--attempt-id",
                    f"concurrent-{number}",
                    "--item-id",
                    f"concurrent-item-{number}",
                    "--at",
                    f"2026-08-10T09:{number:02d}:00+08:00",
                ]
                processes.append(
                    subprocess.Popen(
                        command,
                        cwd=REPO_ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                if process.returncode != 0:
                    failures.append((process.returncode, stdout, stderr))
            self.assertEqual(failures, [])

            topic = _find_topic_record(self._status(data_dir), topic_id)
            self.assertEqual(topic["mastery"]["recognition"]["attempt_count"], 12)
            events = [
                json.loads(line)
                for line in (data_dir / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(len(events), 12)
            self.assertEqual(len({event["attempt_id"] for event in events}), 12)

    def test_corrupt_state_is_not_overwritten_and_repair_recovers_it(self) -> None:
        topic_id = self._recognition_topic()["id"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._init(data_dir)
            _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "0",
                "--max-score",
                "1",
                "--attempt-id",
                "before-corruption",
                "--at",
                "2026-08-10T12:00:00+08:00",
            )
            state_file = _primary_state_file(data_dir)
            corrupt_bytes = b'{"schema_version": 1, "learner": '
            state_file.write_bytes(corrupt_bytes)

            failed_write = _run_cli(
                data_dir,
                "record",
                "--topic",
                topic_id,
                "--skill",
                "recognition",
                "--score",
                "1",
                "--max-score",
                "1",
                "--attempt-id",
                "must-not-overwrite-corrupt-state",
                expected_returncode=None,
            )
            self.assertNotEqual(failed_write.returncode, 0)
            self.assertEqual(state_file.read_bytes(), corrupt_bytes)

            doctor = _run_cli(
                data_dir,
                "doctor",
                "--json",
                expected_returncode=None,
            )
            self.assertIn(
                doctor.returncode,
                (0, 1),
                f"doctor crashed unexpectedly: {doctor.stderr}",
            )
            diagnosis = (doctor.stdout + doctor.stderr).lower()
            self.assertRegex(diagnosis, r"corrupt|invalid|unhealthy|false|error")

            _run_cli(data_dir, "repair")
            repaired_status = self._status(data_dir)
            self.assertIsInstance(repaired_status, dict)
            self.assertNotEqual(state_file.read_bytes(), corrupt_bytes)
            json.loads(state_file.read_text(encoding="utf-8"))

            # A syntactically valid rollback is not enough: attempts.jsonl is
            # the durable evidence source, so repair must not silently forget
            # the last accepted answer. Otherwise replay is also impossible,
            # because the attempt ID will correctly be treated as a duplicate.
            repaired_topic = _find_topic_record(repaired_status, topic_id)
            repaired_counts = [
                value
                for key in ("attempt_count", "attempts", "evidence_count")
                for value in _values_for_key(repaired_topic, key)
                if isinstance(value, (int, float))
            ]
            self.assertTrue(repaired_counts, "repair lost the topic evidence")
            self.assertGreaterEqual(max(repaired_counts), 1)


class RepositoryContractTest(unittest.TestCase):
    def test_project_disables_openviking_plugin(self) -> None:
        config = (REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(
            config,
            '[plugins."openviking-memory@openviking"]\nenabled = false\n',
        )

    def test_2026_recall_source_stays_incomplete_and_non_official(self) -> None:
        source_pdf = (
            REPO_ROOT
            / "past-papers"
            / "source-pdfs"
            / "2026上"
            / "2026年上半年系统架构设计师真题（回忆版·题目与答案）.pdf"
        )
        summary = (REPO_ROOT / "past-papers" / "2026上-recall-signals.md").read_text(
            encoding="utf-8"
        )
        coverage = (REPO_ROOT / "past-papers" / "SOURCE_COVERAGE.md").read_text(
            encoding="utf-8"
        )
        survival = (REPO_ROOT / "past-papers" / "CASE_SURVIVAL.md").read_text(
            encoding="utf-8"
        )

        self.assertTrue(source_pdf.is_file())
        self.assertGreater(source_pdf.stat().st_size, 0)
        self.assertIn("source_type = recalled_real", summary)
        self.assertIn("source-pdfs/2026上/", summary)
        self.assertIn("第 71–75 题英语缺失", summary)
        self.assertIn("不能登记为完整模考证据", summary)
        self.assertIn("2026 上", coverage)
        self.assertIn("题型不固定", survival)

    def test_init_refuses_an_unignored_directory_inside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temporary:
            data_dir = Path(temporary)
            rejected = _run_cli(
                data_dir,
                "init",
                "--daily-minutes",
                "45",
                expected_returncode=None,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Git", rejected.stderr)
            self.assertEqual(list(data_dir.iterdir()), [])

    def test_all_private_study_files_are_gitignored(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "--", ".study"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "", ".study must never be tracked")

        for candidate in (
            ".study/state.json",
            ".study/events.jsonl",
            ".study/backups/state.json",
            ".study/arbitrary/nested/private-note.txt",
        ):
            with self.subTest(candidate=candidate):
                ignored = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", candidate],
                    cwd=REPO_ROOT,
                    check=False,
                )
                self.assertEqual(ignored.returncode, 0, f"not gitignored: {candidate}")

    def test_agent_documentation_local_references_exist(self) -> None:
        documents = (
            REPO_ROOT / ".claude" / "agents" / "senior-architect-pass-coach.md",
            REPO_ROOT / "AGENTS.md",
            REPO_ROOT / "CLAUDE.md",
            REPO_ROOT / "tutor" / "README.md",
            REPO_ROOT / "tutor" / "PROGRESS_PROTOCOL.md",
        )
        for document in documents:
            self.assertTrue(document.is_file(), f"missing agent document: {document}")

        local_reference_count = 0
        link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(text):
                target = raw_target.strip().strip("<>").split(maxsplit=1)[0]
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path_part = unquote(target.split("#", 1)[0])
                if not path_part:
                    continue
                local_reference_count += 1
                referenced = Path(path_part)
                if not referenced.is_absolute():
                    referenced = document.parent / referenced
                self.assertTrue(
                    referenced.resolve().exists(),
                    f"broken local reference in {document}: {raw_target}",
                )
        self.assertGreater(local_reference_count, 0, "agent docs need local references")

    def test_dashboard_uses_backend_status_without_average_mastery_shortcut(self) -> None:
        source = (REPO_ROOT / "tutor" / "app" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("average >= .78", source)
        self.assertIn("focusedRecord?.status", source)
        self.assertIn("/api/learning-plan", source)

    def test_exam_bank_questions_have_matching_answers(self) -> None:
        bank_files = sorted(
            path for path in (REPO_ROOT / "exam-bank").glob("*.md") if path.name != "README.md"
        )
        self.assertTrue(bank_files)

        seen_question_ids: set[str] = set()
        for bank_file in bank_files:
            text = bank_file.read_text(encoding="utf-8")
            headings = list(re.finditer(r"(?m)^###\s+(\d+)\.\s+", text))
            self.assertTrue(headings, f"no questions found in {bank_file}")
            numbers = [int(heading.group(1)) for heading in headings]
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), bank_file)

            for index, heading in enumerate(headings):
                question_number = int(heading.group(1))
                end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
                block = text[heading.start() : end]
                question_id = f"{bank_file.stem}.q{question_number:03d}"
                self.assertNotIn(question_id, seen_question_ids)
                seen_question_ids.add(question_id)

                options = re.findall(
                    r"(?m)^(?:✅\s+)?(?:\*\*)?([A-D])\.\s+",
                    block,
                )
                answers = re.findall(
                    r"(?m)^\*\*答案\*\*[：:]\s*(?:\*\*)?([A-D])",
                    block,
                )
                marked = re.findall(r"(?m)^✅\s+\*\*([A-D])\.", block)
                with self.subTest(question=question_id):
                    self.assertEqual(options, ["A", "B", "C", "D"])
                    self.assertEqual(len(answers), 1)
                    self.assertEqual(len(marked), 1)
                    self.assertEqual(answers[0], marked[0])


if __name__ == "__main__":
    unittest.main()
