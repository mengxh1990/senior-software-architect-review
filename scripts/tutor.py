#!/usr/bin/env python3
"""Local, dependency-free progress engine for the pass-first exam tutor.

The AI coach owns the conversation; this script owns durable evidence.  It
stores learner data only in ``.study/`` (or an explicitly supplied data
directory) and never performs network requests.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import question_registry
import sanitize_bank


SCHEMA_VERSION = 1
QUESTION_LINK_VERSION = 2
REPO_ROOT = Path(__file__).resolve().parents[1]
CURRICULUM_PATH = REPO_ROOT / "tutor" / "curriculum.json"
FREQUENCY_SNAPSHOT_PATH = REPO_ROOT / "tutor" / "frequency-snapshot.json"
FREQUENCY_BUILDER_PATH = REPO_ROOT / "scripts" / "build_frequency_snapshot.py"
SUBJECTS = ("comprehensive", "case", "essay")
SKILLS = ("recognition", "application", "production")
WRONG_REASONS = {
    "knowledge_gap",
    "recall_failure",
    "concept_confusion",
    "misread",
    "calculation",
    "application",
    "missing_keyword",
    "weak_tradeoff",
    "weak_project_detail",
    "no_metric",
    "expression",
    "time_management",
    "careless",
    "guessed_correct",
}
WRONG_REASON_SOURCES = {
    "learner",
    "response_state",
    "confidence",
    "learner_postmortem",
}
REVIEW_INTERVAL_DAYS = (1, 3, 7, 14, 30)
QUIZ_SCHEMA_VERSION = 1
QUIZ_SESSIONS_DIR = "quiz-sessions"
RECALLED_REAL_YEARS = {"2023下", "2024上", "2024下", "2025上", "2025下", "2026上"}
# An item answered correctly with certain confidence, or already shown as a
# follow-up variant, stays out of rotation for this many days.  The fine
# concept keeps its review schedule; only the exact item is retired so spacing
# lands on a fresh question instead of a memorised one.
RECENT_ITEM_COOLDOWN_DAYS = 14
CASE_RESOURCE_RE = re.compile(r"^past-papers/case-types/(\d{2})-")


class TutorError(RuntimeError):
    """A user-actionable state or input error."""


class QuestionLinkMigrationRequired(TutorError):
    """Historical evidence needs the explicit, backed-up link migration."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise TutorError(f"无效时间：{value!r}，请使用 ISO-8601 格式") from error
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise TutorError(f"无效日期：{value!r}，请使用 YYYY-MM-DD") from error


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def fsync_directory(directory: Path) -> None:
    """Persist directory metadata after an atomic replacement when supported."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Validate elsewhere, then atomically replace a file in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    serialized = json_text(value)
    json.loads(serialized)
    atomic_write_text(path, serialized)


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    """Serialize every read-modify-write cycle for one learner directory."""

    lock_path = data_dir / ".tutor.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, label: str) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise TutorError(f"缺少{label}：{path}") from error
    if not content.strip():
        raise TutorError(f"{label}为空，拒绝静默覆盖：{path}")
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise TutorError(f"{label}损坏或不是有效 JSON，拒绝静默覆盖：{path}") from error


def load_curriculum() -> dict[str, Any]:
    curriculum = load_json(CURRICULUM_PATH, "课程表")
    if not isinstance(curriculum, dict) or not isinstance(curriculum.get("topics"), list):
        raise TutorError("tutor/curriculum.json 缺少 topics 数组")
    if curriculum.get("schema_version") != SCHEMA_VERSION:
        raise TutorError("课程表 schema_version 不受支持")

    seen: set[str] = set()
    for topic in curriculum["topics"]:
        if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
            raise TutorError("课程表包含无效考点")
        topic_id = topic["id"]
        if topic_id in seen:
            raise TutorError(f"课程表考点 ID 重复：{topic_id}")
        seen.add(topic_id)
    groups = curriculum.get("strategy", {}).get("comprehensive_cold_start_groups", [])
    if not isinstance(groups, list) or any(not isinstance(group, list) for group in groups):
        raise TutorError("课程表 comprehensive_cold_start_groups 无效")
    grouped_ids = [topic_id for group in groups for topic_id in group]
    if len(grouped_ids) != len(set(grouped_ids)) or any(
        topic_id not in seen for topic_id in grouped_ids
    ):
        raise TutorError("课程表冷启动分组包含重复或未知考点 ID")
    case_type_ids: set[str] = set()
    covered_case_topics: set[str] = set()
    for topic in curriculum["topics"]:
        case_type = topic.get("case_type")
        covered_topic_ids = topic.get("covered_topic_ids", [])
        if case_type is not None and not re.fullmatch(r"\d{2}", str(case_type)):
            raise TutorError(f"课程表考点 {topic['id']} case_type 无效")
        if (
            not isinstance(covered_topic_ids, list)
            or any(
                not isinstance(topic_id, str) or topic_id not in seen
                for topic_id in covered_topic_ids
            )
            or len(covered_topic_ids) != len(set(covered_topic_ids))
        ):
            raise TutorError(f"课程表考点 {topic['id']} covered_topic_ids 无效")
        if covered_topic_ids and not str(topic["id"]).startswith("C"):
            raise TutorError(f"课程表考点 {topic['id']} 不能声明案例路线覆盖")
        if case_type is not None and str(topic["id"]).startswith("C"):
            if str(case_type) in case_type_ids:
                raise TutorError(f"课程表案例题型重复：{case_type}")
            case_type_ids.add(str(case_type))
        duplicate_coverage = covered_case_topics.intersection(covered_topic_ids)
        if duplicate_coverage:
            raise TutorError(
                "课程表案例路线重复覆盖考点：" + ", ".join(sorted(duplicate_coverage))
            )
        covered_case_topics.update(covered_topic_ids)
    return curriculum


def topic_map(curriculum: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {topic["id"]: topic for topic in curriculum["topics"]}


def state_paths(data_dir: Path) -> dict[str, Path]:
    return {
        "profile": data_dir / "profile.json",
        "state": data_dir / "state.json",
        "attempts": data_dir / "attempts.jsonl",
        "dashboard": data_dir / "dashboard.md",
        "question_registry": question_registry.registry_path(data_dir),
        "postmortems": data_dir / "postmortems.jsonl",
    }


def blank_subject() -> dict[str, Any]:
    return {
        "mock_scores": [],
        "latest_mock_score": None,
        "predicted_score": None,
        "lower_bound_score": None,
        "evidence_level": "cold_start",
        "last_practiced_at": None,
        "evidence_count": 0,
    }


def new_state(curriculum: dict[str, Any], created_at: str) -> dict[str, Any]:
    strategy = curriculum.get("strategy", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "question_link_version": QUESTION_LINK_VERSION,
        "strategy": {
            "pass_line": float(strategy.get("pass_line", 45)),
            "safe_target": float(strategy.get("safe_target", 52)),
            "min_review_interval_days": 0,
            "case_tracks": ["C01.CASE_ATAM"],
            "essay_themes": [],
            "case_tracks_configured": False,
            "essay_themes_configured": False,
            "strategic_skips": {},
            "subject_policies": {},
        },
        "subjects": {subject: blank_subject() for subject in SUBJECTS},
        "topics": {},
        "applied_attempt_ids": [],
        "created_at": created_at,
        "last_session_at": None,
    }


def require_number_or_none(value: Any, label: str) -> None:
    if value is not None and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise TutorError(f"state.json {label} 必须是数字或 null")


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise TutorError("state.json 顶层必须是对象")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise TutorError(
            f"state.json schema_version={state.get('schema_version')!r} 不受支持"
        )
    link_version = state.get("question_link_version", 0)
    if not isinstance(link_version, int) or not 0 <= link_version <= QUESTION_LINK_VERSION:
        raise TutorError("state.json question_link_version 不受支持")
    subjects = state.get("subjects")
    if not isinstance(subjects, dict) or any(
        not isinstance(subjects.get(subject), dict) for subject in SUBJECTS
    ):
        raise TutorError("state.json 缺少三科独立状态")
    if not isinstance(state.get("topics"), dict):
        raise TutorError("state.json topics 必须是对象")
    strategy = state.get("strategy")
    if not isinstance(strategy, dict):
        raise TutorError("state.json strategy 必须是对象")
    for key in ("pass_line", "safe_target"):
        require_number_or_none(strategy.get(key), f"strategy.{key}")
    minimum_interval = strategy.get("min_review_interval_days", 0)
    if (
        not isinstance(minimum_interval, int)
        or isinstance(minimum_interval, bool)
        or minimum_interval < 0
    ):
        raise TutorError(
            "state.json strategy.min_review_interval_days 必须是非负整数"
        )
    for key in ("case_tracks", "essay_themes"):
        values = strategy.get(key)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise TutorError(f"state.json strategy.{key} 必须是字符串数组")
    for key in ("case_tracks_configured", "essay_themes_configured"):
        if not isinstance(strategy.get(key), bool):
            raise TutorError(f"state.json strategy.{key} 必须是布尔值")
    skips = strategy.get("strategic_skips", {})
    if not isinstance(skips, dict) or any(
        not isinstance(topic_id, str) or not isinstance(reason, str)
        for topic_id, reason in skips.items()
    ):
        raise TutorError("state.json strategy.strategic_skips 必须是字符串映射")
    policies = strategy.get("subject_policies", {})
    if not isinstance(policies, dict):
        raise TutorError("state.json strategy.subject_policies 必须是对象")
    for subject_name, policy in policies.items():
        if subject_name not in SUBJECTS or not isinstance(policy, dict):
            raise TutorError("state.json strategy.subject_policies 包含无效科目")
        if policy.get("mode") not in ("active", "manual_trigger"):
            raise TutorError(
                f"state.json strategy.subject_policies.{subject_name}.mode 无效"
            )
        if policy.get("reason") is not None and not isinstance(policy["reason"], str):
            raise TutorError(
                f"state.json strategy.subject_policies.{subject_name}.reason 必须是字符串"
            )
        if policy.get("updated_at") is not None:
            parse_datetime(policy["updated_at"])
    applied_ids = state.get("applied_attempt_ids")
    if not isinstance(applied_ids, list) or any(
        not isinstance(item, str) or not item for item in applied_ids
    ):
        raise TutorError("state.json applied_attempt_ids 必须是非空字符串数组")
    if len(applied_ids) != len(set(applied_ids)):
        raise TutorError("state.json applied_attempt_ids 存在重复")

    for subject_name in SUBJECTS:
        subject = subjects[subject_name]
        if not isinstance(subject.get("mock_scores"), list):
            raise TutorError(f"state.json subjects.{subject_name}.mock_scores 必须是数组")
        if subject.get("evidence_level") not in ("cold_start", "low", "medium", "high"):
            raise TutorError(f"state.json subjects.{subject_name}.evidence_level 无效")
        evidence_count = subject.get("evidence_count")
        if not isinstance(evidence_count, int) or isinstance(evidence_count, bool) or evidence_count < 0:
            raise TutorError(f"state.json subjects.{subject_name}.evidence_count 无效")
        for key in ("latest_mock_score", "predicted_score", "lower_bound_score"):
            require_number_or_none(subject.get(key), f"subjects.{subject_name}.{key}")
        if subject.get("last_practiced_at") is not None:
            parse_datetime(subject["last_practiced_at"])
        seen_mocks: set[str] = set()
        for mock in subject["mock_scores"]:
            if not isinstance(mock, dict) or not isinstance(mock.get("mock_id"), str):
                raise TutorError(f"state.json subjects.{subject_name}.mock_scores 包含无效记录")
            if mock["mock_id"] in seen_mocks:
                raise TutorError(f"state.json 模考 ID 重复：{mock['mock_id']}")
            seen_mocks.add(mock["mock_id"])
            if not isinstance(mock.get("paper_id"), str) or not mock["paper_id"]:
                raise TutorError(f"state.json 模考 {mock['mock_id']} 缺少 paper_id")
            parse_datetime(mock.get("at"))
            for key in ("score", "max_score", "score_75", "duration_minutes"):
                value = mock.get(key)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                ):
                    raise TutorError(f"state.json 模考 {mock['mock_id']}.{key} 无效")
            if mock.get("complete") is not True:
                raise TutorError(f"state.json 模考 {mock['mock_id']} 不是完整证据")

    for topic_id, topic in state["topics"].items():
        if not isinstance(topic_id, str) or not isinstance(topic, dict):
            raise TutorError("state.json topics 包含无效考点")
        mastery = topic.get("mastery")
        if not isinstance(mastery, dict):
            raise TutorError(f"state.json topics.{topic_id}.mastery 必须是对象")
        if topic.get("status") not in ("unseen", "learning", "fragile", "pass_ready"):
            raise TutorError(f"state.json topics.{topic_id}.status 无效")
        if topic.get("last_attempt_at") is not None:
            parse_datetime(topic["last_attempt_at"])
        if topic.get("next_review_at") is not None:
            parse_date(topic["next_review_at"])
        for skill, record in mastery.items():
            if skill not in SKILLS or not isinstance(record, dict):
                raise TutorError(f"state.json topics.{topic_id} 包含无效能力维度")
            count = record.get("attempt_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise TutorError(f"state.json topics.{topic_id}.{skill}.attempt_count 无效")
            for key in ("score_sum", "max_score_sum", "mastery"):
                if not isinstance(record.get(key), (int, float)) or isinstance(record.get(key), bool):
                    raise TutorError(f"state.json topics.{topic_id}.{skill}.{key} 无效")
                if not math.isfinite(float(record[key])):
                    raise TutorError(f"state.json topics.{topic_id}.{skill}.{key} 不是有限数")
            if record.get("status") not in ("unseen", "learning", "fragile", "pass_ready"):
                raise TutorError(f"state.json topics.{topic_id}.{skill}.status 无效")
            for key in ("attempted_items", "qualified_evidence", "successful_dates"):
                if not isinstance(record.get(key), list):
                    raise TutorError(f"state.json topics.{topic_id}.{skill}.{key} 必须是数组")
            attempted_items = record["attempted_items"]
            if any(not isinstance(item, str) or not item for item in attempted_items):
                raise TutorError(f"state.json topics.{topic_id}.{skill}.attempted_items 无效")
            if len(attempted_items) != len(set(attempted_items)):
                raise TutorError(f"state.json topics.{topic_id}.{skill}.attempted_items 重复")
            for evidence in record["qualified_evidence"]:
                if (
                    not isinstance(evidence, dict)
                    or not isinstance(evidence.get("attempt_id"), str)
                    or not isinstance(evidence.get("item_id"), str)
                ):
                    raise TutorError(f"state.json topics.{topic_id}.{skill} 资格证据无效")
                parse_datetime(evidence.get("at"))
                ratio = evidence.get("ratio")
                if (
                    not isinstance(ratio, (int, float))
                    or isinstance(ratio, bool)
                    or not math.isfinite(float(ratio))
                ):
                    raise TutorError(f"state.json topics.{topic_id}.{skill} 资格比例无效")
            for successful_date in record["successful_dates"]:
                if not isinstance(successful_date, str):
                    raise TutorError(f"state.json topics.{topic_id}.{skill} 成功日期无效")
                parse_date(successful_date)
            if not isinstance(record.get("wrong_reason_counts"), dict):
                raise TutorError(f"state.json topics.{topic_id}.{skill}.wrong_reason_counts 无效")
            for key in (
                "confirmed_wrong_reason_counts",
                "legacy_inferred_wrong_reason_counts",
            ):
                value = record.get(key, {})
                if not isinstance(value, dict):
                    raise TutorError(f"state.json topics.{topic_id}.{skill}.{key} 无效")
            unclassified = record.get("unclassified_wrong_count", 0)
            if (
                not isinstance(unclassified, int)
                or isinstance(unclassified, bool)
                or unclassified < 0
            ):
                raise TutorError(
                    f"state.json topics.{topic_id}.{skill}.unclassified_wrong_count 无效"
                )
            if record.get("last_attempt_at") is not None:
                parse_datetime(record["last_attempt_at"])
            if record.get("next_review_at") is not None:
                parse_date(record["next_review_at"])
            if "regression_active" in record and not isinstance(
                record["regression_active"], bool
            ):
                raise TutorError(f"state.json topics.{topic_id}.{skill}.regression_active 无效")
            if record.get("regression_active"):
                if not isinstance(record.get("regressed_at"), str):
                    raise TutorError(f"state.json topics.{topic_id}.{skill} 缺少回退时间")
                parse_datetime(record["regressed_at"])
                if not isinstance(record.get("regressed_item_id"), str):
                    raise TutorError(f"state.json topics.{topic_id}.{skill} 缺少回退题目")
    if state.get("last_session_at") is not None:
        parse_datetime(state["last_session_at"])
    return state


def load_profile_and_state(
    data_dir: Path, *, persist_pending: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = state_paths(data_dir)
    profile = load_json(paths["profile"], "私人档案")
    state = validate_state(load_json(paths["state"], "学习状态"))
    if not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA_VERSION:
        raise TutorError("profile.json schema_version 不受支持")
    attempts = load_attempts(paths["attempts"])
    logged_ids = {event["attempt_id"] for event in attempts}
    applied_ids = set(state["applied_attempt_ids"])
    ghost_ids = applied_ids - logged_ids
    if ghost_ids:
        raise TutorError(
            "状态包含证据日志中不存在的作答："
            + ", ".join(sorted(ghost_ids))
            + "；请运行 repair，以事件日志为准重建"
        )
    pending = [event for event in attempts if event["attempt_id"] not in applied_ids]
    if pending:
        curriculum = load_curriculum()
        for event in pending:
            apply_event_to_state(state, event, curriculum)
    # Project the current topic-level mastery rule in memory. Older private
    # states may have been evaluated against subtopic coverage requirements;
    # reading progress must not require rewriting their answer ledger.
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    safe_target = float(state.get("strategy", {}).get("safe_target", 52))
    for topic_id, topic_record in state.get("topics", {}).items():
        topic = topics.get(topic_id)
        if topic is None:
            continue
        for skill, record in topic_record.get("mastery", {}).items():
            if isinstance(record, dict):
                record["status"] = skill_status(skill, record, safe_target)
        refresh_topic_status(topic_record, topic.get("skills", []))
    if pending and persist_pending:
        save_state_bundle(data_dir, profile, state, backup=True)
    return profile, state


def ensure_question_links_current(
    state: dict[str, Any], attempts: list[dict[str, Any]]
) -> None:
    """Do not offer a training route based on old, misclassified evidence."""

    if state.get("question_link_version", 0) >= QUESTION_LINK_VERSION:
        return
    if any(question_registry.canonicalize_public_event(event) != event for event in attempts):
        raise QuestionLinkMigrationRequired(
            "历史题目关联仍使用旧映射；请先运行 "
            "python3 scripts/tutor.py repair --normalize-question-links"
        )


def load_attempts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise TutorError(f"缺少作答证据日志：{path}")
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise TutorError(f"attempts.jsonl 第 {line_number} 行损坏，拒绝写入") from error
        if not isinstance(event, dict) or not isinstance(event.get("attempt_id"), str):
            raise TutorError(f"attempts.jsonl 第 {line_number} 行缺少 attempt_id")
        if not event["attempt_id"]:
            raise TutorError(f"attempts.jsonl 第 {line_number} 行 attempt_id 为空")
        if event["attempt_id"] in seen_ids:
            raise TutorError(f"attempts.jsonl 存在重复 attempt_id：{event['attempt_id']}")
        seen_ids.add(event["attempt_id"])
        events.append(event)
    return events


def write_attempts(path: Path, events: Iterable[dict[str, Any]]) -> None:
    content = "".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events
    )
    atomic_write_text(path, content)


def subject_status(subject: dict[str, Any], safe_target: float) -> str:
    lower_bound = subject.get("lower_bound_score")
    if lower_bound is None:
        return "unmeasured"
    if lower_bound < 45:
        return "danger"
    if lower_bound < safe_target:
        return "near"
    return "safe"


def effective_review_date(
    next_review: str | None,
    last_attempt: str | None,
    minimum_interval: int,
    status: str | None = None,
) -> str | None:
    """Apply the configured review floor only to stable maintenance.

    Stored ``next_review_at`` keeps its organic spacing (1/3/14 days). The
    breadth-oriented ``min_review_interval_days`` floor must never postpone a
    wrong or fragile item's corrective review.
    """

    if not next_review:
        return next_review
    effective = parse_date(next_review)
    stored_interval = None
    if last_attempt:
        stored_interval = (effective - parse_datetime(last_attempt).date()).days
    if (
        status == "pass_ready"
        and minimum_interval > 0
        and last_attempt
        and stored_interval is not None
        and stored_interval >= 14
    ):
        floor = parse_datetime(last_attempt).date() + timedelta(days=minimum_interval)
        if floor > effective:
            effective = floor
    return effective.isoformat()


def scheduled_review_date(
    previous_last_at: str | None,
    previous_due_at: str | None,
    attempted_at: datetime,
    *,
    stable_success: bool,
) -> date:
    """Advance the 1/3/7/14/30-day ladder without postponing early reviews."""

    attempted_on = attempted_at.date()
    if not stable_success:
        return attempted_on + timedelta(days=REVIEW_INTERVAL_DAYS[0])
    if previous_due_at:
        previous_due = parse_date(previous_due_at)
        if attempted_on < previous_due:
            return previous_due
        previous_delay = 0
        if previous_last_at:
            previous_delay = max(
                0,
                (previous_due - parse_datetime(previous_last_at).date()).days,
            )
        next_delay = next(
            (
                delay
                for delay in REVIEW_INTERVAL_DAYS[1:]
                if delay > previous_delay
            ),
            REVIEW_INTERVAL_DAYS[-1],
        )
        return attempted_on + timedelta(days=next_delay)
    return attempted_on + timedelta(days=REVIEW_INTERVAL_DAYS[1])


def status_payload(profile: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    safe_target = float(state.get("strategy", {}).get("safe_target", 52))
    minimum_interval = int(
        state.get("strategy", {}).get("min_review_interval_days", 0) or 0
    )
    subjects: dict[str, Any] = {}
    for name in SUBJECTS:
        item = dict(state["subjects"][name])
        item["status"] = subject_status(item, safe_target)
        subjects[name] = item
    # Report review dates with the interval floor applied, without mutating
    # the stored organic values.
    topics: dict[str, Any] = {}
    for topic_id, record in state.get("topics", {}).items():
        mastery = record.get("mastery") if isinstance(record, dict) else None
        if not isinstance(mastery, dict):
            topics[topic_id] = record
            continue
        effective_mastery: dict[str, Any] = {}
        effective_dates: list[str] = []
        for skill, skill_record in mastery.items():
            if not isinstance(skill_record, dict):
                effective_mastery[skill] = skill_record
                continue
            effective = effective_review_date(
                skill_record.get("next_review_at"),
                skill_record.get("last_attempt_at"),
                minimum_interval,
                skill_record.get("status"),
            )
            if effective:
                effective_dates.append(effective)
            effective_mastery[skill] = {**skill_record, "next_review_at": effective}
        topics[topic_id] = {
            **record,
            "mastery": effective_mastery,
            "next_review_at": min(effective_dates) if effective_dates else None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "strategy": state.get("strategy", {}),
        "subjects": subjects,
        "topics": topics,
        "last_session_at": state.get("last_session_at"),
    }


def render_dashboard(profile: dict[str, Any], state: dict[str, Any]) -> str:
    payload = status_payload(profile, state)
    lines = [
        "# 私人学习进度",
        "",
        "> 此文件位于 `.study/`，不得提交到公共仓库。",
        "",
        f"- 考试日期：{profile.get('exam_date') or '未设置'}",
        f"- 每日预算：{profile.get('daily_minutes', 0)} 分钟",
        f"- 最后学习：{payload.get('last_session_at') or '尚无有效作答'}",
        "",
        "## 三科过线状态",
        "",
        "| 科目 | 状态 | 最近模考 | 保守下界 | 证据 |",
        "|---|---|---:|---:|---|",
    ]
    labels = {"comprehensive": "综合", "case": "案例", "essay": "论文"}
    for name in SUBJECTS:
        item = payload["subjects"][name]
        lines.append(
            "| {label} | {status} | {latest} | {lower} | {evidence} |".format(
                label=labels[name],
                status=item["status"],
                latest=item.get("latest_mock_score")
                if item.get("latest_mock_score") is not None
                else "—",
                lower=item.get("lower_bound_score")
                if item.get("lower_bound_score") is not None
                else "—",
                evidence=item.get("evidence_level", "cold_start"),
            )
        )

    lines.extend(["", "## 已产生证据的考点", ""])
    if not payload["topics"]:
        lines.append("尚无有效作答，不能判断掌握度。")
    else:
        lines.extend(
            [
                "| 考点 | 综合识别 | 案例应用 | 论文产出 | 整体 | 最近作答 | 下次复习 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for topic_id, record in sorted(payload["topics"].items()):
            mastery = record.get("mastery", {})

            def dimension_status(skill: str) -> str:
                value = mastery.get(skill)
                return value.get("status", "unseen") if isinstance(value, dict) else "unseen"

            lines.append(
                f"| {topic_id} | {dimension_status('recognition')} | "
                f"{dimension_status('application')} | {dimension_status('production')} | "
                f"{record.get('status', 'learning')} | "
                f"{record.get('last_attempt_at') or '—'} | "
                f"{record.get('next_review_at') or '—'} |"
            )
    return "\n".join(lines) + "\n"


def save_state_bundle(
    data_dir: Path,
    profile: dict[str, Any],
    state: dict[str, Any],
    *,
    backup: bool = True,
) -> None:
    paths = state_paths(data_dir)
    validate_state(state)
    atomic_write_json(paths["state"], state)
    atomic_write_text(paths["dashboard"], render_dashboard(profile, state))
    if backup:
        # A stale backup is safe because attempts.jsonl is the durable source
        # of truth; repair deterministically replays every logged event.
        atomic_write_json(paths["state"].with_name("state.json.bak"), state)


def new_skill_record() -> dict[str, Any]:
    return {
        "status": "unseen",
        "mastery": 0.0,
        "attempt_count": 0,
        "score_sum": 0.0,
        "max_score_sum": 0.0,
        "attempted_items": [],
        "qualified_evidence": [],
        "successful_dates": [],
        "wrong_reason_counts": {},
        "confirmed_wrong_reason_counts": {},
        "legacy_inferred_wrong_reason_counts": {},
        "unclassified_wrong_count": 0,
        "last_attempt_at": None,
        "next_review_at": None,
    }


def skill_status(
    skill: str,
    record: dict[str, Any],
    safe_target: float,
) -> str:
    attempts = int(record.get("attempt_count", 0))
    if attempts == 0:
        return "unseen"
    maximum = float(record.get("max_score_sum", 0))
    accuracy = float(record.get("score_sum", 0)) / maximum if maximum else 0.0
    evidence = [
        item for item in record.get("qualified_evidence", []) if isinstance(item, dict)
    ]
    unique_items = {item.get("item_id") for item in evidence if item.get("item_id")}
    dates = sorted(
        {
            parse_datetime(item.get("at")).date().isoformat()
            for item in evidence
            if item.get("at")
        }
    )

    if record.get("regression_active"):
        regressed_at = parse_datetime(record.get("regressed_at"))
        regressed_item_id = record.get("regressed_item_id")
        recovery = [
            item
            for item in evidence
            if item.get("at") and parse_datetime(item["at"]) > regressed_at
        ]
        recovered = False
        if skill == "recognition":
            recovery = [
                item for item in recovery if item.get("item_id") != regressed_item_id
            ]
            recovery_items = {item.get("item_id") for item in recovery if item.get("item_id")}
            recovery_dates = {
                parse_datetime(item["at"]).date().isoformat() for item in recovery
            }
            recovered = len(recovery_items) >= 2 and len(recovery_dates) >= 2
        elif skill == "application":
            recovery = [
                item for item in recovery if item.get("item_id") != regressed_item_id
            ]
            recovered = any(
                left.get("item_id") != right.get("item_id")
                and abs(parse_datetime(right["at"]) - parse_datetime(left["at"]))
                >= timedelta(hours=48)
                for left_index, left in enumerate(recovery)
                for right in recovery[left_index + 1 :]
            )
        elif skill == "production":
            recovered = any(item.get("mode") == "full_timed" for item in recovery)
        if not recovered:
            return "fragile"

    if skill == "recognition":
        if (
            len(unique_items) >= 6
            and len(dates) >= 2
            and accuracy >= 0.8
        ):
            return "pass_ready"
    elif skill == "application":
        timed_items = [
            (item.get("item_id"), parse_datetime(item["at"]))
            for item in evidence
            if item.get("item_id") and item.get("at")
        ]
        distinct_pair_is_spaced = any(
            left_id != right_id and abs(right_at - left_at) >= timedelta(hours=48)
            for left_index, (left_id, left_at) in enumerate(timed_items)
            for right_id, right_at in timed_items[left_index + 1 :]
        )
        if (
            len(unique_items) >= 2
            and accuracy >= 0.6
            and distinct_pair_is_spaced
        ):
            return "pass_ready"
    elif skill == "production":
        safe_ratio = safe_target / 75.0
        if any(
            item.get("mode") == "full_timed"
            and (
                float(item.get("score", 0)) / float(item.get("max_score", 1))
                if float(item.get("max_score", 0)) > 0
                else float(item.get("ratio", 0))
            )
            >= safe_ratio
            for item in evidence
        ):
            return "pass_ready"

    if accuracy >= 0.75 and attempts >= 2:
        return "fragile"
    return "learning"


def refresh_topic_status(
    topic_record: dict[str, Any], required_skills: Iterable[str]
) -> None:
    mastery = topic_record.get("mastery", {})
    statuses = []
    for skill in required_skills:
        record = mastery.get(skill)
        statuses.append(
            record.get("status", "unseen") if isinstance(record, dict) else "unseen"
        )
    if statuses and all(status == "pass_ready" for status in statuses):
        overall = "pass_ready"
    elif any(status == "fragile" for status in statuses):
        overall = "fragile"
    elif any(status != "unseen" for status in statuses):
        overall = "learning"
    else:
        overall = "unseen"
    topic_record["status"] = overall


def cmd_init(args: argparse.Namespace) -> int:
    curriculum = load_curriculum()
    data_dir = args.data_dir
    private, privacy_message = privacy_check(data_dir)
    if not private:
        raise TutorError(f"拒绝在可能被 Git 跟踪的目录建档：{privacy_message}")
    paths = state_paths(data_dir)
    if paths["state"].exists() or paths["profile"].exists():
        load_profile_and_state(data_dir)
        print(f"私人学习档案已存在：{data_dir}")
        return 0

    exam_date = None
    if args.exam_date:
        exam_date = parse_date(args.exam_date).isoformat()
    if args.daily_minutes <= 0 or args.daily_minutes > 1440:
        raise TutorError("daily-minutes 必须在 1–1440 之间")

    created_at = now_iso()
    profile = {
        "schema_version": SCHEMA_VERSION,
        "exam_date": exam_date,
        "daily_minutes": args.daily_minutes,
        "timezone": datetime.now().astimezone().tzname(),
        "background": args.background or "",
        "known_strengths": [],
        "known_weaknesses": [],
        "created_at": created_at,
    }
    state = new_state(curriculum, created_at)

    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["profile"], profile)
    atomic_write_text(paths["attempts"], "")
    save_state_bundle(data_dir, profile, state, backup=True)
    print(f"已建立私人学习档案：{data_dir}")
    print("当前三科均为未测量；先做高频考点轻量诊断，不能编造进度。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile, state = load_profile_and_state(args.data_dir, persist_pending=False)
    ensure_question_links_current(
        state, load_attempts(state_paths(args.data_dir)["attempts"])
    )
    payload = status_payload(profile, state)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"考试日期：{profile.get('exam_date') or '未设置'}")
    print(f"每日预算：{profile.get('daily_minutes')} 分钟")
    labels = {"comprehensive": "综合", "case": "案例", "essay": "论文"}
    for subject in SUBJECTS:
        item = payload["subjects"][subject]
        latest = item.get("latest_mock_score")
        lower = item.get("lower_bound_score")
        print(
            f"{labels[subject]}：{item['status']}；最近模考 "
            f"{latest if latest is not None else '未测'}；保守下界 "
            f"{lower if lower is not None else '未测'}；证据 {item['evidence_level']}"
        )
    if not state["topics"]:
        print("尚无有效作答记录，不能判断具体考点掌握度。")
    else:
        print(f"已有 {len(state['topics'])} 个考点产生学习证据。")
    return 0


def choose_subject_for_skill(topic: dict[str, Any], skill: str) -> str:
    preferred = {
        "recognition": "comprehensive",
        "application": "case",
        "production": "essay",
    }[skill]
    subjects = topic.get("subjects", [])
    return preferred if preferred in subjects else subjects[0]


def validate_record_event(event: dict[str, Any], curriculum: dict[str, Any]) -> None:
    topics = topic_map(curriculum)
    topic_id = event.get("topic_id")
    if topic_id not in topics:
        raise TutorError(f"未知稳定考点 ID：{topic_id}")
    topic = topics[topic_id]
    attempt_id = event.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise TutorError("attempt-id 必须是非空字符串")
    skill = event.get("skill")
    if skill not in topic.get("skills", []):
        raise TutorError(f"{topic_id} 不支持能力维度 {skill}")
    item_id = event.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise TutorError("item-id 必填，且必须稳定标识一道独立题目")
    score = event.get("score")
    maximum = event.get("max_score")
    if (
        not isinstance(score, (int, float))
        or not isinstance(maximum, (int, float))
        or isinstance(score, bool)
        or isinstance(maximum, bool)
        or not math.isfinite(float(score))
        or not math.isfinite(float(maximum))
        or maximum <= 0
        or score < 0
        or score > maximum
    ):
        raise TutorError("score 必须在 0 到 max-score 之间，且 max-score > 0")
    subject = event.get("subject")
    if subject not in topic.get("subjects", []):
        raise TutorError(f"考点 {topic_id} 不属于科目 {subject}")
    confidence = event.get("confidence")
    if confidence is None:
        # Only an explicit "不会" may omit confidence: it is negative evidence
        # about knowledge, not a guess about an option.
        if event.get("response_state") != "conceded":
            raise TutorError("confidence 无效")
    elif confidence not in ("guess", "unsure", "sure"):
        raise TutorError("confidence 无效")
    response_state = event.get("response_state")
    if response_state is not None and response_state not in ("answered", "conceded"):
        raise TutorError("response_state 无效")
    if event.get("mode") not in ("diagnostic", "practice", "review", "mock", "full_timed"):
        raise TutorError("mode 无效")
    if event.get("mode") == "full_timed" and skill != "production":
        raise TutorError("full_timed 仅用于完整论文，整科模考请使用 mock 命令")
    if event.get("source_type") not in (
        "official_outline",
        "real",
        "recalled_real",
        "self_authored",
        "simulation",
    ):
        raise TutorError("source_type 无效")
    duration = event.get("duration_seconds")
    if duration is not None and (
        not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0
    ):
        raise TutorError("duration-seconds 必须是正整数")
    word_count = event.get("word_count")
    if word_count is not None and (
        not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 0
    ):
        raise TutorError("word-count 必须是非负整数")
    parse_datetime(event.get("at"))
    reasons = event.get("wrong_reasons")
    if not isinstance(reasons, list):
        raise TutorError("wrong_reasons 必须是数组")
    invalid_reasons = set(reasons) - WRONG_REASONS
    if invalid_reasons:
        raise TutorError("未知错因：" + ", ".join(sorted(invalid_reasons)))
    wrong_reason_source = event.get("wrong_reason_source")
    if wrong_reason_source is not None and wrong_reason_source not in WRONG_REASON_SOURCES:
        raise TutorError("wrong_reason_source 无效")
    for key in (
        "question_fingerprint",
        "variant_of",
        "wrong_reason_source",
    ):
        value = event.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise TutorError(f"{key} 必须是非空字符串")
    if skill == "production" and event.get("mode") == "full_timed":
        if event.get("complete") is not True:
            raise TutorError("完整限时论文必须显式传入 --complete")
        duration = event.get("duration_seconds")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise TutorError("完整限时论文必须记录正整数 duration-seconds")
        word_count = event.get("word_count")
        if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 2500:
            raise TutorError("完整限时论文必须达到 2500 字并记录 word-count")
        if maximum != 75:
            raise TutorError("完整限时论文必须按官方 75 分制记录")


def apply_record_event(
    state: dict[str, Any], event: dict[str, Any], curriculum: dict[str, Any]
) -> dict[str, Any]:
    event = question_registry.canonicalize_public_event(event)
    validate_record_event(event, curriculum)
    attempt_id = event["attempt_id"]
    if attempt_id in set(state.get("applied_attempt_ids", [])):
        return {"already_applied": True}

    topic = topic_map(curriculum)[event["topic_id"]]
    attempted_at = parse_datetime(event["at"])
    attempted_iso = attempted_at.isoformat(timespec="seconds")
    attempted_date = attempted_at.date().isoformat()
    score = float(event["score"])
    maximum = float(event["max_score"])
    ratio = score / maximum
    skill = event["skill"]
    subject = event["subject"]

    topic_record = state["topics"].setdefault(
        event["topic_id"],
        {
            "topic_id": event["topic_id"],
            "name": topic["name"],
            "status": "unseen",
            "required_skills": topic.get("skills", []),
            "mastery": {},
            "wrong_reason_counts": {},
            "last_attempt_at": None,
            "next_review_at": None,
        },
    )
    record = topic_record["mastery"].setdefault(skill, new_skill_record())
    previous_status = record.get("status")
    record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
    record["score_sum"] = round(float(record.get("score_sum", 0)) + score, 4)
    record["max_score_sum"] = round(float(record.get("max_score_sum", 0)) + maximum, 4)
    attempted_items = set(record.get("attempted_items", []))
    attempted_items.add(event["item_id"])
    record["attempted_items"] = sorted(attempted_items)

    previous_last = record.get("last_attempt_at")
    previous_due = record.get("next_review_at")
    is_latest = not previous_last or attempted_at >= parse_datetime(previous_last)
    is_mastery_assessment = skill != "production" or event.get("mode") == "full_timed"
    if is_latest:
        record["last_attempt_at"] = attempted_iso

    safe_target = float(state.get("strategy", {}).get("safe_target", 52))
    threshold = {
        "recognition": 0.8,
        "application": 0.6,
        "production": safe_target / 75,
    }[skill]
    qualifies = (
        ratio >= threshold
        and event.get("confidence") != "guess"
        and (
            skill != "production"
            or (
                event.get("mode") == "full_timed"
                and event.get("complete") is True
                and int(event.get("word_count", 0)) >= 2500
                and int(event.get("duration_seconds", 0)) > 0
            )
        )
    )
    if is_latest and is_mastery_assessment:
        record["latest_qualified"] = qualifies
        record["latest_ratio"] = round(ratio, 4)
        record["latest_confidence"] = event.get("confidence")
        if previous_status == "pass_ready":
            record["ever_pass_ready"] = True
        if record.get("ever_pass_ready") and not qualifies:
            record["regression_active"] = True
            record["regressed_at"] = attempted_iso
            record["regressed_item_id"] = event["item_id"]
    if qualifies:
        evidence = record.setdefault("qualified_evidence", [])
        evidence.append(
            {
                "attempt_id": attempt_id,
                "item_id": event["item_id"],
                "at": attempted_iso,
                "score": score,
                "max_score": maximum,
                "ratio": ratio,
                "mode": event.get("mode"),
                "complete": event.get("complete", False),
                "duration_seconds": event.get("duration_seconds"),
                "word_count": event.get("word_count"),
                "question_fingerprint": event.get("question_fingerprint"),
                "variant_of": event.get("variant_of"),
            }
        )
        dates = set(record.get("successful_dates", []))
        dates.add(attempted_date)
        record["successful_dates"] = sorted(dates)
    if skill == "production" and event.get("mode") == "full_timed":
        record["full_timed_count"] = int(record.get("full_timed_count", 0)) + 1

    reasons = event.get("wrong_reasons", [])
    source = event.get("wrong_reason_source")
    legacy_inferred = (
        source is None
        and re.fullmatch(r"quiz-.+-(?:q|v)-\d+", str(event.get("attempt_id", "")))
        and reasons == ["concept_confusion"]
    )
    for reason in reasons:
        counts = record["wrong_reason_counts"]
        counts[reason] = int(counts.get(reason, 0)) + 1
        topic_counts = topic_record["wrong_reason_counts"]
        topic_counts[reason] = int(topic_counts.get(reason, 0)) + 1
        target_key = (
            "legacy_inferred_wrong_reason_counts"
            if legacy_inferred
            else "confirmed_wrong_reason_counts"
        )
        target_counts = record.setdefault(target_key, {})
        target_counts[reason] = int(target_counts.get(reason, 0)) + 1
    if ratio < 1.0 and not reasons:
        record["unclassified_wrong_count"] = int(
            record.get("unclassified_wrong_count", 0)
        ) + 1

    evidence_target = {"recognition": 6, "application": 2, "production": 1}[skill]
    evidence_factor = min(1.0, len(attempted_items) / evidence_target)
    lifetime_accuracy = record["score_sum"] / record["max_score_sum"]
    record["mastery"] = round(lifetime_accuracy * evidence_factor, 4)
    record["status"] = skill_status(
        skill, record, safe_target
    )
    if record["status"] == "pass_ready":
        record["ever_pass_ready"] = True
        record["regression_active"] = False

    stable_success = qualifies and event.get("confidence") == "sure"
    next_review = scheduled_review_date(
        previous_last,
        previous_due,
        attempted_at,
        stable_success=stable_success,
    )
    if is_latest and (is_mastery_assessment or not record.get("next_review_at")):
        record["next_review_at"] = next_review.isoformat()

    topic_last = topic_record.get("last_attempt_at")
    if not topic_last or attempted_at >= parse_datetime(topic_last):
        topic_record["last_attempt_at"] = attempted_iso
    topic_record["next_review_at"] = min(
        item.get("next_review_at")
        for item in topic_record["mastery"].values()
        if item.get("next_review_at")
    )
    refresh_topic_status(topic_record, topic.get("skills", []))

    subject_last = state["subjects"][subject].get("last_practiced_at")
    if not subject_last or attempted_at >= parse_datetime(subject_last):
        state["subjects"][subject]["last_practiced_at"] = attempted_iso
    state["subjects"][subject]["evidence_count"] = int(
        state["subjects"][subject].get("evidence_count", 0)
    ) + 1
    session_last = state.get("last_session_at")
    if not session_last or attempted_at >= parse_datetime(session_last):
        state["last_session_at"] = attempted_iso
    state["applied_attempt_ids"].append(attempt_id)
    minimum_interval = int(
        state.get("strategy", {}).get("min_review_interval_days", 0) or 0
    )
    return {
        "already_applied": False,
        "topic_status": topic_record["status"],
        "next_review_at": effective_review_date(
            record.get("next_review_at") or next_review.isoformat(),
            record.get("last_attempt_at"),
            minimum_interval,
            record.get("status"),
        ),
    }


# Enrichment keys added after the first deployed version. Events recorded
# before the keys existed store ``None``; a replay that now fills them in is
# still the same answer, so ``None`` on either side must stay compatible.
DERIVED_EVENT_KEYS = (
    "question_fingerprint",
    "variant_of",
)


def events_conflict(existing: dict[str, Any], candidate: dict[str, Any], *, compare_at: bool) -> bool:
    existing = question_registry.canonicalize_public_event(existing)
    candidate = question_registry.canonicalize_public_event(candidate)
    keys = (
        "event_type",
        "topic_id",
        "item_id",
        "subject",
        "skill",
        "mode",
        "score",
        "max_score",
        "duration_seconds",
        "word_count",
        "complete",
        "confidence",
        "wrong_reasons",
        "source_type",
        "source",
        "question_id",
        "selected_answer",
        "correct_answer",
    )
    if any(existing.get(key) != candidate.get(key) for key in keys):
        return True
    if any(
        existing.get(key) is not None
        and candidate.get(key) is not None
        and existing.get(key) != candidate.get(key)
        for key in DERIVED_EVENT_KEYS
    ):
        return True
    return compare_at and existing.get("at") != candidate.get("at")


def cmd_record(args: argparse.Namespace) -> int:
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    topic = topics.get(args.topic)
    if topic is None:
        raise TutorError(f"未知稳定考点 ID：{args.topic}")
    subject = args.subject or choose_subject_for_skill(topic, args.skill)
    private_registry = load_private_question_registry(args.data_dir)
    registered = private_registry.get(args.item_id)
    if args.item_id.startswith("self-authored/") and registered is None:
        raise TutorError(
            "自编题必须先用 register-question 登记题干和选项，"
            "再记录作答"
        )
    if registered is not None and registered.get("topic_id") != args.topic:
        raise TutorError(
            f"题目 {args.item_id} 已登记到 {registered.get('topic_id')}，"
            f"不能记录到 {args.topic}"
        )
    fingerprint = args.question_fingerprint or (registered or {}).get(
        "question_fingerprint"
    )
    variant_of = args.variant_of or (registered or {}).get("variant_of")
    if fingerprint:
        duplicate = next(
            (
                item_id
                for item_id, entry in private_registry.items()
                if entry.get("question_fingerprint") == fingerprint
                and item_id != args.item_id
            ),
            None,
        )
        if duplicate:
            raise TutorError(
                f"题目内容已登记为 {duplicate}；请复用原 item_id，不能换 ID 重复计证据"
            )
    event = {
        "attempt_id": args.attempt_id,
        "event_type": "practice",
        "topic_id": args.topic,
        "item_id": args.item_id,
        "at": parse_datetime(args.at).isoformat(timespec="seconds"),
        "subject": subject,
        "skill": args.skill,
        "mode": args.mode,
        "score": args.score,
        "max_score": args.max_score,
        "duration_seconds": args.duration_seconds,
        "word_count": args.word_count,
        "complete": args.complete,
        "confidence": args.confidence,
        "wrong_reasons": args.wrong_reason or [],
        "wrong_reason_source": "learner" if args.wrong_reason else None,
        "source_type": args.source_type,
        "source": args.source,
        "feedback_seen": False,
        "question_fingerprint": fingerprint,
        "variant_of": variant_of,
    }
    corrected = question_registry.canonicalize_public_event(event)
    if corrected["topic_id"] != event["topic_id"]:
        raise TutorError(
            f"题目 {event['item_id']} 已规范映射到 {corrected['topic_id']}，"
            "不能按旧考点记录"
        )
    event = corrected
    validate_record_event(event, curriculum)
    profile, state = load_profile_and_state(args.data_dir)
    paths = state_paths(args.data_dir)
    attempts = load_attempts(paths["attempts"])
    existing = next(
        (item for item in attempts if item["attempt_id"] == args.attempt_id), None
    )
    if existing is not None:
        if events_conflict(existing, event, compare_at=bool(args.at)):
            raise TutorError(f"attempt-id {args.attempt_id} 与已记录内容冲突")
        print(f"作答 {args.attempt_id} 已记录，本次幂等跳过。")
        return 0

    # attempts.jsonl is the write-ahead source of truth. If the process stops
    # after this replacement, the next command replays the pending event.
    write_attempts(paths["attempts"], [*attempts, event])
    result = apply_record_event(state, event, curriculum)
    save_state_bundle(args.data_dir, profile, state, backup=True)
    print(
        f"已记录 {args.attempt_id}：{args.topic}/{args.skill} "
        f"{args.score:g}/{args.max_score:g}，状态 {result['topic_status']}，"
        f"下次复习 {result['next_review_at']}。"
    )
    return 0


def recompute_mock_summary(subject: dict[str, Any]) -> None:
    scores = [float(item["score_75"]) for item in subject["mock_scores"][-3:]]
    count = len(subject["mock_scores"])
    latest = scores[-1]
    if count == 1:
        predicted = latest
        lower = max(0.0, latest - 5.0)
        evidence = "low"
    elif count == 2:
        predicted = statistics.mean(scores)
        lower = min(scores)
        evidence = "medium"
    else:
        predicted = statistics.median(scores)
        lower = min(scores)
        evidence = "high"
    subject["latest_mock_score"] = round(latest, 2)
    subject["predicted_score"] = round(predicted, 2)
    subject["lower_bound_score"] = round(lower, 2)
    subject["evidence_level"] = evidence


def validate_mock_event(event: dict[str, Any]) -> None:
    score = event.get("score")
    maximum = event.get("max_score")
    if (
        not isinstance(score, (int, float))
        or not isinstance(maximum, (int, float))
        or isinstance(score, bool)
        or isinstance(maximum, bool)
        or not math.isfinite(float(score))
        or not math.isfinite(float(maximum))
        or maximum <= 0
        or score < 0
        or score > maximum
    ):
        raise TutorError("score 必须在 0 到 max-score 之间，且 max-score > 0")
    if maximum != 75:
        raise TutorError("完整模考必须按官方 75 分制记录，不能把小测归一化成模考")
    if event.get("complete") is not True:
        raise TutorError("只有完整完成的同科试卷才能作为模考证据")
    duration = event.get("duration_seconds")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise TutorError("duration-minutes 必须大于 0")
    if event.get("subject") not in SUBJECTS:
        raise TutorError("模考科目无效")
    if not isinstance(event.get("attempt_id"), str) or not event["attempt_id"].strip():
        raise TutorError("mock-id 必填")
    if not isinstance(event.get("item_id"), str) or not event["item_id"]:
        raise TutorError("paper-id 必填")
    if event.get("source_type") not in (
        "real",
        "recalled_real",
        "self_authored",
        "simulation",
    ):
        raise TutorError("模考 source_type 无效")
    parse_datetime(event.get("at"))


def apply_mock_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    validate_mock_event(event)
    attempt_id = event["attempt_id"]
    if attempt_id in set(state.get("applied_attempt_ids", [])):
        return {"already_applied": True}
    measured_at = parse_datetime(event["at"]).isoformat(timespec="seconds")
    subject_name = event["subject"]
    score = float(event["score"])
    maximum = float(event["max_score"])
    score_75 = score / maximum * 75
    subject = state["subjects"][subject_name]
    subject["mock_scores"].append(
        {
            "mock_id": attempt_id,
            "paper_id": event["item_id"],
            "at": measured_at,
            "score": score,
            "max_score": maximum,
            "score_75": round(score_75, 2),
            "duration_minutes": round(int(event["duration_seconds"]) / 60, 2),
            "complete": True,
        }
    )
    subject["mock_scores"].sort(key=lambda item: item["at"])
    previous_last = subject.get("last_practiced_at")
    if not previous_last or parse_datetime(measured_at) >= parse_datetime(previous_last):
        subject["last_practiced_at"] = measured_at
    subject["evidence_count"] = int(subject.get("evidence_count", 0)) + 1
    recompute_mock_summary(subject)
    previous_session = state.get("last_session_at")
    if not previous_session or parse_datetime(measured_at) >= parse_datetime(previous_session):
        state["last_session_at"] = measured_at
    state["applied_attempt_ids"].append(attempt_id)
    return {
        "already_applied": False,
        "lower_bound_score": subject["lower_bound_score"],
        "evidence_level": subject["evidence_level"],
    }


def apply_event_to_state(
    state: dict[str, Any], event: dict[str, Any], curriculum: dict[str, Any]
) -> dict[str, Any]:
    if event.get("event_type") == "mock" or event.get("mode") == "full_mock":
        return apply_mock_event(state, event)
    return apply_record_event(state, event, curriculum)


def cmd_mock(args: argparse.Namespace) -> int:
    if not math.isfinite(args.duration_minutes) or args.duration_minutes <= 0:
        raise TutorError("duration-minutes 必须是大于 0 的有限数")
    measured_at = parse_datetime(args.at).isoformat(timespec="seconds")
    event = {
        "attempt_id": args.mock_id,
        "event_type": "mock",
        "topic_id": None,
        "item_id": args.paper_id,
        "at": measured_at,
        "subject": args.subject,
        "skill": {
            "comprehensive": "recognition",
            "case": "application",
            "essay": "production",
        }[args.subject],
        "mode": "full_mock",
        "score": args.score,
        "max_score": args.max_score,
        "duration_seconds": round(args.duration_minutes * 60),
        "word_count": None,
        "confidence": "sure",
        "wrong_reasons": [],
        "source_type": args.source_type,
        "source": args.paper_id,
        "complete": args.complete,
        "feedback_seen": False,
    }
    validate_mock_event(event)
    profile, state = load_profile_and_state(args.data_dir)
    paths = state_paths(args.data_dir)
    attempts = load_attempts(paths["attempts"])
    logged_event = next(
        (item for item in attempts if item.get("attempt_id") == args.mock_id), None
    )
    if logged_event is not None:
        if events_conflict(logged_event, event, compare_at=bool(args.at)):
            raise TutorError(f"mock-id {args.mock_id} 与已记录内容冲突")
        print(f"模考 {args.mock_id} 已记录，本次幂等跳过。")
        return 0

    write_attempts(paths["attempts"], [*attempts, event])
    result = apply_mock_event(state, event)
    save_state_bundle(args.data_dir, profile, state, backup=True)
    print(
        f"已记录模考 {args.mock_id}：{args.subject} {args.score:g}/{args.max_score:g}；"
        f"保守下界 {result['lower_bound_score']:g}/75，证据 {result['evidence_level']}。"
    )
    return 0


def subject_allocations(state: dict[str, Any], today: date) -> dict[str, float]:
    safe_target = float(state.get("strategy", {}).get("safe_target", 52))
    any_practice = any(
        state["subjects"][subject].get("last_practiced_at") for subject in SUBJECTS
    )
    raw: dict[str, float] = {}
    for subject in SUBJECTS:
        subject_state = state["subjects"][subject]
        lower = subject_state.get("lower_bound_score")
        if lower is None:
            evidence_count = int(subject_state.get("evidence_count", 0))
            raw[subject] = 1.2 + 0.4 / (1 + evidence_count)
        else:
            raw[subject] = max(0.2, min(2.0, (safe_target - float(lower)) / 10 + 0.2))
        last_practiced = subject_state.get("last_practiced_at")
        if any_practice and not last_practiced:
            raw[subject] += 0.3
        elif last_practiced:
            age_days = max(0, (today - parse_datetime(last_practiced).date()).days)
            if age_days >= 3:
                raw[subject] += 0.2
    total = sum(raw.values())
    return {subject: round(raw[subject] / total, 4) for subject in SUBJECTS}


def effective_subject_allocations(
    state: dict[str, Any], today: date
) -> tuple[dict[str, float], dict[str, float]]:
    """Return raw need and the actionable allocation after policy filtering."""

    raw = subject_allocations(state, today)
    paused = manual_trigger_subjects(state)
    active = [subject for subject in SUBJECTS if subject not in paused]
    if not active:
        return raw, raw
    if active == ["comprehensive", "case"]:
        return raw, {"comprehensive": 0.5, "case": 0.5, "essay": 0.0}
    total = sum(raw[subject] for subject in active)
    effective = {
        subject: (
            round(raw[subject] / total, 4)
            if subject in active and total > 0
            else 0.0
        )
        for subject in SUBJECTS
    }
    return raw, effective


def case_track_by_supporting_topic(
    curriculum: dict[str, Any],
) -> dict[str, str]:
    """Map fine-grained knowledge topics to their canonical case track."""

    result: dict[str, str] = {}
    for topic in curriculum.get("topics", []):
        if not str(topic.get("id", "")).startswith("C"):
            continue
        for topic_id in topic.get("covered_topic_ids", []):
            result[topic_id] = topic["id"]
    return result


def topic_mastery(state: dict[str, Any], topic_id: str, skill: str) -> float:
    record = state.get("topics", {}).get(topic_id)
    if not record:
        return 0.0
    skill_record = record.get("mastery", {}).get(skill)
    if not isinstance(skill_record, dict):
        return 0.0
    if skill_record.get("status") == "pass_ready":
        # Once the evidence gate is met, this track moves to maintenance even
        # when its rubric threshold is intentionally only 60% (case) or 52/75
        # (essay). Raw accuracy remains visible in state; this value is solely
        # the scheduling need signal.
        return 1.0
    return float(skill_record.get("mastery", 0))


def case_application_review_due(state: dict[str, Any], today: date) -> bool:
    """Only urgent, non-pass-ready case reviews override the subject split."""

    strategy = state.get("strategy", {})
    configured = set(strategy.get("case_tracks", []))
    skipped = set(strategy.get("strategic_skips", {}))
    actionable_topics: set[str] = set()
    for track in load_curriculum().get("topics", []):
        track_id = str(track.get("id", ""))
        if not track_id.startswith("C") or track_id in skipped:
            continue
        if strategy.get("case_tracks_configured") and track_id not in configured:
            continue
        actionable_topics.add(track_id)
        actionable_topics.update(
            topic_id
            for topic_id in track.get("covered_topic_ids", [])
            if topic_id not in skipped
        )
    minimum_interval = int(
        strategy.get("min_review_interval_days", 0) or 0
    )
    for topic_id, topic in state.get("topics", {}).items():
        if topic_id not in actionable_topics:
            continue
        application = (topic.get("mastery") or {}).get("application")
        if not isinstance(application, dict):
            continue
        if application.get("status") == "pass_ready":
            continue
        review_at = application.get("next_review_at")
        if review_at and parse_date(
            effective_review_date(
                review_at,
                application.get("last_attempt_at"),
                minimum_interval,
                application.get("status"),
            )
        ) <= today:
            return True
    return False


def select_target_subject(
    state: dict[str, Any], allocations: dict[str, float], today: date
) -> str:
    paused = manual_trigger_subjects(state)
    if "case" not in paused and case_application_review_due(state, today):
        return "case"
    critical = [
        subject
        for subject in SUBJECTS
        if subject not in paused
        and (
            state["subjects"][subject].get("lower_bound_score") is None
            or float(state["subjects"][subject]["lower_bound_score"]) < 45
        )
    ]
    candidates = critical or [subject for subject in SUBJECTS if subject not in paused]
    # A learner may pause every subject on purpose; recommending something is
    # still better than failing, and the pause only suppresses auto-selection.
    candidates = candidates or list(SUBJECTS)
    return max(
        candidates,
        key=lambda subject: (allocations[subject], -SUBJECTS.index(subject)),
    )


def manual_trigger_subjects(state: dict[str, Any]) -> set[str]:
    """Subjects the learner only wants to train on an explicit request."""

    policies = state.get("strategy", {}).get("subject_policies", {})
    if not isinstance(policies, dict):
        return set()
    return {
        subject
        for subject, policy in policies.items()
        if isinstance(policy, dict) and policy.get("mode") == "manual_trigger"
    }


def next_training_action(
    state: dict[str, Any],
    today: date,
    *,
    quiz_id: str | None = None,
    variant_count: int = 0,
) -> dict[str, Any]:
    """Return one deterministic next route without changing learner state."""

    if variant_count:
        return {
            "mode": "await_variants",
            "subject": "comprehensive",
            "quiz_id": quiz_id,
            "variant_count": variant_count,
            "command": "quiz-variant-grade",
            "user_override_allowed": True,
        }
    _, allocations = effective_subject_allocations(state, today)
    subject = select_target_subject(state, allocations, today)
    if subject == "comprehensive":
        mode = "quiz_prepare"
        command = "quiz-prepare --subject comprehensive"
    elif subject == "case":
        mode = "case_prepare"
        command = "case-prepare"
    else:
        mode = "essay_manual_flow"
        command = "paper_practice --subject essay"
    return {
        "mode": mode,
        "subject": subject,
        "command": command,
        "subject_allocation": allocations,
        "decision_source": "existing_subject_allocator",
        "user_override_allowed": True,
    }


def select_maintenance_subject(
    state: dict[str, Any], target_subject: str, today: date
) -> str | None:
    paused = manual_trigger_subjects(state)
    overdue: list[tuple[int, str]] = []
    for subject in SUBJECTS:
        if subject == target_subject or subject in paused:
            continue
        last_practiced = state["subjects"][subject].get("last_practiced_at")
        if not last_practiced:
            continue
        age_days = max(0, (today - parse_datetime(last_practiced).date()).days)
        if age_days >= 3:
            overdue.append((age_days, subject))
    if not overdue:
        return None
    return max(overdue, key=lambda item: (item[0], -SUBJECTS.index(item[1])))[1]


def load_private_question_registry(data_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        return question_registry.load_registry(question_registry.registry_path(data_dir))
    except ValueError as error:
        raise TutorError(str(error)) from error


def load_postmortems(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "postmortems.jsonl"
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise TutorError(
                f"postmortems.jsonl 第 {line_number} 行损坏，拒绝用于诊断"
            ) from error
        if not isinstance(item, dict) or not isinstance(item.get("mock_id"), str):
            raise TutorError(f"postmortems.jsonl 第 {line_number} 行无效")
        result.append(item)
    return result


def _event_ratio(event: dict[str, Any]) -> float:
    maximum = float(event.get("max_score", 0) or 0)
    return float(event.get("score", 0) or 0) / maximum if maximum > 0 else 0.0


def learning_diagnosis(
    data_dir: Path,
    subject: str,
    today: date,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize mock gaps and later practice by stable topic and skill."""

    if subject not in SUBJECTS:
        raise TutorError(f"未知科目：{subject}")
    if state is None:
        _, state = load_profile_and_state(data_dir, persist_pending=False)
    attempts = load_attempts(state_paths(data_dir)["attempts"])
    mock_events = sorted(
        (
            event for event in attempts
            if event.get("event_type") == "mock" and event.get("subject") == subject
        ),
        key=lambda event: parse_datetime(event.get("at")),
    )
    if not mock_events:
        return {"subject": subject, "mock": None, "issues": []}
    mock = mock_events[-1]
    postmortems = {item["mock_id"]: item for item in load_postmortems(data_dir)}
    topics = topic_map(load_curriculum())
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for source_mock in mock_events:
        mock_id = source_mock["attempt_id"]
        prefix = f"{mock_id}-q-"
        question_events = sorted(
            (
                event for event in attempts
                if event.get("event_type") == "practice"
                and str(event.get("attempt_id", "")).startswith(prefix)
            ),
            key=lambda event: str(event.get("attempt_id")),
        )
        reasons_by_question = {
            item.get("question_id"): item.get("wrong_reason")
            for item in postmortems.get(mock_id, {}).get("items", [])
            if isinstance(item, dict)
        }
        for original in question_events:
            event = question_registry.canonicalize_public_event(original)
            wrong = _event_ratio(event) < 1.0
            uncertain = event.get("confidence") in {"guess", "unsure"}
            if not wrong and not uncertain:
                continue
            topic_id = event.get("topic_id")
            skill = event.get("skill")
            if topic_id not in topics or skill not in SKILLS:
                continue
            issue = grouped.setdefault(
                (topic_id, skill),
                {
                    "topic_id": topic_id,
                    "topic_name": topics[topic_id]["name"],
                    "subject": subject,
                    "skill": skill,
                    "source_mock_id": mock_id,
                    "source_mock_ids": [],
                    "source_paper_id": source_mock.get("item_id"),
                    "source_item_ids": [],
                    "source_question_ids": [],
                    "wrong_reasons": [],
                    "signal": "wrong" if wrong else "uncertain",
                    "source_at": source_mock["at"],
                },
            )
            if mock_id not in issue["source_mock_ids"]:
                issue["source_mock_ids"].append(mock_id)
            if event.get("item_id") not in issue["source_item_ids"]:
                issue["source_item_ids"].append(event.get("item_id"))
            if event.get("question_id") and event["question_id"] not in issue["source_question_ids"]:
                issue["source_question_ids"].append(event["question_id"])
            reason = reasons_by_question.get(event.get("question_id"))
            if reason and reason not in issue["wrong_reasons"]:
                issue["wrong_reasons"].append(reason)
            if parse_datetime(source_mock["at"]) >= parse_datetime(issue["source_at"]):
                issue["source_at"] = source_mock["at"]
                issue["source_mock_id"] = mock_id
                issue["source_paper_id"] = source_mock.get("item_id")
            if wrong:
                issue["signal"] = "wrong"

    strong_by_topic: dict[tuple[str, str], list[tuple[datetime, dict[str, Any]]]] = {}
    for original in attempts:
        event = question_registry.canonicalize_public_event(original)
        if (
            event.get("event_type") != "practice"
            or event.get("subject") != subject
            or _event_ratio(event) < 0.8
            or event.get("confidence") != "sure"
        ):
            continue
        key = (event.get("topic_id"), event.get("skill"))
        if key in grouped:
            strong_by_topic.setdefault(key, []).append((parse_datetime(event["at"]), event))
    for key, issue in grouped.items():
        source_items = set(issue["source_item_ids"])
        source_at = parse_datetime(issue["source_at"])
        remedies = [
            {
                "attempt_id": event.get("attempt_id"),
                "item_id": event.get("item_id"),
                "at": event.get("at"),
            }
            for attempted_at, event in strong_by_topic.get(key, [])
            if attempted_at > source_at and event.get("item_id") not in source_items
        ]
        distinct_items = {item["item_id"] for item in remedies if item.get("item_id")}
        dates = {
            parse_datetime(item["at"]).date().isoformat()
            for item in remedies if item.get("at")
        }
        verified = len(distinct_items) >= 2 and len(dates) >= 2
        last_remedy_date = max((parse_date(value) for value in dates), default=None)
        review_date = last_remedy_date + timedelta(days=1) if last_remedy_date else today
        if verified:
            status = "verified"
        elif remedies and review_date <= today:
            status = "due_review"
        elif remedies:
            status = "awaiting_review"
        else:
            status = "pending_remediation"
        issue["status"] = status
        issue["remedy_attempts"] = remedies
        issue["next_review_at"] = review_date.isoformat()
        issue["avoid_item_ids"] = sorted(source_items | distinct_items)
        issue["reason"] = {
            "pending_remediation": "模考该考点有失分，优先安排新题练习",
            "due_review": "该考点已到跨日复测日",
            "awaiting_review": "该考点等待跨日复测",
            "verified": "该考点已有不同题目和不同日期的确定作答证据",
        }[status]

    priority = {
        "pending_remediation": 0,
        "due_review": 1,
        "awaiting_review": 2,
        "verified": 3,
    }
    issues = sorted(
        grouped.values(),
        key=lambda item: (
            priority[item["status"]],
            0 if item["signal"] == "wrong" else 1,
            item["topic_id"],
        ),
    )
    return {
        "subject": subject,
        "mock": {
            "mock_id": mock["attempt_id"],
            "paper_id": mock.get("item_id"),
            "at": mock.get("at"),
            "score": mock.get("score"),
            "max_score": mock.get("max_score"),
        },
        "issues": issues,
    }


def cmd_diagnose(args: argparse.Namespace) -> int:
    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    payload = learning_diagnosis(args.data_dir, args.subject, today)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if payload["mock"] is None:
        print(f"{args.subject} 尚无完整模考，暂无逐题诊断。")
        return 0
    print(
        f"最近模考：{payload['mock']['score']:g}/{payload['mock']['max_score']:g} "
        f"({payload['mock']['at']})"
    )
    for index, issue in enumerate(payload["issues"], 1):
        print(
            f"{index}. {issue['topic_name']} [{issue['status']}] — {issue['reason']}"
        )
    return 0


def weakpoint_action(row: dict[str, Any]) -> str:
    """Pick one deterministic next action for a weakpoint row."""

    if int(row.get("attempt_count", 0)) == 0:
        return "未覆盖：先做一次诊断"
    overdue = row.get("overdue_days")
    if overdue is not None and overdue >= 0:
        return f"到期复测（逾期 {overdue} 天）" if overdue else "今天到期：复测"
    accuracy = row.get("recent_accuracy")
    if accuracy is not None and int(row.get("recent_attempts", 0)) >= 3 and accuracy < 0.6:
        return "近期正确率偏低：定向补练"
    if int(row.get("guess_correct", 0)) or int(row.get("unsure_correct", 0)):
        return "有蒙对/不确定：加一道变式确认"
    if row.get("status") == "pass_ready":
        return "已达标：维持低频复测"
    return "继续练习"


def weakpoints_payload(
    data_dir: Path,
    subject: str,
    today: date,
    *,
    days: int = 21,
    limit: int = 10,
) -> dict[str, Any]:
    """Rank one subject's overdue, recently weak and uncovered topics.

    Read-only by contract: it loads curriculum, state and the attempt log and
    never writes to ``.study/``, so it is safe to call inside a training round.
    """

    if subject not in SUBJECTS:
        raise TutorError(f"未知科目：{subject}")
    if days <= 0:
        raise TutorError("days 必须大于 0")
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    _, state = load_profile_and_state(data_dir, persist_pending=False)
    attempts = load_attempts(state_paths(data_dir)["attempts"])
    ensure_question_links_current(state, attempts)
    minimum_interval = int(
        state.get("strategy", {}).get("min_review_interval_days", 0) or 0
    )
    cutoff = today - timedelta(days=days)

    window: dict[tuple[str, str], dict[str, Any]] = {}
    for event in attempts:
        if event.get("event_type") != "practice" or event.get("subject") != subject:
            continue
        event = question_registry.canonicalize_public_event(event)
        topic_id = event.get("topic_id")
        skill = event.get("skill")
        if not topic_id or not skill or not event.get("at"):
            continue
        attempted_on = parse_datetime(event["at"]).date()
        if attempted_on < cutoff or attempted_on > today:
            continue
        bucket = window.setdefault(
            (topic_id, skill),
            {
                "recent_attempts": 0,
                "recent_score": 0.0,
                "recent_max_score": 0.0,
                "guess_correct": 0,
                "unsure_correct": 0,
            },
        )
        bucket["recent_attempts"] += 1
        bucket["recent_score"] += float(event.get("score", 0) or 0)
        bucket["recent_max_score"] += float(event.get("max_score", 0) or 0)
        if _event_ratio(event) >= 1.0:
            if event.get("confidence") == "guess":
                bucket["guess_correct"] += 1
            elif event.get("confidence") == "unsure":
                bucket["unsure_correct"] += 1

    rows: list[dict[str, Any]] = []
    for topic_id, topic in topics.items():
        topic_record = state.get("topics", {}).get(topic_id)
        mastery = (
            topic_record.get("mastery")
            if isinstance(topic_record, dict)
            and isinstance(topic_record.get("mastery"), dict)
            else {}
        )
        for skill in topic.get("skills", []):
            if choose_subject_for_skill(topic, skill) != subject:
                continue
            record = mastery.get(skill)
            record = record if isinstance(record, dict) else {}
            bucket = window.get((topic_id, skill)) or {}
            recent_attempts = int(bucket.get("recent_attempts", 0))
            recent_score = float(bucket.get("recent_score", 0) or 0)
            recent_max_score = float(bucket.get("recent_max_score", 0) or 0)
            last_attempt_at = record.get("last_attempt_at")
            status = str(record.get("status") or "unseen")
            next_review_at = effective_review_date(
                record.get("next_review_at"),
                last_attempt_at,
                minimum_interval,
                status,
            )
            row = {
                "topic_id": topic_id,
                "topic_name": topic.get("name") or topic_id,
                "skill": skill,
                "status": status,
                "mastery": round(float(record.get("mastery", 0.0) or 0.0), 4),
                "attempt_count": int(record.get("attempt_count", 0) or 0),
                "recent_attempts": recent_attempts,
                "recent_accuracy": (
                    round(recent_score / recent_max_score, 4)
                    if recent_max_score > 0
                    else None
                ),
                "recent_score": round(recent_score, 2),
                "recent_max_score": round(recent_max_score, 2),
                "guess_correct": int(bucket.get("guess_correct", 0)),
                "unsure_correct": int(bucket.get("unsure_correct", 0)),
                "last_attempt_at": last_attempt_at,
                "next_review_at": next_review_at,
                "overdue_days": (
                    (today - parse_date(next_review_at)).days
                    if next_review_at
                    else None
                ),
                "frequency_count": topic.get("frequency_count"),
                "priority_weight": topic.get("priority_weight"),
            }
            row["action"] = weakpoint_action(row)
            rows.append(row)

    due = sorted(
        (
            row
            for row in rows
            if row["overdue_days"] is not None and row["overdue_days"] >= 0
        ),
        key=lambda row: (-row["overdue_days"], row["mastery"], row["topic_id"]),
    )
    recent = sorted(
        (row for row in rows if row["recent_attempts"] > 0),
        key=lambda row: (
            row["recent_accuracy"] if row["recent_accuracy"] is not None else 1.0,
            -row["recent_attempts"],
            row["topic_id"],
        ),
    )
    uncovered = sorted(
        (row for row in rows if row["attempt_count"] == 0),
        key=lambda row: (
            -float(row.get("priority_weight") or 0),
            -float(row.get("frequency_count") or 0),
            row["topic_id"],
        ),
    )
    return {
        "subject": subject,
        "subject_policy": state.get("strategy", {})
        .get("subject_policies", {})
        .get(subject),
        "today": today.isoformat(),
        "days": days,
        "due": due[:limit],
        "recent": recent[:limit],
        "uncovered": uncovered[:limit],
        "counts": {
            "due": len(due),
            "recent": len(recent),
            "uncovered": len(uncovered),
        },
    }


def display_width(text: str) -> int:
    """Terminal width of one cell, counting CJK glyphs as two columns."""

    return sum(
        2 if unicodedata.east_asian_width(character) in "WF" else 1
        for character in text
    )


def render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))

    def render(cells: list[str]) -> str:
        padded = [
            cell + " " * max(0, widths[index] - display_width(cell))
            for index, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()

    lines = [render(headers), "  ".join("-" * width for width in widths)]
    lines.extend(render(row) for row in rows)
    return lines


def cmd_weakpoints(args: argparse.Namespace) -> int:
    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    payload = weakpoints_payload(
        args.data_dir, args.subject, today, days=args.days, limit=args.limit
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    def row_cells(row: dict[str, Any]) -> list[str]:
        accuracy = row["recent_accuracy"]
        guessed = f"{row['guess_correct']}/{row['unsure_correct']}"
        return [
            row["topic_id"],
            row["topic_name"],
            f"{accuracy * 100:.0f}%" if accuracy is not None else "-",
            str(row["recent_attempts"]),
            guessed,
            str(row["last_attempt_at"])[:10] if row["last_attempt_at"] else "-",
            str(row["overdue_days"]) if row["overdue_days"] is not None else "-",
            f"{row['mastery']:.2f}",
            row["action"],
        ]

    headers = [
        "考点 ID",
        "考点",
        "近期正确率",
        "样本",
        "蒙对/不确定",
        "最近作答",
        "逾期(天)",
        "掌握度",
        "建议动作",
    ]
    counts = payload["counts"]
    policy = payload.get("subject_policy")
    print(
        f"{payload['subject']} 薄弱点（近 {payload['days']} 天，截至 {payload['today']}；只读，不写档）"
    )
    if isinstance(policy, dict) and policy.get("mode") == "manual_trigger":
        print(
            "注意：该科已设为 manual_trigger，只有考生明确要求时才训练（"
            + str(policy.get("reason") or "")
            + "）"
        )
    for title, key in (
        ("到期与逾期", "due"),
        ("近期正确率（低→高）", "recent"),
        ("未覆盖考点", "uncovered"),
    ):
        rows = payload[key]
        print(f"\n[{title}] 共 {counts[key]} 项，显示 {len(rows)} 项")
        if not rows:
            print("  （无）")
            continue
        print("\n".join(render_table(headers, [row_cells(row) for row in rows])))
    return 0


def build_progress_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Build one compact, read-only coaching overview."""

    profile, state = load_profile_and_state(args.data_dir, persist_pending=False)
    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    status = status_payload(profile, state)
    raw_allocations, allocations = effective_subject_allocations(state, today)
    paused = manual_trigger_subjects(state)
    next_action = next_training_action(state, today)
    recommendation_args = argparse.Namespace(
        data_dir=args.data_dir,
        subject=next_action["subject"],
        limit=max(args.limit, len(load_curriculum()["topics"])),
        today=today.isoformat(),
    )
    plan = build_recommendation_payload(recommendation_args)

    per_subject: dict[str, Any] = {}
    active_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    due_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for subject in SUBJECTS:
        weakpoints = weakpoints_payload(
            args.data_dir,
            subject,
            today,
            days=args.days,
            limit=max(args.limit * 4, 20),
        )
        per_subject[subject] = {
            "counts": weakpoints["counts"],
            "subject_policy": weakpoints.get("subject_policy"),
        }
        for section in ("due", "recent", "uncovered"):
            for row in weakpoints[section]:
                enriched = {**row, "subject": subject}
                key = (subject, row["topic_id"], row["skill"])
                if subject not in paused:
                    active_rows.setdefault(key, enriched)
                if section == "due" and subject not in paused:
                    due_rows.setdefault(key, enriched)

    def weakpoint_key(row: dict[str, Any]) -> tuple[Any, ...]:
        overdue = row.get("overdue_days")
        due_rank = 0 if overdue is not None and overdue >= 0 else 1
        accuracy = row.get("recent_accuracy")
        return (
            due_rank,
            -(overdue or 0) if due_rank == 0 else 0,
            accuracy if accuracy is not None else 1.1,
            row.get("mastery", 0.0),
            -float(row.get("frequency_count") or 0),
            row["topic_id"],
        )

    recommendations = plan.get("recommendations") or []
    if recommendations:
        if next_action["mode"] == "quiz_prepare":
            curriculum = load_curriculum()
            selected, selected_recommendations = select_quiz_group(
                args.data_dir,
                today,
                5,
                recommendations,
                curriculum,
                topic_map(curriculum),
            )
            first = selected_recommendations[0]
            topic_id = selected[0]["topic_id"]
        else:
            first = recommendations[0]
            topic_id = first["topic_id"]
        command = next_action["command"]
        if next_action["mode"] in {"quiz_prepare", "case_prepare"}:
            command = f"{command} --topic {topic_id}"
        next_action = {
            **next_action,
            "topic_id": topic_id,
            "topic_name": first.get("name"),
            "skill": first.get("skill"),
            "reason": first.get("reason"),
            "estimated_minutes": first.get("estimated_minutes"),
            "command": command,
        }
    exam_date = profile.get("exam_date")
    return {
        "today": today.isoformat(),
        "profile": {
            "exam_date": exam_date,
            "days_left": (
                (parse_date(exam_date) - today).days if exam_date else None
            ),
            "daily_minutes": profile.get("daily_minutes"),
        },
        "pass_line": state.get("strategy", {}).get("pass_line", 45),
        "safe_target": state.get("strategy", {}).get("safe_target", 52),
        "subjects": status["subjects"],
        "focus_subject": plan["target_subject"],
        "subject_allocation": allocations,
        "raw_subject_allocation": raw_allocations,
        "suppressed_subjects": [
            {
                "subject": subject,
                "policy": state.get("strategy", {})
                .get("subject_policies", {})
                .get(subject),
                "status": status["subjects"][subject]["status"],
            }
            for subject in SUBJECTS
            if subject in paused
        ],
        "weakpoints": sorted(active_rows.values(), key=weakpoint_key)[: args.limit],
        "due_reviews": sorted(due_rows.values(), key=weakpoint_key)[: args.limit],
        "weakpoint_counts": per_subject,
        "next_action": next_action,
    }


def runtime_progress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the routing facts needed to start one teaching round."""

    next_action = payload["next_action"]
    subject = next_action["subject"]
    return {
        "today": payload["today"],
        "profile": payload["profile"],
        "focus_subject": payload["focus_subject"],
        "subject_allocation": payload["subject_allocation"],
        "suppressed_subjects": payload["suppressed_subjects"],
        "subject_status": {
            "subject": subject,
            **payload["subjects"][subject],
        },
        "next_action": next_action,
    }


def cmd_progress(args: argparse.Namespace) -> int:
    payload = build_progress_payload(args)
    if args.runtime_json:
        print(
            json.dumps(
                runtime_progress_payload(payload), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return 0
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(
        f"距离考试 {payload['profile']['days_left'] if payload['profile']['days_left'] is not None else '未知'} 天；"
        f"每日预算 {payload['profile']['daily_minutes']} 分钟"
    )
    labels = {"comprehensive": "综合", "case": "案例", "essay": "论文"}
    for subject in SUBJECTS:
        item = payload["subjects"][subject]
        print(
            f"{labels[subject]}：{item['status']}；保守下界 "
            f"{item.get('lower_bound_score') if item.get('lower_bound_score') is not None else '未测'}；"
            f"证据 {item['evidence_level']}"
        )
    if payload["suppressed_subjects"]:
        print(
            "仅主动触发："
            + "、".join(
                labels[item["subject"]] for item in payload["suppressed_subjects"]
            )
        )
    print("薄弱 Top：")
    for index, row in enumerate(payload["weakpoints"], 1):
        print(
            f"{index}. [{labels[row['subject']]}] {row['topic_id']} "
            f"{row['topic_name']} — {row['action']}"
        )
    action = payload["next_action"]
    print(
        f"下一步：[{labels[action['subject']]}] "
        f"{action.get('topic_name') or action.get('topic_id') or action['command']}"
    )
    return 0


def cmd_register_question(args: argparse.Namespace) -> int:
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    try:
        payload = json.loads(args.file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TutorError(f"题目登记文件不存在：{args.file}") from error
    except json.JSONDecodeError as error:
        raise TutorError(f"题目登记文件不是有效 JSON：{args.file}") from error
    raw_entries = payload if isinstance(payload, list) else [payload]
    path = question_registry.registry_path(args.data_dir)
    existing = load_private_question_registry(args.data_dir)
    fingerprints = {
        entry.get("question_fingerprint"): item_id
        for item_id, entry in existing.items()
        if entry.get("question_fingerprint")
    }
    changed = False
    for raw in raw_entries:
        try:
            entry = question_registry.validate_entry(raw)
        except ValueError as error:
            raise TutorError(str(error)) from error
        if entry["topic_id"] not in topics:
            raise TutorError(f"未知稳定考点 ID：{entry['topic_id']}")
        previous = existing.get(entry["item_id"])
        if previous is not None:
            if previous != entry:
                raise TutorError(f"题目 {entry['item_id']} 已登记且内容不同")
            continue
        duplicate = fingerprints.get(entry["question_fingerprint"])
        if duplicate:
            raise TutorError(
                f"题目内容已登记为 {duplicate}；请复用原 item_id，不能换 ID 重复计证据"
            )
        existing[entry["item_id"]] = entry
        fingerprints[entry["question_fingerprint"]] = entry["item_id"]
        changed = True
    if changed:
        atomic_write_text(path, question_registry.serialize_registry(existing.values()))
    print(f"已登记 {len(raw_entries)} 道题；私人题目登记共 {len(existing)} 道。")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    if (
        args.case_track is None
        and args.essay_theme is None
        and args.skip_topic is None
        and args.unskip_topic is None
        and args.min_review_interval_days is None
        and args.subject_policy is None
    ):
        raise TutorError(
            "至少提供路线选项或 --skip-topic TOPIC=原因 / --unskip-topic TOPIC"
            " / --subject-policy 科目=模式"
        )
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    profile, state = load_profile_and_state(args.data_dir)
    if args.case_track is not None:
        tracks = list(dict.fromkeys(args.case_track))
        if not 1 <= len(tracks) <= 3:
            raise TutorError("案例主赛道请选择 1–3 个")
        invalid = [
            topic_id
            for topic_id in tracks
            if topic_id not in topics
            or "case" not in topics[topic_id].get("subjects", [])
            or not topic_id.startswith("C")
        ]
        if invalid:
            raise TutorError("无效案例赛道：" + ", ".join(invalid))
        state["strategy"]["case_tracks"] = tracks
        state["strategy"]["case_tracks_configured"] = True
    if args.essay_theme is not None:
        themes = list(dict.fromkeys(args.essay_theme))
        if not 1 <= len(themes) <= 3:
            raise TutorError("论文主题请选择 1–3 个")
        invalid = [
            topic_id
            for topic_id in themes
            if topic_id not in topics
            or "essay" not in topics[topic_id].get("subjects", [])
            or not topic_id.startswith("P")
        ]
        if invalid:
            raise TutorError("无效论文主题：" + ", ".join(invalid))
        state["strategy"]["essay_themes"] = themes
        state["strategy"]["essay_themes_configured"] = True
    skips = state["strategy"].setdefault("strategic_skips", {})
    if not isinstance(skips, dict):
        raise TutorError("state.json strategic_skips 无效")
    for specification in args.skip_topic or []:
        topic_id, separator, reason = specification.partition("=")
        topic_id = topic_id.strip()
        reason = reason.strip()
        if not separator or topic_id not in topics or not reason:
            raise TutorError("skip-topic 必须使用 TOPIC_ID=原因，且考点必须存在")
        skips[topic_id] = reason
    for topic_id in args.unskip_topic or []:
        if topic_id not in topics:
            raise TutorError(f"未知稳定考点 ID：{topic_id}")
        skips.pop(topic_id, None)
    if args.min_review_interval_days is not None:
        if args.min_review_interval_days < 0:
            raise TutorError("min-review-interval-days 必须是非负整数")
        # The floor is applied at every read site (status, recommend, record
        # output), so changing the value in either direction takes effect
        # immediately without rewriting stored review dates.
        state["strategy"]["min_review_interval_days"] = args.min_review_interval_days
    policies = state["strategy"].setdefault("subject_policies", {})
    if not isinstance(policies, dict):
        raise TutorError("state.json subject_policies 无效")
    for specification in args.subject_policy or []:
        subject_name, separator, mode = specification.partition("=")
        subject_name = subject_name.strip()
        mode = mode.strip()
        if not separator or subject_name not in SUBJECTS:
            raise TutorError(
                "subject-policy 必须使用 科目=模式，科目取 "
                + "/".join(SUBJECTS)
            )
        if mode not in ("active", "manual_trigger"):
            raise TutorError("subject-policy 模式只支持 active / manual_trigger")
        if mode == "active":
            policies.pop(subject_name, None)
        else:
            policies[subject_name] = {
                "mode": mode,
                "reason": args.subject_policy_reason or "由 configure 设定",
                "updated_at": now_iso(),
            }
    save_state_bundle(args.data_dir, profile, state, backup=True)
    print(
        "已更新个人路线：案例 "
        + ", ".join(state["strategy"].get("case_tracks", []))
        + "；论文 "
        + (", ".join(state["strategy"].get("essay_themes", [])) or "待诊断")
        + f"；战略放弃 {len(skips)} 个"
        + "；学科策略 "
        + (
            ", ".join(
                f"{subject}={policy.get('mode')}"
                for subject, policy in sorted(policies.items())
            )
            or "全部主动"
        )
    )
    return 0


def build_recommendation_payload(args: argparse.Namespace) -> dict[str, Any]:
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    profile, state = load_profile_and_state(args.data_dir, persist_pending=False)
    ensure_question_links_current(
        state, load_attempts(state_paths(args.data_dir)["attempts"])
    )
    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    exam_date = parse_date(profile["exam_date"]) if profile.get("exam_date") else None
    days_to_exam = (exam_date - today).days if exam_date else None
    crunch_mode = days_to_exam is not None and 0 <= days_to_exam <= 3
    raw_allocations, allocations = effective_subject_allocations(state, today)
    target_subject = args.subject or select_target_subject(state, allocations, today)
    maintenance_subject = (
        None
        if args.subject
        else select_maintenance_subject(state, target_subject, today)
    )
    strategy = state.get("strategy", {})
    configured_case_tracks = set(strategy.get("case_tracks", []))
    configured_essay_themes = set(strategy.get("essay_themes", []))
    strategic_skips = set(strategy.get("strategic_skips", {}))
    paused_subjects = set() if args.subject else manual_trigger_subjects(state)
    case_support_map = case_track_by_supporting_topic(curriculum)
    minimum_interval = int(strategy.get("min_review_interval_days", 0) or 0)
    cold_start_groups = curriculum.get("strategy", {}).get(
        "comprehensive_cold_start_groups", []
    )
    cold_start_group_by_topic = {
        topic_id: group_index
        for group_index, topic_ids in enumerate(cold_start_groups, 1)
        for topic_id in topic_ids
    }

    ranked: list[dict[str, Any]] = []
    for topic in curriculum["topics"]:
        if topic["id"] in strategic_skips:
            continue
        progress = state.get("topics", {}).get(topic["id"], {})
        survival_resource = any(
            "SURVIVAL.md" in resource or resource.startswith("cheatsheets/")
            for resource in topic.get("resources", [])
        )
        if crunch_mode and not progress and not survival_resource:
            continue
        subjects = topic.get("subjects", [])
        if args.subject and args.subject not in subjects:
            continue
        if (
            topic["id"].startswith("C")
            and strategy.get("case_tracks_configured")
            and topic["id"] not in configured_case_tracks
        ):
            continue
        if (
            topic["id"].startswith("P")
            and strategy.get("essay_themes_configured")
            and topic["id"] not in configured_essay_themes
        ):
            continue
        chosen_subject = target_subject if target_subject in subjects else max(
            subjects, key=lambda subject: allocations.get(subject, 0)
        )
        # A paused subject stays out of the plan unless the caller asked for it
        # explicitly with --subject.
        if not args.subject and chosen_subject in paused_subjects:
            continue
        # Automatic case planning operates on the canonical Cxx route layer.
        # Fine-grained Kxx topics contribute evidence to a route below, but
        # may only be selected directly through case-prepare --topic.
        if chosen_subject == "case" and not topic["id"].startswith("C"):
            continue
        skill = {
            "comprehensive": "recognition",
            "case": "application",
            "essay": "production",
        }[chosen_subject]
        if skill not in topic.get("skills", []):
            continue
        mastery = topic_mastery(state, topic["id"], skill)
        supporting_topic_ids = list(topic.get("covered_topic_ids", []))
        supporting_masteries = [
            topic_mastery(state, topic_id, "application")
            for topic_id in supporting_topic_ids
            if isinstance(
                state.get("topics", {})
                .get(topic_id, {})
                .get("mastery", {})
                .get("application"),
                dict,
            )
        ]
        need = max(
            [0.08, 1.0 - mastery]
            + [1.0 - supporting_mastery for supporting_mastery in supporting_masteries]
        )
        skill_progress = progress.get("mastery", {}).get(skill, {})
        review_at = (
            skill_progress.get("next_review_at")
            if isinstance(skill_progress, dict)
            else None
        )
        due = False
        urgent_due = False
        maintenance_due = False
        if review_at:
            try:
                due = (
                    parse_date(
                        effective_review_date(
                            review_at,
                            skill_progress.get("last_attempt_at"),
                            minimum_interval,
                            skill_progress.get("status"),
                        )
                    )
                    <= today
                )
                urgent_due = due and skill_progress.get("status") != "pass_ready"
                maintenance_due = due and not urgent_due
            except TutorError:
                due = True
                urgent_due = True
        supporting_due_names: list[str] = []
        for supporting_topic_id in supporting_topic_ids:
            supporting_progress = state.get("topics", {}).get(supporting_topic_id, {})
            supporting_record = supporting_progress.get("mastery", {}).get(
                "application", {}
            )
            supporting_review_at = (
                supporting_record.get("next_review_at")
                if isinstance(supporting_record, dict)
                else None
            )
            if not supporting_review_at:
                continue
            try:
                supporting_is_due = (
                    parse_date(
                        effective_review_date(
                            supporting_review_at,
                            supporting_record.get("last_attempt_at"),
                            minimum_interval,
                            supporting_record.get("status"),
                        )
                    )
                    <= today
                )
            except TutorError:
                supporting_is_due = True
            if supporting_is_due:
                due = True
                if supporting_record.get("status") == "pass_ready":
                    maintenance_due = True
                else:
                    urgent_due = True
                supporting_due_names.append(
                    topics[supporting_topic_id]["name"]
                    if supporting_topic_id in topics
                    else supporting_topic_id
                )
        due_factor = 1.7 if urgent_due else (1.15 if maintenance_due else 1.0)
        supporting_topics = [
            topics[topic_id]
            for topic_id in supporting_topic_ids
            if topic_id in topics
        ]
        frequency = max(
            [max(0.0, float(topic.get("frequency_count", 0)))]
            + [
                max(0.0, float(supporting_topic.get("frequency_count", 0)))
                for supporting_topic in supporting_topics
            ]
        )
        confidence_factor = {
            "high": 1.0,
            "medium": 0.9,
            "low": 0.7,
            "expert_estimate": 0.65,
        }.get(str(topic.get("frequency_confidence", "low")), 0.7)
        value = (
            (1.0 + math.log1p(frequency))
            * confidence_factor
            * float(topic.get("priority_weight", 0.5))
            * (
                1.0
                + 0.2
                * max(
                    [float(topic.get("quick_win", 0))]
                    + [float(item.get("quick_win", 0)) for item in supporting_topics]
                )
            )
            * (
                1.0
                + 0.2
                * max(
                    [float(topic.get("cross_subject_value", 0))]
                    + [
                        float(item.get("cross_subject_value", 0))
                        for item in supporting_topics
                    ]
                )
            )
        )
        cost = max(0.5, float(topic.get("estimated_minutes", 60)) / 60)
        score = allocations[chosen_subject] * need * due_factor * value / cost
        # Pass-first is a hard subject gate, not merely a soft score. This
        # prevents a 75-point strong subject from outranking a 44-point weak one.
        gate = 1 if chosen_subject == target_subject else 0
        expected_prefix = {"comprehensive": "K", "case": "C", "essay": "P"}[
            chosen_subject
        ]
        track_gate = 1 if topic["id"].startswith(expected_prefix) else 0
        cold_start_group = 999
        if (
            chosen_subject == "comprehensive"
            and state["subjects"]["comprehensive"].get("lower_bound_score") is None
            and int(state["subjects"]["comprehensive"].get("evidence_count", 0)) < 6
        ):
            cold_start_group = cold_start_group_by_topic.get(topic["id"], 999)
        reasons = []
        if state["subjects"][chosen_subject].get("lower_bound_score") is None:
            reasons.append("该科尚未测量，先诊断")
        elif state["subjects"][chosen_subject]["lower_bound_score"] < 45:
            reasons.append("该科保守下界未过线")
        if due:
            reasons.append("已到复习日" if urgent_due else "已到维护复习日")
        if supporting_due_names:
            reasons.append("关联应用考点到期：" + "、".join(supporting_due_names))
        if crunch_mode:
            reasons.append("考前 3 天，只做错题、保命卡或答题骨架")
        if frequency >= 6:
            reasons.append(f"历年高频证据 {int(frequency)} 次")
        if float(topic.get("cross_subject_value", 0)) >= 0.8:
            reasons.append("三科复用价值高")
        if not reasons:
            reasons.append("当前投入产出比最高")
        ranked.append(
            {
                "topic_id": topic["id"],
                "name": topic["name"],
                "subject": chosen_subject,
                "skill": skill,
                "priority_score": round(score, 4),
                "mastery": round(mastery, 4),
                "review_due": due,
                "urgent_review_due": urgent_due,
                "estimated_minutes": topic.get("estimated_minutes"),
                "reason": "；".join(reasons),
                "resources": topic.get("resources", []),
                "supporting_topic_ids": supporting_topic_ids,
                "_gate": gate,
                "_track_gate": track_gate,
                "_strategy_rank": int(topic.get("strategy_rank", 999)),
                "_frequency": frequency,
                "_cold_start_group": cold_start_group,
            }
        )

    ranked.sort(
        key=lambda item: (
            -item["_gate"],
            -int(item.get("urgent_review_due", item["review_due"])),
            -item["_track_gate"],
            item["_cold_start_group"],
            -item["priority_score"],
            item["_strategy_rank"],
            -item["_frequency"],
            item["topic_id"],
        )
    )
    # A corrupt private data file must not take the generic plan down with
    # it: fall back to ranked topics and surface the diagnosis failure.
    diagnosis_error: str | None = None
    try:
        diagnosis = learning_diagnosis(
            args.data_dir, target_subject, today, state=state
        )
    except TutorError as error:
        diagnosis = {"subject": target_subject, "mock": None, "issues": []}
        diagnosis_error = str(error)
    active_diagnostics: list[dict[str, Any]] = []
    for issue in diagnosis["issues"]:
        if issue["status"] not in {"pending_remediation", "due_review"}:
            continue
        issue = dict(issue)
        original_topic_id = issue["topic_id"]
        if target_subject == "case":
            track_id = (
                original_topic_id
                if original_topic_id.startswith("C")
                else case_support_map.get(original_topic_id)
            )
            if track_id is None:
                continue
            issue["topic_id"] = track_id
            issue["supporting_topic_id"] = original_topic_id
        if issue["topic_id"] in strategic_skips:
            continue
        if (
            issue["topic_id"].startswith("C")
            and strategy.get("case_tracks_configured")
            and issue["topic_id"] not in configured_case_tracks
        ):
            continue
        if (
            issue["topic_id"].startswith("P")
            and strategy.get("essay_themes_configured")
            and issue["topic_id"] not in configured_essay_themes
        ):
            continue
        active_diagnostics.append(issue)
    diagnostic_items = [
        {
            "topic_id": issue["topic_id"],
            "name": issue["topic_name"],
            "subject": issue["subject"],
            "skill": issue["skill"],
            "priority_score": 999.0 - index,
            "mastery": topic_mastery(state, issue["topic_id"], issue["skill"]),
            "review_due": issue["status"] == "due_review",
            "estimated_minutes": 5,
            "reason": issue["reason"],
            "resources": [],
            "diagnostic_status": issue["status"],
            "source_mock_id": issue["source_mock_id"],
            "source_item_ids": issue["source_item_ids"],
            "wrong_reasons": issue["wrong_reasons"],
            "supporting_topic_id": issue.get("supporting_topic_id"),
            "avoid_item_ids": issue["avoid_item_ids"],
        }
        for index, issue in enumerate(active_diagnostics)
    ]
    selected = diagnostic_items[: args.limit]
    diagnostic_topics = {item["topic_id"] for item in selected}
    if len(selected) < args.limit:
        selected.extend(
            item
            for item in ranked
            if item["topic_id"] not in diagnostic_topics
        )
        selected = selected[: args.limit]
    if maintenance_subject and args.limit >= 2 and not any(
        item["subject"] == maintenance_subject for item in selected
    ):
        maintenance_item = next(
            (item for item in ranked if item["subject"] == maintenance_subject),
            None,
        )
        # Maintenance is best-effort: never evict an uncorrected mock gap
        # (a diagnostic item) to make room for it.
        replaceable = [
            index
            for index, item in enumerate(selected)
            if "diagnostic_status" not in item
        ]
        if maintenance_item is not None and replaceable:
            maintenance_item = dict(maintenance_item)
            maintenance_item["reason"] = "三天最低维护；" + maintenance_item["reason"]
            selected[replaceable[-1]] = maintenance_item

    recommendations = []
    for item in selected:
        clean = {key: value for key, value in item.items() if not key.startswith("_")}
        recommendations.append(clean)

    payload = {
        "today": today.isoformat(),
        "safe_target": state.get("strategy", {}).get("safe_target", 52),
        "target_subject": target_subject,
        "maintenance_subject": maintenance_subject,
        "crunch_mode": crunch_mode,
        "days_to_exam": days_to_exam,
        "subject_allocation": allocations,
        "raw_subject_allocation": raw_allocations,
        "suppressed_subjects": sorted(paused_subjects),
        "recommendations": recommendations,
        "diagnosis_error": diagnosis_error,
        "profile": {
            "exam_date": profile.get("exam_date"),
            "daily_minutes": profile.get("daily_minutes"),
        },
    }
    payload["diagnosis"] = diagnosis
    return payload


def cmd_recommend(args: argparse.Namespace) -> int:
    payload = build_recommendation_payload(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"当前优先科目：{payload['target_subject']}")
    print(
        "时间分配："
        + "，".join(
            f"{subject} {allocation:.0%}"
            for subject, allocation in payload["subject_allocation"].items()
        )
    )
    if payload.get("diagnosis_error"):
        print(f"注意：逐题诊断暂不可用（{payload['diagnosis_error']}），已回退到通用排序。")
    for index, item in enumerate(payload["recommendations"], 1):
        print(
            f"{index}. [{item['subject']}] {item['topic_id']} {item['name']} "
            f"— {item['reason']}"
        )
    return 0


def case_type_for_topic(topic: dict[str, Any]) -> str | None:
    """Resolve a stable topic to its candidate real-paper case type.

    The curriculum remains the single source of truth for this mapping.  This
    avoids a second hard-coded routing table drifting away from configured
    tracks and recommendation priorities.
    """

    explicit = topic.get("case_type")
    if explicit is not None:
        return f"案例 {explicit}"
    for resource in topic.get("resources", []):
        match = CASE_RESOURCE_RE.match(str(resource))
        if match:
            return f"案例 {match.group(1)}"
    return None


def case_figure_assets(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return absolute existing and missing figure paths for one case item."""

    existing: list[str] = []
    missing: list[str] = []
    for filename in item.get("figures", []):
        path = REPO_ROOT / "past-papers" / "assets" / item["year"] / filename
        if path.is_file():
            existing.append(str(path.resolve()))
        else:
            missing.append(str(path.resolve()))
    return existing, missing


def cmd_case_prepare(args: argparse.Namespace) -> int:
    """Select one complete blind case from the existing adaptive plan.

    This is deliberately read-only.  It combines recommendation, case-type
    resolution, freshness and figure checks so the teaching turn does not need
    to rediscover those decisions through several model/tool round trips.
    """

    import paper_practice

    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    requested_topic = args.topic
    if requested_topic:
        topic = topics.get(requested_topic)
        if topic is None:
            raise TutorError(f"未知稳定考点 ID：{requested_topic}")
        if "case" not in topic.get("subjects", []) or "application" not in topic.get(
            "skills", []
        ):
            raise TutorError(f"考点 {requested_topic} 不支持案例应用训练")

    recommendation_args = argparse.Namespace(
        data_dir=args.data_dir,
        subject="case",
        limit=max(len(topics), 20),
        today=today.isoformat(),
    )
    plan = build_recommendation_payload(recommendation_args)
    recommendations = plan["recommendations"]
    if requested_topic:
        selected_recommendations = [
            item for item in recommendations if item["topic_id"] == requested_topic
        ]
        if not selected_recommendations:
            topic = topics[requested_topic]
            selected_recommendations = [
                {
                    "topic_id": requested_topic,
                    "name": topic["name"],
                    "subject": "case",
                    "skill": "application",
                    "priority_score": None,
                    "mastery": topic_mastery(
                        load_profile_and_state(args.data_dir, persist_pending=False)[1],
                        requested_topic,
                        "application",
                    ),
                    "review_due": False,
                    "estimated_minutes": topic.get("estimated_minutes"),
                    "reason": "考生明确指定该案例考点",
                    "resources": topic.get("resources", []),
                }
            ]
    else:
        selected_recommendations = recommendations

    attempts = load_attempts(state_paths(args.data_dir)["attempts"])
    last_attempt_by_item: dict[str, str] = {}
    for event in attempts:
        if event.get("subject") != "case" or not event.get("item_id"):
            continue
        attempted_at = str(event.get("at") or "")
        item_id = str(event["item_id"])
        if attempted_at > last_attempt_by_item.get(item_id, ""):
            last_attempt_by_item[item_id] = attempted_at

    case_items = paper_practice.practice_items(paper_practice.build_case_items())
    skipped_incomplete = 0
    chosen_recommendation: dict[str, Any] | None = None
    chosen_item: dict[str, Any] | None = None
    chosen_assets: list[str] = []
    chosen_missing_assets: list[str] = []
    chosen_case_type: str | None = None

    for recommendation in selected_recommendations:
        topic = topics.get(recommendation["topic_id"])
        if topic is None:
            continue
        case_type = case_type_for_topic(topic)
        if case_type is None:
            continue
        candidates: list[tuple[dict[str, Any], list[str], list[str]]] = []
        for item in case_items:
            if item.get("tag") != case_type or item.get("practice_mode") != "blind":
                continue
            assets, missing_assets = case_figure_assets(item)
            complete = not item.get("missing_figure") and not missing_assets
            if not complete and not args.allow_missing_figures:
                skipped_incomplete += 1
                continue
            candidates.append((item, assets, missing_assets))
        if not candidates:
            continue

        def candidate_key(
            candidate: tuple[dict[str, Any], list[str], list[str]]
        ) -> tuple[Any, ...]:
            item = candidate[0]
            last_attempt = last_attempt_by_item.get(item["id"])
            source_rank = 0 if item.get("source_type") == "real" else 1
            year_match = re.match(r"(\d{4})", str(item.get("year") or "0"))
            year = int(year_match.group(1)) if year_match else 0
            return (
                1 if last_attempt else 0,
                last_attempt or "",
                source_rank,
                -year,
                item["id"],
            )

        chosen_item, chosen_assets, chosen_missing_assets = min(
            candidates, key=candidate_key
        )
        chosen_recommendation = recommendation
        chosen_case_type = case_type
        break

    if chosen_item is None or chosen_recommendation is None or chosen_case_type is None:
        detail = f"考点 {requested_topic}" if requested_topic else "当前案例推荐"
        raise TutorError(
            f"{detail} 没有可用的完整盲练真题；"
            "可维护题面材料，或显式使用 --allow-missing-figures"
        )

    public_item = copy.deepcopy(chosen_item)
    public_item.pop("answer", None)
    public_item["figure_assets"] = chosen_assets
    public_item["missing_figure_assets"] = chosen_missing_assets
    public_item["figures_complete"] = (
        not public_item.get("missing_figure") and not chosen_missing_assets
    )
    public_item["coach_note"] = (
        "只呈现 stem，不展示答案；若 figure_assets 非空，按顺序查看后用文字准确描述图意，"
        "不要向考生输出本地路径；作答后再按 year + numeral 调用 paper_practice --reveal。"
    )
    selected_topic_definition = topics[chosen_recommendation["topic_id"]]
    track_id = (
        chosen_recommendation["topic_id"]
        if chosen_recommendation["topic_id"].startswith("C")
        else case_track_by_supporting_topic(curriculum).get(
            chosen_recommendation["topic_id"]
        )
    )
    supporting_topic_ids = list(
        selected_topic_definition.get("covered_topic_ids", [])
    )
    payload = {
        "route_lock": {
            "mode": "case_start",
            "subject": "case",
            "track_id": track_id,
            "topic_id": chosen_recommendation["topic_id"],
            "supporting_topic_ids": supporting_topic_ids,
            "case_type": chosen_case_type,
            "item_id": public_item["id"],
        },
        "today": today.isoformat(),
        "selected_topic": {
            "topic_id": chosen_recommendation["topic_id"],
            "name": chosen_recommendation["name"],
            "mastery": chosen_recommendation.get("mastery"),
            "review_due": chosen_recommendation.get("review_due"),
            "priority_score": chosen_recommendation.get("priority_score"),
            "reason": chosen_recommendation["reason"],
            "track_id": track_id,
            "supporting_topic_ids": supporting_topic_ids,
        },
        "case_type": chosen_case_type,
        "item": public_item,
        "suggested_minutes": 25,
        "selection": {
            "policy": "adaptive_recommendation_then_curriculum_case_type",
            "fresh_item": public_item["id"] not in last_attempt_by_item,
            "skipped_incomplete_items": skipped_incomplete,
            "allow_missing_figures": bool(args.allow_missing_figures),
        },
        "reveal": {
            "year": public_item["year"],
            "numeral": public_item["numeral"],
        },
        "record": {
            "topic_id": chosen_recommendation["topic_id"],
            "skill": "application",
            "subject": "case",
            "item_id": public_item["id"],
            "source_type": public_item["source_type"],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def quiz_sessions_dir(data_dir: Path) -> Path:
    return data_dir / QUIZ_SESSIONS_DIR


def quiz_session_path(data_dir: Path, quiz_id: str) -> Path:
    if not re.fullmatch(r"quiz-[0-9A-Za-z._-]+", quiz_id):
        raise TutorError("quiz-id 格式无效")
    return quiz_sessions_dir(data_dir) / f"{quiz_id}.json"


def quiz_questions_served_on(data_dir: Path, day: date) -> set[str]:
    """Include prepared but ungraded questions in the same-day item cooldown."""

    items: set[str] = set()
    directory = quiz_sessions_dir(data_dir)
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TutorError(f"客观题会话损坏：{path}") from error
        created_at = manifest.get("created_at")
        questions = manifest.get("questions")
        if not isinstance(created_at, str) or not isinstance(questions, list):
            raise TutorError(f"客观题会话损坏：{path}")
        if parse_datetime(created_at).date() != day:
            continue
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("item_id"), str):
                raise TutorError(f"客观题会话损坏：{path}")
            items.add(question["item_id"])
    return items


def recently_served_variant_item_ids(
    data_dir: Path, day: date, days: int = RECENT_ITEM_COOLDOWN_DAYS
) -> set[str]:
    """Item ids already handed to the learner as a follow-up variant.

    A variant is answered out loud in chat and never becomes an attempt, so the
    session manifests are the only durable trace of what was already served.
    Without this set the picker repeats the same follow-up question day after
    day, because nothing else marks it as seen.
    """

    items: set[str] = set()
    directory = quiz_sessions_dir(data_dir)
    if not directory.is_dir():
        return items
    earliest = day - timedelta(days=max(days, 0))
    for path in sorted(directory.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TutorError(f"客观题会话损坏：{path}") from error
        # A variant is served while grading, so graded_at is the serving time.
        stamp = manifest.get("graded_at") or manifest.get("created_at")
        if not isinstance(stamp, str):
            raise TutorError(f"客观题会话损坏：{path}")
        served_on = parse_datetime(stamp).date()
        if not earliest <= served_on <= day:
            continue
        result = manifest.get("result")
        if not isinstance(result, dict):
            continue
        for entry in result.get("results") or []:
            if not isinstance(entry, dict):
                continue
            variant = entry.get("variant_question")
            if isinstance(variant, dict) and isinstance(variant.get("item_id"), str):
                items.add(variant["item_id"])
    return items


def recently_mastered_item_ids(
    attempts: Iterable[dict[str, Any]],
    day: date,
    days: int = RECENT_ITEM_COOLDOWN_DAYS,
) -> set[str]:
    """Items answered correctly with certain confidence inside the cooldown window.

    A guess or an unsure answer is fragile evidence, so the item stays eligible.
    """

    earliest = day - timedelta(days=max(days, 0))
    items: set[str] = set()
    for event in attempts:
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        if event.get("confidence") != "sure":
            continue
        if event.get("response_state") == "conceded":
            continue
        try:
            score = float(event.get("score") or 0)
            max_score = float(event.get("max_score") or 0)
        except (TypeError, ValueError):
            continue
        if max_score <= 0 or score < max_score:
            continue
        at = event.get("at")
        if not isinstance(at, str):
            continue
        if parse_datetime(at).date() < earliest:
            continue
        items.add(item_id)
    return items


def paper_source_type(year: str | None) -> str:
    return "recalled_real" if year in RECALLED_REAL_YEARS else "real"



def load_quiz_question_pool(curriculum: dict[str, Any]) -> list[dict[str, Any]]:
    """Load all objective questions once for a quiz preparation request.

    Every item carries the quality verdict from ``sanitize_bank``; questions
    that are not ``ready`` stay in the pool so doctor can report them, but
    ``quiz_question_for_topic`` never offers them to a quiz.
    """

    merged: dict[str, dict[str, Any]] = {}
    for paper in sorted(sanitize_bank.PAPER_DIR.glob("*.md")):
        for item in sanitize_bank.parse_paper(paper):
            if not item.get("options") or not item.get("correct"):
                continue
            normalized = dict(item)
            normalized["source"] = paper.relative_to(REPO_ROOT).as_posix()
            is_reconstruction = (
                "非原题" in str(item.get("tag_label") or "")
                or "非原题" in str(item.get("stem") or "")
            )
            normalized["source_type"] = (
                "self_authored" if is_reconstruction else paper_source_type(item.get("year"))
            )
            normalized["teaching_status"] = (
                "not_applicable"
                if normalized.get("quality_status") != "ready"
                else (
                    "ready"
                    if str(normalized.get("explanation") or "").strip()
                    else "missing_explanation"
                )
            )
            override = question_registry.topic_override(item["id"])
            topics = {override} if override else set(item.get("candidate_topics", []))
            normalized["candidate_topics"] = sorted(topics)
            merged[item["id"]] = normalized

    topics_by_resource: dict[str, set[str]] = {}
    for topic in curriculum["topics"]:
        for resource in topic.get("resources", []):
            if resource.startswith("exam-bank/"):
                topics_by_resource.setdefault(resource, set()).add(topic["id"])
    for resource, topic_ids in topics_by_resource.items():
        path = REPO_ROOT / resource
        if not path.is_file():
            continue
        for parsed in sanitize_bank.parse_exam_bank(path):
            if not parsed.get("options") or not parsed.get("correct"):
                continue
            item_id = parsed["id"]
            existing = merged.get(item_id, {})
            override = question_registry.topic_override(item_id)
            candidate_ids = (
                {override}
                if override
                else set(existing.get("candidate_topics", [])) | topic_ids
            )
            normalized = {
                **parsed,
                "year": None,
                "tag": None,
                "tag_label": None,
                "source": resource,
                "source_type": "self_authored",
                "candidate_topics": sorted(candidate_ids),
            }
            normalized["teaching_status"] = (
                "not_applicable"
                if normalized.get("quality_status") != "ready"
                else (
                    "ready"
                    if str(normalized.get("explanation") or "").strip()
                    else "missing_explanation"
                )
            )
            merged[item_id] = normalized
    return list(merged.values())


def quiz_question_for_topic(
    raw: dict[str, Any],
    topic: dict[str, Any],
) -> dict[str, Any] | None:
    topic_id = topic["id"]
    # The quality gate is absolute: an item that is missing its figure, table
    # or a legal answer key never reaches a quiz, and the coach never repairs
    # it live.
    if raw.get("quality_status") != "ready" or raw.get("teaching_status") != "ready":
        return None
    override = question_registry.topic_override(raw["id"])
    if override and override != topic_id:
        return None
    if topic_id not in raw.get("candidate_topics", []):
        return None
    return {
        "item_id": raw["id"],
        "topic_id": topic_id,
        "topic_name": topic["name"],
        "stem": raw["stem"],
        "context_id": raw.get("context_id"),
        "context_title": raw.get("context_title"),
        "context": raw.get("context"),
        "options": raw["options"],
        "correct": sorted(raw["correct"]),
        "explanation": raw.get("explanation"),
        "source": raw.get("source"),
        "source_type": raw["source_type"],
        "year": raw.get("year"),
    }


def _quiz_candidate_sort_key(
    item: dict[str, Any], answer_counts: dict[str, int]
) -> tuple[Any, ...]:
    signature = "".join(item["correct"])
    source_rank = {"real": 0, "recalled_real": 1, "self_authored": 2}.get(
        item["source_type"], 3
    )
    year_match = re.match(r"(\d{4})", str(item.get("year") or "0"))
    year = int(year_match.group(1)) if year_match else 0
    return (source_rank, answer_counts.get(signature, 0), -year, item["item_id"])


def select_quiz_group(
    data_dir: Path,
    today: date,
    limit: int,
    recommendations: list[dict[str, Any]],
    curriculum: dict[str, Any],
    topics: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select a complete group within one stable topic, avoiding seen items."""

    attempts = load_attempts(state_paths(data_dir)["attempts"])
    route_topics = list(dict.fromkeys(item["topic_id"] for item in recommendations))
    pool = load_quiz_question_pool(curriculum)
    attempted_items = {event.get("item_id") for event in attempts}
    served_today_items = quiz_questions_served_on(data_dir, today)
    served_today_items.update(
        event.get("item_id") for event in attempts
        if event.get("at") and parse_datetime(event["at"]).date() == today
    )
    protected_items = recently_mastered_item_ids(attempts, today) | (
        recently_served_variant_item_ids(data_dir, today)
    )
    best_count = 0
    for allow_protected in (False, True):
        for allow_repeated in (False, True):
            for allow_avoided in (False, True):
                for topic_id in route_topics:
                    topic = topics.get(topic_id)
                    if topic is None:
                        continue
                    recommendation = next(
                        item for item in recommendations if item["topic_id"] == topic_id
                    )
                    avoided = set(recommendation.get("avoid_item_ids", []))
                    candidates = []
                    for raw in pool:
                        candidate = quiz_question_for_topic(raw, topic)
                        if candidate is None or candidate["item_id"] in served_today_items:
                            continue
                        if not allow_protected and candidate["item_id"] in protected_items:
                            continue
                        if not allow_repeated and candidate["item_id"] in attempted_items:
                            continue
                        if not allow_avoided and candidate["item_id"] in avoided:
                            continue
                        candidates.append(candidate)
                    best_count = max(best_count, len(candidates))
                    if len(candidates) < limit:
                        continue
                    selected: list[dict[str, Any]] = []
                    answer_counts: dict[str, int] = {}
                    while len(selected) < limit:
                        chosen = min(
                            candidates,
                            key=lambda item: _quiz_candidate_sort_key(item, answer_counts),
                        )
                        candidates.remove(chosen)
                        chosen["number"] = len(selected) + 1
                        chosen["mode"] = (
                            "review" if recommendation.get("diagnostic_status") else "practice"
                        )
                        chosen["prior_wrong_reasons"] = recommendation.get("wrong_reasons") or []
                        selected.append(chosen)
                        signature = "".join(chosen["correct"])
                        answer_counts[signature] = answer_counts.get(signature, 0) + 1
                    return selected, [recommendation] * len(selected)
    raise TutorError(
        f"只能找到 {best_count} 道符合去重和元数据要求的客观题，无法组成 {limit} 题"
    )


def build_quiz_prepare_payload(
    args: argparse.Namespace,
    *,
    quiz_id: str | None = None,
    continuation_parent: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create or replay one private quiz session and return its public payload.

    A continuation uses a deterministic quiz id.  Persisting the public payload
    alongside its private manifest makes a retry return the same question set
    without re-running selection after a partial parent operation.
    """

    if args.subject != "comprehensive":
        raise TutorError("quiz-prepare 当前只支持综合知识客观题")
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    if args.topic:
        requested_topic = topics.get(args.topic)
        if requested_topic is None:
            raise TutorError(f"未知稳定考点 ID：{args.topic}")
        if (
            not args.topic.startswith("K")
            or "comprehensive" not in requested_topic.get("subjects", [])
            or "recognition" not in requested_topic.get("skills", [])
        ):
            raise TutorError(f"考点 {args.topic} 不支持综合知识识记训练")
    if quiz_id is not None:
        existing_path = quiz_session_path(args.data_dir, quiz_id)
        if existing_path.exists():
            if continuation_parent is None:
                raise TutorError(f"quiz-id {quiz_id} 已存在")
            existing = load_json(existing_path, "客观题会话")
            if existing.get("continuation_parent") != continuation_parent:
                raise TutorError(f"quiz-id {quiz_id} 已被其他续练会话占用")
            public_payload = existing.get("public_payload")
            if not isinstance(public_payload, dict):
                raise TutorError(f"续练会话 {quiz_id} 缺少可重放题面")
            return copy.deepcopy(public_payload)
    profile, state = load_profile_and_state(args.data_dir)
    today = parse_date(args.today) if args.today else datetime.now().astimezone().date()
    recommendation_args = argparse.Namespace(
        data_dir=args.data_dir,
        subject=args.subject,
        limit=max(args.limit * 4, len(topics)),
        today=today.isoformat(),
    )
    recommendations = build_recommendation_payload(recommendation_args)[
        "recommendations"
    ]
    if args.topic:
        recommendations = [
            item for item in recommendations if item["topic_id"] == args.topic
        ]
        if not recommendations:
            topic = topics[args.topic]
            recommendations = [{
                "topic_id": args.topic,
                "name": topic["name"],
                "subject": "comprehensive",
                "skill": "recognition",
                "review_due": False,
                "reason": "考生显式指定该综合知识考点",
            }]
    selected, chosen_recommendations = select_quiz_group(
        args.data_dir, today, args.limit, recommendations, curriculum, topics,
    )

    created_at = now_iso()
    if quiz_id is None:
        quiz_id = (
            "quiz-"
            + parse_datetime(created_at).strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:8]
        )
    path = quiz_session_path(args.data_dir, quiz_id)

    manifest = {
        "schema_version": QUIZ_SCHEMA_VERSION,
        "quiz_id": quiz_id,
        "status": "pending",
        "subject": args.subject,
        "created_at": created_at,
        "questions": selected,
    }
    if continuation_parent is not None:
        manifest["continuation_parent"] = copy.deepcopy(continuation_parent)
    public_questions = [
        {
            "number": item["number"],
            "stem": item["stem"],
            "context_id": item.get("context_id"),
            "options": item["options"],
            "source_type": item["source_type"],
            "year": item.get("year"),
            "topic_name": item["topic_name"],
        }
        for item in selected
    ]
    public_contexts: list[dict[str, str]] = []
    seen_context_ids: set[str] = set()
    for item in selected:
        context_id = item.get("context_id")
        context = item.get("context")
        if not context_id or not context or context_id in seen_context_ids:
            continue
        public_contexts.append(
            {
                "id": context_id,
                "title": item.get("context_title") or "题目上下文",
                "text": context,
            }
        )
        seen_context_ids.add(context_id)
    covered_topics: list[dict[str, str]] = []
    for item in selected:
        if all(topic["topic_id"] != item["topic_id"] for topic in covered_topics):
            covered_topics.append(
                {"topic_id": item["topic_id"], "name": item["topic_name"]}
            )
    weakpoints = weakpoints_payload(
        args.data_dir, args.subject, today, days=21, limit=100
    )
    rows_by_topic: dict[str, dict[str, Any]] = {}
    for section in ("due", "recent", "uncovered"):
        for row in weakpoints[section]:
            rows_by_topic.setdefault(row["topic_id"], row)
    evidence_summary: list[str] = []
    for topic in covered_topics:
        row = rows_by_topic.get(topic["topic_id"])
        if row is None:
            continue
        if row["recent_attempts"]:
            evidence_summary.append(
                f"{topic['topic_id']} 近 21 天正确率 {row['recent_accuracy']:.0%}"
                f"（{row['recent_attempts']} 题，蒙对/不确定 {row['guess_correct']}/{row['unsure_correct']}）"
            )
        if row["overdue_days"] is not None and row["overdue_days"] >= 0:
            evidence_summary.append(
                f"{topic['topic_id']} 已到复习日（逾期 {row['overdue_days']} 天）"
            )
    evidence_summary.append(
        "本组覆盖：" + "、".join(topic["name"] for topic in covered_topics)
    )
    exam_date = profile.get("exam_date")
    days_left = None
    if exam_date:
        days_left = (parse_date(exam_date) - today).days
    # Announce a recommendation that actually contributed a question; the
    # primary entry may be a diagnostic gap whose items are all avoided.
    primary = (
        chosen_recommendations[0]
        if chosen_recommendations
        else (recommendations[0] if recommendations else None)
    )
    objective = "训练本组考点"
    if primary is not None:
        verb = (
            "复测"
            if any(item.get("review_due") for item in chosen_recommendations)
            else "训练"
        )
        objective = f"{verb}{primary['name']}（{primary['reason']}）"
        if len(covered_topics) > 1:
            objective += f"，同批覆盖 {len(covered_topics) - 1} 个考点"
    payload = {
        "quiz_id": quiz_id,
        "subject": args.subject,
        "selected_subject": args.subject,
        "count": len(public_questions),
        "questions": public_questions,
        "contexts": public_contexts,
        "exam_date": exam_date,
        "days_left": days_left,
        "daily_minutes": profile.get("daily_minutes"),
        "objective": objective,
        "evidence_summary": evidence_summary,
    }
    if continuation_parent is not None:
        manifest["public_payload"] = copy.deepcopy(payload)
    atomic_write_json(path, manifest)
    return payload


def cmd_quiz_prepare(args: argparse.Namespace) -> int:
    payload = build_quiz_prepare_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def prepare_next_quiz_payload(
    data_dir: Path,
    source_path: Path,
    source_manifest: dict[str, Any],
    grade_payload: dict[str, Any],
    *,
    phase: str,
    limit: int,
) -> dict[str, Any]:
    """Prepare a deterministic continuation when grading routes to a quiz.

    The parent manifest records the child id before selection starts.  If the
    process stops after grading, the same command can resume preparation
    without creating a second pending session or recording duplicate evidence.
    """

    if limit <= 0:
        raise TutorError("next-limit 必须大于 0")
    next_action = grade_payload.get("next_action")
    payload = {
        "grade": grade_payload,
        "next_quiz": None,
        "preparation_status": "not_applicable",
    }
    if not isinstance(next_action, dict):
        raise TutorError("判分结果缺少 next_action")
    if (
        next_action.get("mode") != "quiz_prepare"
        or next_action.get("subject") != "comprehensive"
    ):
        return payload

    source_quiz_id = source_manifest.get("quiz_id")
    if not isinstance(source_quiz_id, str):
        raise TutorError("续练来源缺少 quiz_id")
    if phase not in {"grade", "variant"}:
        raise TutorError(f"未知续练阶段：{phase}")
    suffix = "after-grade" if phase == "grade" else "after-variant"
    child_quiz_id = f"{source_quiz_id}-{suffix}"
    continuation = source_manifest.get("next_quiz")
    if continuation is None:
        continuation = {
            "phase": phase,
            "quiz_id": child_quiz_id,
            "status": "reserved",
            "next_action": copy.deepcopy(next_action),
        }
        source_manifest["next_quiz"] = continuation
        atomic_write_json(source_path, source_manifest)
    elif not isinstance(continuation, dict):
        raise TutorError(f"quiz-id {source_quiz_id} 的续练元数据无效")
    elif (
        continuation.get("phase") != phase
        or continuation.get("quiz_id") != child_quiz_id
    ):
        raise TutorError(f"quiz-id {source_quiz_id} 的续练元数据冲突")

    graded_at = (
        source_manifest.get("graded_at")
        if phase == "grade"
        else source_manifest.get("variant_graded_at")
    )
    if not isinstance(graded_at, str):
        raise TutorError(f"quiz-id {source_quiz_id} 缺少续练时间")
    continuation_parent = {"quiz_id": source_quiz_id, "phase": phase}
    prepare_args = argparse.Namespace(
        data_dir=data_dir,
        subject="comprehensive",
        topic=next_action.get("topic_id"),
        limit=limit,
        today=parse_datetime(graded_at).date().isoformat(),
    )
    next_quiz = build_quiz_prepare_payload(
        prepare_args,
        quiz_id=child_quiz_id,
        continuation_parent=continuation_parent,
    )
    continuation["status"] = "ready"
    atomic_write_json(source_path, source_manifest)
    payload["next_quiz"] = next_quiz
    payload["preparation_status"] = "ready"
    return payload


def runtime_grade_payload(grade_payload: dict[str, Any]) -> dict[str, Any]:
    """Project a grading result to the fields used in one coaching response."""

    fields = (
        "quiz_id",
        "score",
        "max_score",
        "counted_questions",
        "invalidated_count",
        "conceded_count",
        "results",
        "recorded_attempts",
        "idempotent",
        "subject_status",
        "next_action",
    )
    return {
        field: copy.deepcopy(grade_payload[field])
        for field in fields
        if field in grade_payload
    }


def runtime_prepared_grade_payload(payload: dict[str, Any]) -> dict[str, Any]:
    grade_payload = payload.get("grade")
    if not isinstance(grade_payload, dict):
        raise TutorError("续练结果缺少判分数据")
    return {
        **runtime_grade_payload(grade_payload),
        "preparation_status": payload.get("preparation_status"),
        "next_quiz": copy.deepcopy(payload.get("next_quiz")),
    }


def parse_quiz_answers(value: str) -> list[list[str]]:
    tokens = [token for token in re.split(r"[,，\s]+", value.strip()) if token]
    answers: list[list[str]] = []
    for token in tokens:
        normalized = token.upper()
        letters = sorted(set(re.findall(r"[A-Z]", normalized)))
        if "X" in letters:
            # "不会" is a first-class answer state, but it must never be mixed
            # with option letters: AX would silently look like a choice.
            if letters != ["X"] or normalized.count("X") != 1:
                raise TutorError(f"X（明确不会）不能与其他选项混写：{token}")
            answers.append(["X"])
            continue
        if not letters or any(letter not in {"A", "B", "C", "D"} for letter in letters):
            raise TutorError(f"答案格式无效：{token}")
        answers.append(letters)
    return answers


def parse_quiz_confidences(value: str | None, count: int) -> list[str]:
    if not value:
        return ["sure"] * count
    values = [token for token in re.split(r"[,，\s]+", value.strip()) if token]
    if len(values) == 1:
        values *= count
    if len(values) != count or any(item not in {"sure", "unsure", "guess"} for item in values):
        raise TutorError("confidences 必须为 sure/unsure/guess，数量为 1 或与题目数一致")
    return values


def parse_variant_confidences(value: str | None, count: int) -> list[str]:
    """Use the normal quiz default for variants unless confidence is stated."""

    if not value:
        return ["sure"] * count
    return parse_quiz_confidences(value, count)


QUIZ_INVALIDATION_REASONS = (
    "missing_required_figure",
    "missing_required_table",
    "missing_figure_asset",
    "figure_not_renderable",
    "answer_marker_leak",
    "explanation_leak",
    "empty_stem",
    "missing_options",
    "incomplete_option_set",
    "missing_required_context",
    "multi_question_group",
    "duplicate_options",
    "answer_not_in_options",
    "unclear_stem",
    "wrong_answer_key",
)
QUIZ_INVALIDATION_ALIASES = {
    "incomplete_stem": "unclear_stem",
    "stem_incomplete": "unclear_stem",
    "missing_table": "missing_required_table",
    "missing_figure": "missing_required_figure",
    "missing_context": "missing_required_context",
    "answer_leak": "answer_marker_leak",
}


def parse_quiz_marks(
    value: str | None,
    count: int,
    *,
    label: str,
    allowed: Iterable[str] = (),
    aliases: dict[str, str] | None = None,
) -> dict[int, str]:
    """Parse ``--invalidate/--audit`` values written as ``4=reason``."""

    marks: dict[int, str] = {}
    if not value:
        return marks
    for token in [item for item in re.split(r"[,，;；]+", value.strip()) if item.strip()]:
        number_text, separator, reason_text = token.partition("=")
        if not separator:
            raise TutorError(f"{label} 需要写成 题号=原因：{token}")
        try:
            number = int(number_text.strip())
        except ValueError as error:
            raise TutorError(f"{label} 的题号必须是数字：{token}") from error
        if not 1 <= number <= count:
            raise TutorError(f"{label} 的题号超出范围：{token}")
        reason = reason_text.strip()
        if not reason:
            raise TutorError(f"{label} 缺少原因：{token}")
        if aliases:
            reason = aliases.get(reason, reason)
        if allowed and reason not in allowed:
            raise TutorError(
                f"{label} 原因无效：{reason}（可用：" + ", ".join(sorted(allowed)) + "）"
            )
        marks[number] = reason
    return marks


def comparable_existing_event(
    existing: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Let a quiz graded before the response-state fields existed replay cleanly."""

    filled = dict(existing)
    for key in ("response_state", "selected_answer", "correct_answer"):
        if key not in filled:
            filled[key] = candidate.get(key)
    return filled


def variant_question_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": candidate["item_id"],
        "topic_id": candidate["topic_id"],
        "stem": candidate["stem"],
        "context_id": candidate.get("context_id"),
        "context_title": candidate.get("context_title"),
        "context": candidate.get("context"),
        "options": candidate["options"],
        "answer": "".join(candidate["correct"]),
        "source_type": candidate["source_type"],
    }


def pick_variant_question(
    question: dict[str, Any],
    pool: list[dict[str, Any]],
    topics: dict[str, dict[str, Any]],
    blocked_items: set[str],
    recently_served_items: set[str] | None = None,
) -> dict[str, Any] | None:
    """Pick a fresh, quality-gated follow-up on the same stable topic.

    The variant comes from the verified pool instead of being written on the
    spot, so the coaching round never needs to search the question bank.  An
    item the learner already saw is skipped until fresh items are exhausted.
    """

    topic = topics.get(question.get("topic_id"))
    if not topic:
        return None
    stale: dict[str, Any] | None = None
    for raw in pool:
        candidate = quiz_question_for_topic(raw, topic)
        if candidate is None:
            continue
        if candidate["item_id"] == question.get("item_id"):
            continue
        if candidate["item_id"] in blocked_items:
            continue
        if recently_served_items and candidate["item_id"] in recently_served_items:
            if stale is None:
                stale = candidate
            continue
        return variant_question_payload(candidate)
    return variant_question_payload(stale) if stale is not None else None


def append_quiz_audit_entries(
    data_dir: Path,
    quiz_id: str,
    audits: dict[int, str],
    questions: list[dict[str, Any]],
    at: str,
) -> None:
    """Idempotently queue question-bank suspicions for a maintenance task."""

    path = data_dir / "quiz-audit-queue.jsonl"
    entries: list[dict[str, Any]] = []
    existing_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    if path.exists():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise TutorError(
                    f"quiz-audit-queue.jsonl 第 {line_number} 行损坏"
                ) from error
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("quiz_id"), str)
                or not isinstance(entry.get("number"), int)
            ):
                raise TutorError(
                    f"quiz-audit-queue.jsonl 第 {line_number} 行缺少 quiz_id/number"
                )
            key = (entry["quiz_id"], entry["number"])
            if key in existing_by_key:
                raise TutorError(
                    f"quiz-audit-queue.jsonl 存在重复审计项：{key[0]}#{key[1]}"
                )
            entries.append(entry)
            existing_by_key[key] = entry

    for number, note in sorted(audits.items()):
        question = questions[number - 1]
        candidate = {
            "at": at,
            "quiz_id": quiz_id,
            "number": number,
            "item_id": question.get("item_id"),
            "topic_id": question.get("topic_id"),
            "note": note,
            "status": "open",
        }
        key = (quiz_id, number)
        existing = existing_by_key.get(key)
        if existing is not None:
            comparable_keys = ("item_id", "topic_id", "note")
            if any(existing.get(name) != candidate.get(name) for name in comparable_keys):
                raise TutorError(f"审计项 {quiz_id}#{number} 与已记录内容冲突")
            continue
        entries.append(candidate)
        existing_by_key[key] = candidate

    content = "".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )
    atomic_write_text(path, content)


def load_quiz_manifest(data_dir: Path, quiz_id: str) -> tuple[Path, dict[str, Any]]:
    path = quiz_session_path(data_dir, quiz_id)
    manifest = load_json(path, "客观题会话")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != QUIZ_SCHEMA_VERSION
        or manifest.get("quiz_id") != quiz_id
        or not isinstance(manifest.get("questions"), list)
        or not manifest["questions"]
    ):
        raise TutorError(f"客观题会话损坏：{path}")
    return path, normalized_quiz_manifest(manifest, topic_map(load_curriculum()))


def normalized_quiz_manifest(
    manifest: dict[str, Any], topics: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Correct only derived public-item links, including cached feedback."""

    normalized = copy.deepcopy(manifest)
    questions = normalized.get("questions", [])
    if not isinstance(questions, list):
        raise TutorError("客观题会话 questions 无效")
    for question in questions:
        if not isinstance(question, dict):
            raise TutorError("客观题会话题目无效")
        corrected = question_registry.canonicalize_public_event(question)
        if corrected is not question:
            question.update(corrected)
            question["topic_name"] = topics[question["topic_id"]]["name"]
    for key in ("result", "variant_result"):
        payload = normalized.get(key)
        if not isinstance(payload, dict):
            continue
        for entry in payload.get("results") or []:
            if not isinstance(entry, dict):
                continue
            if key == "result":
                number = entry.get("number")
                if isinstance(number, int) and 1 <= number <= len(questions):
                    source = questions[number - 1]
                    if source.get("item_id") in question_registry.PUBLIC_ITEM_CORRECTIONS:
                        entry["topic_id"] = source["topic_id"]
            else:
                corrected = question_registry.canonicalize_public_event(entry)
                if corrected is not entry:
                    entry["topic_id"] = corrected["topic_id"]
            variant = entry.get("variant_question")
            if isinstance(variant, dict):
                corrected = question_registry.canonicalize_public_event(variant)
                if corrected is not variant:
                    variant["topic_id"] = corrected["topic_id"]
    return normalized


def cmd_quiz_grade(args: argparse.Namespace) -> int:
    path, manifest = load_quiz_manifest(args.data_dir, args.quiz_id)
    answers = parse_quiz_answers(args.answers)
    questions = manifest["questions"]
    if len(answers) != len(questions):
        raise TutorError(f"答案数量为 {len(answers)}，题目数量为 {len(questions)}")
    confidences = parse_quiz_confidences(args.confidences, len(questions))
    invalidations = parse_quiz_marks(
        args.invalidate,
        len(questions),
        label="--invalidate",
        allowed=QUIZ_INVALIDATION_REASONS,
        aliases=QUIZ_INVALIDATION_ALIASES,
    )
    audits = parse_quiz_marks(args.audit, len(questions), label="--audit")
    declared_wrong_reasons = parse_quiz_marks(
        args.wrong_reason,
        len(questions),
        label="--wrong-reason",
        allowed=WRONG_REASONS - {"guessed_correct"},
    )
    response_key = {
        "answers": ["".join(answer) for answer in answers],
        "confidences": confidences,
    }
    if invalidations:
        response_key["invalidations"] = {
            str(number): reason for number, reason in sorted(invalidations.items())
        }
    if audits:
        response_key["audits"] = {
            str(number): note for number, note in sorted(audits.items())
        }
    if declared_wrong_reasons:
        response_key["wrong_reasons"] = {
            str(number): reason
            for number, reason in sorted(declared_wrong_reasons.items())
        }
    if manifest.get("status") == "graded":
        _, current_state = load_profile_and_state(args.data_dir)
        ensure_question_links_current(
            current_state, load_attempts(state_paths(args.data_dir)["attempts"])
        )
        if manifest.get("response_key") != response_key:
            raise TutorError(f"quiz-id {args.quiz_id} 已使用不同答案完成")
        replay = {
            **manifest["result"],
            "recorded_attempts": 0,
            "idempotent": True,
        }
        if args.prepare_next:
            advanced = prepare_next_quiz_payload(
                args.data_dir,
                path,
                manifest,
                replay,
                phase="grade",
                limit=args.next_limit,
            )
            output = (
                runtime_prepared_grade_payload(advanced)
                if getattr(args, "runtime_json", False)
                else advanced
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            output = (
                runtime_grade_payload(replay)
                if getattr(args, "runtime_json", False)
                else replay
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    profile, state = load_profile_and_state(args.data_dir)
    attempts_path = state_paths(args.data_dir)["attempts"]
    attempts = load_attempts(attempts_path)
    existing_by_id = {event["attempt_id"]: event for event in attempts}
    private_registry = load_private_question_registry(args.data_dir)
    graded_at = parse_datetime(args.at).isoformat(timespec="seconds")
    per_question_duration = (
        max(1, args.duration_seconds // len(questions))
        if args.duration_seconds is not None
        else None
    )
    events: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for index, (question, selected, confidence) in enumerate(
        zip(questions, answers, confidences, strict=True), 1
    ):
        correct = sorted(question["correct"])
        invalid_reason = invalidations.get(index)
        declared_reason = declared_wrong_reasons.get(index)
        if invalid_reason:
            if declared_reason:
                raise TutorError(f"第 {index} 题已标记无效，不能同时记录错因")
            # A broken question is not learner evidence: no attempt is written
            # and the rest of the batch still grades normally.
            results.append(
                {
                    "number": index,
                    "topic_id": question["topic_id"],
                    "response_state": "invalidated",
                    "selected": None,
                    "correct": None,
                    "is_correct": None,
                    "counted": False,
                    "invalid_reason": invalid_reason,
                    "message": "题目无效，本题不计分",
                }
            )
            continue
        conceded = selected == ["X"]
        is_correct = selected == correct
        if conceded:
            if declared_reason and declared_reason != "knowledge_gap":
                raise TutorError(f"第 {index} 题明确不会时错因只能是 knowledge_gap")
            wrong_reasons = ["knowledge_gap"]
            wrong_reason_source = "response_state"
            event_confidence = None
        elif is_correct:
            if declared_reason:
                raise TutorError(f"第 {index} 题答对，不能记录错因")
            wrong_reasons = ["guessed_correct"] if confidence == "guess" else []
            wrong_reason_source = "confidence" if wrong_reasons else None
            event_confidence = confidence
        else:
            wrong_reasons = [declared_reason] if declared_reason else []
            wrong_reason_source = "learner" if declared_reason else None
            event_confidence = confidence
        event = {
            "attempt_id": f"{args.quiz_id}-q-{index}",
            "event_type": "practice",
            "topic_id": question["topic_id"],
            "item_id": question["item_id"],
            "at": graded_at,
            "subject": manifest["subject"],
            "skill": "recognition",
            "mode": question.get("mode", "practice"),
            "score": 0 if conceded else (1 if is_correct else 0),
            "max_score": 1,
            "duration_seconds": per_question_duration,
            "word_count": None,
            "complete": False,
            "confidence": event_confidence,
            "response_state": "conceded" if conceded else "answered",
            "selected_answer": None if conceded else "".join(selected),
            "correct_answer": "".join(correct),
            "wrong_reasons": wrong_reasons,
            "wrong_reason_source": wrong_reason_source,
            "source_type": question["source_type"],
            "source": question.get("source"),
            "feedback_seen": False,
            "question_fingerprint": None,
            "variant_of": None,
        }
        event = question_registry.canonicalize_public_event(event)
        validate_record_event(event, curriculum)
        existing = existing_by_id.get(event["attempt_id"])
        if existing is not None and events_conflict(
            comparable_existing_event(existing, event),
            event,
            compare_at=bool(args.at),
        ):
            raise TutorError(f"attempt-id {event['attempt_id']} 与已记录内容冲突")
        events.append(event)
        results.append(
            {
                "number": index,
                "topic_id": question["topic_id"],
                "response_state": event["response_state"],
                "selected": None if conceded else "".join(selected),
                "correct": "".join(correct),
                "is_correct": is_correct,
                "counted": True,
                "confidence": event_confidence,
                "wrong_reasons": wrong_reasons,
                "wrong_reason_source": wrong_reason_source,
                "wrong_reason_status": (
                    "confirmed" if wrong_reasons else "unclassified"
                ),
                "prior_wrong_reasons": question.get("prior_wrong_reasons", []),
                "explanation": question.get("explanation"),
                "memory_hook": (
                    private_registry.get(question["item_id"], {}) or {}
                ).get("memory_hook"),
            }
        )

    next_state = copy.deepcopy(state)
    missing_events = [event for event in events if event["attempt_id"] not in existing_by_id]
    applied_results = {}
    for event in missing_events:
        applied_results[event["attempt_id"]] = apply_record_event(
            next_state, event, curriculum
        )
    minimum_interval = int(
        next_state.get("strategy", {}).get("min_review_interval_days", 0) or 0
    )
    counted_results = [result for result in results if result["counted"]]
    wrong_results = [
        result for result in counted_results if result["is_correct"] is False
    ]
    # The variant pool is only loaded when a wrong answer actually needs one.
    variant_pool = load_quiz_question_pool(curriculum) if wrong_results else []
    blocked_items = {question["item_id"] for question in questions} | {
        event.get("item_id") for event in attempts
    }
    recently_served_variants = recently_served_variant_item_ids(
        args.data_dir, parse_datetime(graded_at).date()
    )
    for result in counted_results:
        topic_record = next_state["topics"].get(result["topic_id"]) or state["topics"].get(
            result["topic_id"]
        )
        if not topic_record:
            result["topic_status"] = None
            result["next_review_at"] = None
            continue
        skill_record = topic_record["mastery"]["recognition"]
        result["topic_status"] = topic_record["status"]
        result["next_review_at"] = effective_review_date(
            skill_record.get("next_review_at"),
            skill_record.get("last_attempt_at"),
            minimum_interval,
        )
        if result["is_correct"] and result["confidence"] == "sure":
            result["explanation"] = None
        if result["is_correct"] is False:
            result["variant_question"] = pick_variant_question(
                questions[result["number"] - 1],
                variant_pool,
                topics,
                blocked_items,
                recently_served_variants,
            )
            if result["variant_question"] is not None:
                blocked_items.add(result["variant_question"]["item_id"])
        else:
            result["variant_question"] = None

    if audits:
        for result in results:
            if result["number"] in audits:
                result["needs_audit"] = True
                result["audit_note"] = audits[result["number"]]

    subject = status_payload(profile, next_state)["subjects"][manifest["subject"]]
    payload = {
        "quiz_id": args.quiz_id,
        "score": sum(1 for result in counted_results if result["is_correct"]),
        "max_score": len(counted_results),
        "counted_questions": len(counted_results),
        "invalidated_count": len(results) - len(counted_results),
        "conceded_count": sum(
            1 for result in counted_results if result["response_state"] == "conceded"
        ),
        "results": results,
        "recorded_attempts": len(missing_events),
        "idempotent": not missing_events,
        "subject_status": {
            "status": subject["status"],
            "latest_mock_score": subject.get("latest_mock_score"),
            "lower_bound_score": subject.get("lower_bound_score"),
            "evidence_level": subject.get("evidence_level"),
        },
    }
    payload["next_action"] = next_training_action(
        next_state,
        parse_datetime(graded_at).date(),
        quiz_id=args.quiz_id,
        variant_count=sum(
            1
            for result in results
            if isinstance(result.get("variant_question"), dict)
        ),
    )
    completed = {
        **manifest,
        "status": "graded",
        "graded_at": graded_at,
        "response_key": response_key,
        "result": payload,
    }
    # Resolve every fallible read and build the complete response before
    # committing learner evidence. The audit queue is maintenance metadata, so
    # write it first and make that write idempotent; a retry after any later
    # failure cannot duplicate it.
    if audits:
        append_quiz_audit_entries(
            args.data_dir, args.quiz_id, audits, questions, graded_at
        )
    if missing_events:
        write_attempts(attempts_path, [*attempts, *missing_events])
        save_state_bundle(args.data_dir, profile, next_state, backup=True)
    atomic_write_json(path, completed)
    if args.prepare_next:
        advanced = prepare_next_quiz_payload(
            args.data_dir,
            path,
            completed,
            payload,
            phase="grade",
            limit=args.next_limit,
        )
        output = (
            runtime_prepared_grade_payload(advanced)
            if getattr(args, "runtime_json", False)
            else advanced
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        output = (
            runtime_grade_payload(payload)
            if getattr(args, "runtime_json", False)
            else payload
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_quiz_variant_grade(args: argparse.Namespace) -> int:
    """Record the follow-up variants a graded round handed to the learner.

    A variant is answered in chat and would otherwise vanish, leaving the
    picker blind to what was already served and the review ladder blind to a
    missed follow-up.  Each answer becomes a normal attempt whose
    ``variant_of`` points back at the question that produced it.
    """

    path, manifest = load_quiz_manifest(args.data_dir, args.quiz_id)
    graded = manifest.get("result")
    if manifest.get("status") != "graded" or not isinstance(graded, dict):
        raise TutorError("变式判分要求本组已完成判分，请先运行 quiz-grade")
    served: list[tuple[int, dict[str, Any]]] = []
    for position, entry in enumerate(graded.get("results") or [], 1):
        if not isinstance(entry, dict):
            continue
        variant = entry.get("variant_question")
        if isinstance(variant, dict):
            number = entry.get("number")
            served.append((number if isinstance(number, int) else position, variant))
    if not served:
        raise TutorError(f"quiz-id {args.quiz_id} 本组没有变式题，无需判分")
    answers = parse_quiz_answers(args.answers)
    if len(answers) != len(served):
        raise TutorError(f"变式答案数量为 {len(answers)}，变式题数量为 {len(served)}")
    confidences = parse_variant_confidences(args.confidences, len(served))
    declared_wrong_reasons = parse_quiz_marks(
        args.wrong_reason,
        len(served),
        label="--wrong-reason",
        allowed=WRONG_REASONS - {"guessed_correct"},
    )
    variant_key = {
        "answers": ["".join(answer) for answer in answers],
        "confidences": confidences,
    }
    if declared_wrong_reasons:
        variant_key["wrong_reasons"] = {
            str(number): reason
            for number, reason in sorted(declared_wrong_reasons.items())
        }
    if manifest.get("variant_result") is not None:
        _, current_state = load_profile_and_state(args.data_dir)
        ensure_question_links_current(
            current_state, load_attempts(state_paths(args.data_dir)["attempts"])
        )
        if manifest.get("variant_key") != variant_key:
            raise TutorError(f"quiz-id {args.quiz_id} 的变式题已使用不同答案完成")
        replay = {
            **manifest["variant_result"],
            "recorded_attempts": 0,
            "idempotent": True,
        }
        if args.prepare_next:
            advanced = prepare_next_quiz_payload(
                args.data_dir,
                path,
                manifest,
                replay,
                phase="variant",
                limit=args.next_limit,
            )
            output = (
                runtime_prepared_grade_payload(advanced)
                if getattr(args, "runtime_json", False)
                else advanced
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            output = (
                runtime_grade_payload(replay)
                if getattr(args, "runtime_json", False)
                else replay
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    questions = manifest["questions"]
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    profile, state = load_profile_and_state(args.data_dir)
    attempts_path = state_paths(args.data_dir)["attempts"]
    attempts = load_attempts(attempts_path)
    existing_by_id = {event["attempt_id"]: event for event in attempts}
    private_registry = load_private_question_registry(args.data_dir)
    graded_at = parse_datetime(args.at).isoformat(timespec="seconds")
    # Resolve the item against the current quality gate and topic mapping.
    pool = load_quiz_question_pool(curriculum)
    resolved: dict[str, dict[str, Any]] = {}
    for _, variant in served:
        item_id = variant.get("item_id")
        topic = topics.get(variant.get("topic_id"))
        if not isinstance(item_id, str) or topic is None:
            raise TutorError(f"变式题元数据无效：{item_id}")
        for raw in pool:
            candidate = quiz_question_for_topic(raw, topic)
            if candidate is not None and candidate["item_id"] == item_id:
                resolved[item_id] = candidate
                break
        if item_id not in resolved:
            raise TutorError(f"变式题已不在可出题池中：{item_id}")

    events: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for index, ((source_number, variant), selected, confidence) in enumerate(
        zip(served, answers, confidences, strict=True), 1
    ):
        candidate = resolved[variant["item_id"]]
        source = (
            questions[source_number - 1] if 1 <= source_number <= len(questions) else {}
        )
        correct = sorted(variant["answer"])
        conceded = selected == ["X"]
        is_correct = selected == correct
        declared_reason = declared_wrong_reasons.get(index)
        if conceded:
            if declared_reason and declared_reason != "knowledge_gap":
                raise TutorError(f"第 {index} 道变式明确不会时错因只能是 knowledge_gap")
            wrong_reasons = ["knowledge_gap"]
            wrong_reason_source = "response_state"
            event_confidence = None
        elif is_correct:
            if declared_reason:
                raise TutorError(f"第 {index} 道变式答对，不能记录错因")
            wrong_reasons = ["guessed_correct"] if confidence == "guess" else []
            wrong_reason_source = "confidence" if wrong_reasons else None
            event_confidence = confidence
        else:
            wrong_reasons = [declared_reason] if declared_reason else []
            wrong_reason_source = "learner" if declared_reason else None
            event_confidence = confidence
        event = {
            "attempt_id": f"{args.quiz_id}-v-{index}",
            "event_type": "practice",
            "topic_id": candidate["topic_id"],
            "item_id": candidate["item_id"],
            "at": graded_at,
            "subject": manifest["subject"],
            "skill": "recognition",
            "mode": "review",
            "score": 0 if conceded else (1 if is_correct else 0),
            "max_score": 1,
            "duration_seconds": None,
            "word_count": None,
            "complete": False,
            "confidence": event_confidence,
            "response_state": "conceded" if conceded else "answered",
            "selected_answer": None if conceded else "".join(selected),
            "correct_answer": "".join(correct),
            "wrong_reasons": wrong_reasons,
            "wrong_reason_source": wrong_reason_source,
            "source_type": candidate["source_type"],
            "source": candidate.get("source"),
            "feedback_seen": False,
            "question_fingerprint": None,
            "variant_of": source.get("item_id"),
        }
        event = question_registry.canonicalize_public_event(event)
        validate_record_event(event, curriculum)
        existing = existing_by_id.get(event["attempt_id"])
        if existing is not None and events_conflict(
            comparable_existing_event(existing, event),
            event,
            compare_at=bool(args.at),
        ):
            raise TutorError(f"attempt-id {event['attempt_id']} 与已记录内容冲突")
        events.append(event)
        results.append(
            {
                "number": index,
                "source_number": source_number,
                "item_id": candidate["item_id"],
                "topic_id": candidate["topic_id"],
                "response_state": event["response_state"],
                "selected": None if conceded else "".join(selected),
                "correct": "".join(correct),
                "is_correct": is_correct,
                "confidence": event_confidence,
                "wrong_reasons": wrong_reasons,
                "wrong_reason_source": wrong_reason_source,
                "wrong_reason_status": (
                    "confirmed" if wrong_reasons else "unclassified"
                ),
                "memory_hook": (
                    private_registry.get(candidate["item_id"], {}) or {}
                ).get("memory_hook"),
            }
        )

    next_state = copy.deepcopy(state)
    missing_events = [
        event for event in events if event["attempt_id"] not in existing_by_id
    ]
    for event in missing_events:
        apply_record_event(next_state, event, curriculum)
    minimum_interval = int(
        next_state.get("strategy", {}).get("min_review_interval_days", 0) or 0
    )
    for result in results:
        topic_record = next_state["topics"].get(result["topic_id"]) or state[
            "topics"
        ].get(result["topic_id"])
        if not topic_record:
            result["topic_status"] = None
            result["next_review_at"] = None
            continue
        skill_record = (topic_record.get("mastery") or {}).get("recognition") or {}
        result["topic_status"] = topic_record.get("status")
        result["next_review_at"] = effective_review_date(
            skill_record.get("next_review_at"),
            skill_record.get("last_attempt_at"),
            minimum_interval,
        )

    payload = {
        "quiz_id": args.quiz_id,
        "score": sum(1 for result in results if result["is_correct"]),
        "max_score": len(results),
        "results": results,
        "recorded_attempts": len(missing_events),
        "idempotent": not missing_events,
    }
    payload["next_action"] = next_training_action(
        next_state, parse_datetime(graded_at).date()
    )
    completed = {
        **manifest,
        "variant_key": variant_key,
        "variant_graded_at": graded_at,
        "variant_result": payload,
    }
    if missing_events:
        write_attempts(attempts_path, [*attempts, *missing_events])
        save_state_bundle(args.data_dir, profile, next_state, backup=True)
    atomic_write_json(path, completed)
    if args.prepare_next:
        advanced = prepare_next_quiz_payload(
            args.data_dir,
            path,
            completed,
            payload,
            phase="variant",
            limit=args.next_limit,
        )
        output = (
            runtime_prepared_grade_payload(advanced)
            if getattr(args, "runtime_json", False)
            else advanced
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        output = (
            runtime_grade_payload(payload)
            if getattr(args, "runtime_json", False)
            else payload
        )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def privacy_check(data_dir: Path) -> tuple[bool, str]:
    try:
        relative_data_dir = data_dir.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return True, "私人目录位于仓库外，不会被此仓库提交"

    if not (REPO_ROOT / ".git").exists():
        safe_name = relative_data_dir.parts and (
            relative_data_dir.parts[0] == ".study"
            or relative_data_dir.parts[0].startswith(".study-")
        )
        return (
            bool(safe_name),
            f"{relative_data_dir} 匹配私人目录规则"
            if safe_name
            else f"{relative_data_dir} 不匹配私人目录规则",
        )

    candidate = (relative_data_dir / "state.json").as_posix()
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", candidate],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0
    return (
        ignored,
        f"{relative_data_dir} 已被 Git 忽略"
        if ignored
        else f"{relative_data_dir} 未被 Git 忽略",
    )


def doctor_checks(data_dir: Path) -> tuple[bool, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    healthy = True

    try:
        curriculum = load_curriculum()
        missing = []
        for topic in curriculum["topics"]:
            for resource in topic.get("resources", []):
                if not (REPO_ROOT / resource.split("#", 1)[0]).exists():
                    missing.append(resource)
        ok = not missing
        checks.append(
            {
                "name": "curriculum",
                "healthy": ok,
                "message": "课程表与资源有效" if ok else "缺少资源：" + ", ".join(missing),
            }
        )
        healthy = healthy and ok
    except TutorError as error:
        checks.append({"name": "curriculum", "healthy": False, "message": str(error)})
        healthy = False

    paths = state_paths(data_dir)
    state_path = paths["state"]
    if not state_path.exists():
        partial = [
            path.name
            for key, path in paths.items()
            if key != "state" and path.exists()
        ]
        backup_path = state_path.with_name("state.json.bak")
        if backup_path.exists():
            partial.append(backup_path.name)
        ok = not partial
        checks.append(
            {
                "name": "state",
                "healthy": ok,
                "message": "尚未建档；没有学习进度可读取"
                if ok
                else "建档不完整，可运行 repair：" + ", ".join(sorted(partial)),
            }
        )
        healthy = healthy and ok
    else:
        try:
            stored_state = validate_state(load_json(state_path, "学习状态"))
            _, state = load_profile_and_state(data_dir, persist_pending=False)
            attempts = load_attempts(paths["attempts"])
            load_private_question_registry(data_dir)
            backup_path = state_path.with_name("state.json.bak")
            validate_state(load_json(backup_path, "状态备份"))
            logged_ids = {event["attempt_id"] for event in attempts}
            applied_ids = set(state["applied_attempt_ids"])
            if logged_ids != applied_ids:
                raise TutorError("状态与事件日志集合不一致，请运行 repair")
            ensure_question_links_current(state, attempts)
            pending_count = len(logged_ids - set(stored_state["applied_attempt_ids"]))
            if pending_count:
                checks.append(
                    {
                        "name": "state",
                        "healthy": False,
                        "message": (
                            f"事件日志有 {pending_count} 条尚未回放到状态；"
                            "只读查询已在内存投影，下次有效状态写入会持久化"
                        ),
                    }
                )
                healthy = False
            else:
                checks.append(
                    {
                        "name": "state",
                        "healthy": True,
                        "message": "私人状态、事件日志与备份一致",
                    }
                )
        except TutorError as error:
            checks.append(
                {
                    "name": "state",
                    "healthy": False,
                    "message": (
                        f"需迁移：{error}"
                        if isinstance(error, QuestionLinkMigrationRequired)
                        else f"invalid/corrupt: {error}"
                    ),
                }
            )
            healthy = False

    ignored, privacy_message = privacy_check(data_dir)
    checks.append(
        {
            "name": "privacy",
            "healthy": ignored,
            "message": privacy_message,
        }
    )
    healthy = healthy and ignored

    try:
        curriculum = load_curriculum()
        topics = topic_map(curriculum)
        pool = load_quiz_question_pool(curriculum)
        ready = [
            item
            for item in pool
            if item.get("quality_status") == "ready"
            and item.get("teaching_status") == "ready"
        ]
        routable = [
            item for item in ready
            if any(
                topic_id in topics
                and quiz_question_for_topic(item, topics[topic_id]) is not None
                for topic_id in item.get("candidate_topics", [])
            )
        ]
        unmapped = len(ready) - len(routable)
        referenced_bank_files = {
            resource
            for topic in curriculum["topics"]
            for resource in topic.get("resources", [])
            if resource.startswith("exam-bank/")
        }
        unreferenced_bank_files = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in sorted((REPO_ROOT / "exam-bank").glob("*.md"))
            if path.name != "README.md"
            and path.relative_to(REPO_ROOT).as_posix() not in referenced_bank_files
            and sanitize_bank.parse_exam_bank(path)
        ]
        blocked = [item for item in pool if item not in ready]
        reason_counts: dict[str, int] = {}
        for item in blocked:
            issues = list(item.get("quality_issues", []))
            if item.get("teaching_status") == "missing_explanation":
                issues.append("missing_explanation")
            for issue in issues:
                name = issue.split(":", 1)[0]
                reason_counts[name] = reason_counts.get(name, 0) + 1
        top_reasons = ", ".join(
            f"{name} {count}"
            for name, count in sorted(
                reason_counts.items(), key=lambda item: (-item[1], item[0])
            )[:4]
        )
        audit_path = data_dir / "quiz-audit-queue.jsonl"
        open_audits = (
            len(
                [
                    line
                    for line in audit_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            )
            if audit_path.exists()
            else 0
        )
        ok = bool(routable) and not unmapped and not unreferenced_bank_files
        checks.append(
            {
                "name": "question-bank",
                "healthy": ok,
                "message": (
                    f"可出题 {len(routable)} 道，质量门禁拦下 {len(blocked)} 道"
                    + (f"（{top_reasons}）" if top_reasons else "")
                    + f"，考点映射未入池 {unmapped} 道"
                    + f"，未接入题库文件 {len(unreferenced_bank_files)} 个"
                    + ("（" + "、".join(unreferenced_bank_files) + "）" if unreferenced_bank_files else "")
                    + f"，待维护核对 {open_audits} 条"
                ),
            }
        )
        healthy = healthy and ok
    except (TutorError, ValueError) as error:
        checks.append(
            {"name": "question-bank", "healthy": False, "message": str(error)}
        )
        healthy = False

    try:
        snapshot = load_json(FREQUENCY_SNAPSHOT_PATH, "考频快照")
        checked = subprocess.run(
            [sys.executable, str(FREQUENCY_BUILDER_PATH), "--check"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        coverage = snapshot.get("coverage", {})
        stable = float(coverage.get("stable", {}).get("ratio", 0))
        recent = float(coverage.get("recent", {}).get("ratio", 0))
        ok = checked.returncode == 0
        checks.append(
            {
                "name": "frequency-model",
                "healthy": ok,
                "message": (
                    f"快照 {snapshot.get('mode', 'unknown')}；"
                    f"稳定层映射 {stable:.1%}，趋势层映射 {recent:.1%}"
                    if ok
                    else (checked.stderr.strip() or "考频快照已过期")
                ),
            }
        )
        healthy = healthy and ok
    except (TutorError, ValueError, TypeError) as error:
        checks.append(
            {"name": "frequency-model", "healthy": False, "message": str(error)}
        )
        healthy = False
    return healthy, checks


def cmd_doctor(args: argparse.Namespace) -> int:
    healthy, checks = doctor_checks(args.data_dir)
    payload = {"healthy": healthy, "checks": checks}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for check in checks:
            marker = "PASS" if check["healthy"] else "FAIL"
            print(f"[{marker}] {check['name']}: {check['message']}")
    return 0 if healthy else 1


def normalize_question_links(data_dir: Path) -> int:
    """Migrate verified question links without rewriting answer evidence."""

    paths = state_paths(data_dir)
    profile = load_json(paths["profile"], "私人档案")
    if not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA_VERSION:
        raise TutorError("profile.json schema_version 不受支持")
    current = validate_state(load_json(paths["state"], "学习状态"))
    attempts = load_attempts(paths["attempts"])
    if set(current["applied_attempt_ids"]) != {event["attempt_id"] for event in attempts}:
        raise TutorError("状态与事件日志集合不一致，请先修复状态")
    validate_state(load_json(paths["state"].with_name("state.json.bak"), "状态备份"))

    corrections = [
        (event, question_registry.canonicalize_public_event(event))
        for event in attempts
        if question_registry.canonicalize_public_event(event) != event
    ]
    impacted_topics = {
        topic_id
        for old, new in corrections
        for topic_id in (old.get("topic_id"), new.get("topic_id"))
        if isinstance(topic_id, str)
    }
    curriculum = load_curriculum()
    topics = topic_map(curriculum)
    rebuilt = copy.deepcopy(current)
    if impacted_topics:
        scratch = new_state(curriculum, str(profile.get("created_at") or now_iso()))
        scratch["strategy"] = copy.deepcopy(current["strategy"])
        for event in attempts:
            if event.get("event_type") == "mock" or event.get("mode") == "full_mock":
                continue
            corrected = question_registry.canonicalize_public_event(event)
            if event.get("topic_id") in impacted_topics or corrected.get("topic_id") in impacted_topics:
                apply_record_event(scratch, corrected, curriculum)
        for topic_id in impacted_topics:
            rebuilt["topics"].pop(topic_id, None)
            if topic_id in scratch["topics"]:
                rebuilt["topics"][topic_id] = scratch["topics"][topic_id]
    rebuilt["question_link_version"] = QUESTION_LINK_VERSION
    validate_state(rebuilt)
    if (
        rebuilt["subjects"] != current["subjects"]
        or rebuilt["strategy"] != current["strategy"]
        or rebuilt["applied_attempt_ids"] != current["applied_attempt_ids"]
    ):
        raise TutorError("迁移预检发现非题目关联字段变化，已取消")

    changed_manifests: dict[Path, dict[str, Any]] = {}
    directory = quiz_sessions_dir(data_dir)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            manifest = load_json(path, "客观题会话")
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != QUIZ_SCHEMA_VERSION
                or not isinstance(manifest.get("questions"), list)
            ):
                raise TutorError(f"客观题会话损坏：{path}")
            normalized = normalized_quiz_manifest(manifest, topics)
            if normalized != manifest:
                changed_manifests[path] = normalized
    if rebuilt == current and not changed_manifests:
        print("题目关联已是最新版本，无需迁移。")
        return 0

    source_paths = [
        paths["state"],
        paths["state"].with_name("state.json.bak"),
        paths["dashboard"],
        paths["attempts"],
        *changed_manifests,
    ]
    originals = {path: path.read_bytes() for path in source_paths}
    log_hash = hashlib.sha256(originals[paths["attempts"]]).digest()
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
    backup_dir = data_dir / "migration-backups" / f"question-links-{stamp}"
    for path, content in originals.items():
        atomic_write_bytes(backup_dir / path.relative_to(data_dir), content)
    try:
        save_state_bundle(data_dir, profile, rebuilt, backup=True)
        for path, manifest in changed_manifests.items():
            atomic_write_json(path, manifest)
        if hashlib.sha256(paths["attempts"].read_bytes()).digest() != log_hash:
            raise TutorError("迁移期间作答日志发生变化")
    except Exception as error:
        restoration_errors = []
        for path, content in originals.items():
            if path == paths["attempts"]:
                continue  # The migration never writes the evidence ledger.
            try:
                atomic_write_bytes(path, content)
            except OSError as restore_error:
                restoration_errors.append(str(restore_error))
        if restoration_errors:
            raise TutorError(
                f"迁移失败且自动恢复不完整；原文件备份位于 {backup_dir}："
                + "; ".join(restoration_errors)
            ) from error
        raise TutorError(f"迁移失败，已从 {backup_dir} 恢复：{error}") from error
    print(
        f"已迁移 {len(corrections)} 条历史题目关联、{len(changed_manifests)} 份客观题会话；"
        f"原作答日志未改动；备份：{backup_dir}"
    )
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    if args.normalize_question_links:
        return normalize_question_links(args.data_dir)
    paths = state_paths(args.data_dir)
    state_path = paths["state"]
    backup_path = state_path.with_name(state_path.name + ".bak")
    profile = load_json(paths["profile"], "私人档案")
    if not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA_VERSION:
        raise TutorError("profile.json schema_version 不受支持")
    attempts = load_attempts(paths["attempts"])
    logged_ids = {event["attempt_id"] for event in attempts}
    current: dict[str, Any] | None = None
    try:
        current = validate_state(load_json(state_path, "学习状态"))
    except TutorError:
        pass
    else:
        if set(current["applied_attempt_ids"]) == logged_ids and not args.recompute_derived:
            try:
                validate_state(load_json(backup_path, "状态备份"))
            except TutorError:
                pass
            else:
                print("状态、事件日志与备份有效，无需修复。")
                return 0

    curriculum = load_curriculum()
    source_state = current
    if source_state is None:
        try:
            source_state = validate_state(load_json(backup_path, "状态备份"))
        except TutorError:
            source_state = None
    rebuilt = new_state(curriculum, str(profile.get("created_at") or now_iso()))
    if source_state is not None and isinstance(source_state.get("strategy"), dict):
        for key in (
            "pass_line",
            "safe_target",
            "min_review_interval_days",
            "case_tracks",
            "essay_themes",
            "case_tracks_configured",
            "essay_themes_configured",
            "strategic_skips",
            "subject_policies",
        ):
            if key in source_state["strategy"]:
                rebuilt["strategy"][key] = source_state["strategy"][key]
    try:
        for event in attempts:
            apply_event_to_state(rebuilt, event, curriculum)
        validate_state(rebuilt)
    except TutorError as error:
        raise TutorError(f"事件日志无法确定性重建，已保留原文件：{error}") from error

    corrupt_path: Path | None = None
    if state_path.exists():
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
        suffix = "pre-recompute" if args.recompute_derived else "corrupt"
        corrupt_path = state_path.with_name(f"{state_path.name}.{suffix}.{stamp}")
        atomic_write_bytes(corrupt_path, state_path.read_bytes())
    save_state_bundle(args.data_dir, profile, rebuilt, backup=True)
    if corrupt_path is None:
        print("state.json 缺失，已依据事件日志重建。")
    else:
        print(f"已依据事件日志重建；原状态保留为 {corrupt_path.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="系统架构设计师过线私教的本地进度引擎"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / ".study",
        help="私人状态目录（默认：仓库根目录 .study）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="建立或恢复私人学习档案")
    init_parser.add_argument("--exam-date")
    init_parser.add_argument("--daily-minutes", type=int, default=45)
    init_parser.add_argument("--background", default="")
    init_parser.set_defaults(func=cmd_init)

    status_parser = subparsers.add_parser("status", help="查看三科独立进度")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    progress_parser = subparsers.add_parser(
        "progress", help="只读汇总三科状态、薄弱点与下一步"
    )
    progress_parser.add_argument("--limit", type=int, default=5)
    progress_parser.add_argument("--days", type=int, default=21)
    progress_parser.add_argument("--today")
    progress_output = progress_parser.add_mutually_exclusive_group()
    progress_output.add_argument("--json", action="store_true")
    progress_output.add_argument(
        "--runtime-json",
        action="store_true",
        help="只输出当前训练回合所需的路由与目标科目状态",
    )
    progress_parser.set_defaults(func=cmd_progress)

    recommend_parser = subparsers.add_parser("recommend", help="推荐下一项高收益任务")
    recommend_parser.add_argument("--json", action="store_true")
    recommend_parser.add_argument("--subject", choices=SUBJECTS)
    recommend_parser.add_argument("--limit", type=int, default=5)
    recommend_parser.add_argument("--today")
    recommend_parser.set_defaults(func=cmd_recommend)

    quiz_prepare_parser = subparsers.add_parser(
        "quiz-prepare", help="一次完成客观题诊断、选题和脱敏"
    )
    quiz_prepare_parser.add_argument(
        "--subject", choices=("comprehensive",), default="comprehensive"
    )
    quiz_prepare_parser.add_argument(
        "--topic", help="锁定 progress 返回的综合知识稳定考点 ID"
    )
    quiz_prepare_parser.add_argument("--limit", type=int, default=5)
    quiz_prepare_parser.add_argument("--today")
    quiz_prepare_parser.set_defaults(func=cmd_quiz_prepare)

    case_prepare_parser = subparsers.add_parser(
        "case-prepare",
        help="一次完成案例薄弱点路由、真题选取与插图完整性检查（只读）",
    )
    case_prepare_parser.add_argument(
        "--topic",
        help="可选：明确指定支持 application 的稳定考点 ID；缺省时使用自适应推荐",
    )
    case_prepare_parser.add_argument("--today")
    case_prepare_parser.add_argument(
        "--allow-missing-figures",
        action="store_true",
        help="显式允许插图被移除的案例题；默认只选择材料完整的盲练题",
    )
    case_prepare_parser.set_defaults(func=cmd_case_prepare)

    quiz_grade_parser = subparsers.add_parser(
        "quiz-grade", help="一次完成客观题判分、批量记档和状态更新"
    )
    quiz_grade_parser.add_argument("--quiz-id", required=True)
    quiz_grade_parser.add_argument("--answers", required=True)
    quiz_grade_parser.add_argument("--confidences")
    quiz_grade_parser.add_argument(
        "--invalidate",
        help="把坏题排除出本组，写成 题号=原因（如 4=missing_required_table）",
    )
    quiz_grade_parser.add_argument(
        "--audit",
        help="标记需要维护核对的题，写成 题号=说明（如 3=答案键疑似有误）",
    )
    quiz_grade_parser.add_argument(
        "--wrong-reason",
        help="仅记录考生明确说明的错因，写成 题号=原因（可用分号分隔）",
    )
    quiz_grade_parser.add_argument("--duration-seconds", type=int)
    quiz_grade_parser.add_argument("--at")
    quiz_grade_parser.add_argument(
        "--prepare-next",
        action="store_true",
        help="判分后按 next_action 在同一命令内准备下一组综合知识题",
    )
    quiz_grade_parser.add_argument(
        "--next-limit",
        type=int,
        default=5,
        help="--prepare-next 创建的下一组题目数量（默认：5）",
    )
    quiz_grade_parser.add_argument(
        "--runtime-json",
        action="store_true",
        help="输出判分与续练题面所需的紧凑 JSON",
    )
    quiz_grade_parser.set_defaults(func=cmd_quiz_grade)

    quiz_variant_parser = subparsers.add_parser(
        "quiz-variant-grade",
        help="记录并判分上一组给出的变式题（作答后调用一次）",
    )
    quiz_variant_parser.add_argument("--quiz-id", required=True)
    quiz_variant_parser.add_argument(
        "--answers",
        required=True,
        help="按变式出现顺序作答，逗号分隔；明确不会写 X",
    )
    quiz_variant_parser.add_argument("--confidences")
    quiz_variant_parser.add_argument(
        "--wrong-reason",
        help="仅记录考生明确说明的变式错因，写成 题号=原因",
    )
    quiz_variant_parser.add_argument("--at")
    quiz_variant_parser.add_argument(
        "--prepare-next",
        action="store_true",
        help="变式判分后按 next_action 在同一命令内准备下一组综合知识题",
    )
    quiz_variant_parser.add_argument(
        "--next-limit",
        type=int,
        default=5,
        help="--prepare-next 创建的下一组题目数量（默认：5）",
    )
    quiz_variant_parser.add_argument(
        "--runtime-json",
        action="store_true",
        help="输出判分与续练题面所需的紧凑 JSON",
    )
    quiz_variant_parser.set_defaults(func=cmd_quiz_variant_grade)

    configure_parser = subparsers.add_parser(
        "configure", help="保存诊断后的案例赛道与论文主题"
    )
    configure_parser.add_argument("--case-track", action="append")
    configure_parser.add_argument("--essay-theme", action="append")
    configure_parser.add_argument("--skip-topic", action="append")
    configure_parser.add_argument("--unskip-topic", action="append")
    configure_parser.add_argument("--min-review-interval-days", type=int)
    configure_parser.add_argument(
        "--subject-policy",
        action="append",
        help="学科启停策略，写成 科目=模式（manual_trigger / active）",
    )
    configure_parser.add_argument("--subject-policy-reason")
    configure_parser.set_defaults(func=cmd_configure)

    record_parser = subparsers.add_parser("record", help="记录一次有效作答")
    record_parser.add_argument("--topic", required=True)
    record_parser.add_argument("--skill", choices=SKILLS, required=True)
    record_parser.add_argument("--score", type=float, required=True)
    record_parser.add_argument("--max-score", type=float, required=True)
    record_parser.add_argument("--attempt-id", required=True)
    record_parser.add_argument("--item-id", required=True)
    record_parser.add_argument("--at")
    record_parser.add_argument("--subject", choices=SUBJECTS)
    record_parser.add_argument("--wrong-reason", action="append")
    record_parser.add_argument("--source")
    record_parser.add_argument(
        "--source-type",
        choices=("official_outline", "real", "recalled_real", "self_authored", "simulation"),
        default="self_authored",
    )
    record_parser.add_argument("--duration-seconds", type=int)
    record_parser.add_argument("--word-count", type=int)
    record_parser.add_argument("--complete", action="store_true")
    record_parser.add_argument("--confidence", choices=("guess", "unsure", "sure"), default="sure")
    record_parser.add_argument("--question-fingerprint")
    record_parser.add_argument("--variant-of")
    record_parser.add_argument(
        "--mode",
        choices=("diagnostic", "practice", "review", "mock", "full_timed"),
        default="practice",
    )
    record_parser.set_defaults(func=cmd_record)

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="合并最近模考、错因与后续补练，列出具体薄弱点"
    )
    diagnose_parser.add_argument("--subject", choices=SUBJECTS, default="comprehensive")
    diagnose_parser.add_argument("--today")
    diagnose_parser.add_argument("--json", action="store_true")
    diagnose_parser.set_defaults(func=cmd_diagnose)

    weakpoints_parser = subparsers.add_parser(
        "weakpoints", help="按考点列出到期、近期正确率与未覆盖薄弱点（只读）"
    )
    weakpoints_parser.add_argument(
        "--subject", choices=SUBJECTS, default="comprehensive"
    )
    weakpoints_parser.add_argument("--days", type=int, default=21)
    weakpoints_parser.add_argument("--limit", type=int, default=10)
    weakpoints_parser.add_argument("--today")
    weakpoints_parser.add_argument("--json", action="store_true")
    weakpoints_parser.set_defaults(func=cmd_weakpoints)

    register_parser = subparsers.add_parser(
        "register-question", help="登记私人自编题的身份与内容指纹"
    )
    register_parser.add_argument("--file", type=Path, required=True)
    register_parser.set_defaults(func=cmd_register_question)

    mock_parser = subparsers.add_parser("mock", help="记录一科完整限时成绩")
    mock_parser.add_argument("--subject", choices=SUBJECTS, required=True)
    mock_parser.add_argument("--mock-id", required=True)
    mock_parser.add_argument("--paper-id", required=True)
    mock_parser.add_argument("--score", type=float, required=True)
    mock_parser.add_argument("--max-score", type=float, default=75)
    mock_parser.add_argument("--duration-minutes", type=float, required=True)
    mock_parser.add_argument("--complete", action="store_true")
    mock_parser.add_argument(
        "--source-type",
        choices=("real", "recalled_real", "self_authored", "simulation"),
        default="simulation",
    )
    mock_parser.add_argument("--at")
    mock_parser.set_defaults(func=cmd_mock)

    doctor_parser = subparsers.add_parser("doctor", help="检查课程、状态和隐私设置")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(func=cmd_doctor)

    repair_parser = subparsers.add_parser("repair", help="从最近有效备份恢复损坏状态")
    repair_modes = repair_parser.add_mutually_exclusive_group()
    repair_modes.add_argument(
        "--recompute-derived",
        action="store_true",
        help="即使当前状态有效，也按事件日志重算复习日期和派生统计",
    )
    repair_modes.add_argument(
        "--normalize-question-links",
        action="store_true",
        help="只重建已核实的错标考点并纠正会话元数据，保留原作答日志和私人备份",
    )
    repair_parser.set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()
    if getattr(args, "limit", 1) <= 0:
        print("错误：limit 必须大于 0", file=sys.stderr)
        return 2
    try:
        private, privacy_message = privacy_check(args.data_dir)
        if not private:
            raise TutorError(
                f"拒绝读取或写入可能被 Git 跟踪的私人目录：{privacy_message}"
            )
        if args.command == "init":
            args.data_dir.mkdir(parents=True, exist_ok=True)
        if args.data_dir.exists():
            with data_lock(args.data_dir):
                return int(args.func(args))
        return int(args.func(args))
    except TutorError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
