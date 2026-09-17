"""Stable question metadata used by mock diagnosis and quiz deduplication."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]

# Hand-curated fine-grained concepts for mock questions where the whole topic
# is too coarse a merge unit. Every item without an override falls back to its
# stable topic (+facet) so mock gaps always merge with later same-topic
# variants; per-item composite ids would make each question its own concept
# and remediation structurally impossible.
CONCEPT_OVERRIDES = {
    "exam-bank/05-uml.md#1": ("K03.uml_diagram_count", "K03.uml_diagram_count"),
    "exam-bank/05-uml.md#4": ("K03.uml_relationships", "K03.uml_relationships"),
    "exam-bank/05-uml.md#5": ("K03.uml_relationships", "K03.uml_relationships"),
    "exam-bank/02-os-concepts.md#3": ("K01.deadlock_avoidance", "K01.deadlock_avoidance"),
    "exam-bank/01-computer-systems.md#1": ("K18.mips_cpi", "K18.mips_cpi"),
    "exam-bank/21-security.md#1": ("K20.cia_triad", "K20.cia_triad"),
    "exam-bank/23-english-reading.md#3": ("K22.cloud_cost_vocabulary", "K22.cloud_cost_vocabulary"),
}

CONCEPT_LABELS = {
    "K03.uml_diagram_count": "UML 2.x 图分类与数量",
    "K03.uml_relationships": "UML 泛化、实现与依赖关系",
    "K01.deadlock_avoidance": "银行家算法与死锁避免",
    "K18.mips_cpi": "主频、CPI 与 MIPS 计算",
    "K20.cia_triad": "信息安全 CIA 三要素",
    "K22.cloud_cost_vocabulary": "云计算成本语境词汇",
}


@lru_cache(maxsize=1)
def _topic_names() -> dict[str, str]:
    """Stable topic id → readable name, used as the concept label fallback."""

    try:
        curriculum = json.loads(
            (REPO_ROOT / "tutor" / "curriculum.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        topic["id"]: topic["name"]
        for topic in curriculum.get("topics", [])
        if isinstance(topic, dict) and topic.get("id") and topic.get("name")
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def content_fingerprint(stem: str, options: Iterable[str]) -> str:
    normalized_options = sorted(normalize_text(option) for option in options)
    payload = json.dumps(
        [normalize_text(stem), *normalized_options],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_metadata(item_id: str, topic_id: str, facet: str | None = None) -> dict[str, str]:
    override = CONCEPT_OVERRIDES.get(item_id)
    if override:
        concept_id, family_id = override
    else:
        # Stable merge unit for every un-curated item: the curriculum topic,
        # split by facet when the topic declares one, so all same-topic mock
        # questions and later variants resolve to one concept id.
        concept_id = f"{topic_id}:{facet}" if facet else topic_id
        family_id = concept_id
    return {
        "item_id": item_id,
        "topic_id": topic_id,
        "concept_id": concept_id,
        "question_family_id": family_id,
        "concept_label": (
            CONCEPT_LABELS.get(concept_id)
            or _topic_names().get(topic_id)
            or exam_question_stem(item_id)
            or concept_id
        ),
    }


@lru_cache(maxsize=256)
def exam_question_stem(item_id: str) -> str | None:
    match = re.fullmatch(r"(exam-bank/.+\.md)#(\d+)", item_id)
    if not match:
        return None
    path = REPO_ROOT / match.group(1)
    if not path.is_file():
        return None
    number = match.group(2)
    header = re.search(rf"(?m)^###\s+{re.escape(number)}\.\s*(.+)$", path.read_text(encoding="utf-8"))
    if not header:
        return None
    return re.sub(r"\*\*|✅", "", header.group(1)).strip()


def registry_path(data_dir: Path) -> Path:
    return data_dir / "question-registry.json"


def validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("题目登记项必须是对象")
    required = ("item_id", "topic_id", "concept_id", "question_family_id", "stem", "options")
    for key in required:
        if key == "options":
            continue
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"题目登记项缺少 {key}")
    options = entry.get("options")
    if not isinstance(options, list) or len(options) < 2 or any(
        not isinstance(option, str) or not option.strip() for option in options
    ):
        raise ValueError("题目登记项 options 至少包含两个非空字符串")
    normalized = dict(entry)
    normalized["question_fingerprint"] = content_fingerprint(entry["stem"], options)
    variant_of = normalized.get("variant_of")
    if variant_of is not None and (not isinstance(variant_of, str) or not variant_of.strip()):
        raise ValueError("variant_of 必须是非空字符串")
    return normalized


def load_registry(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"题目登记文件损坏：{path}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("题目登记文件 schema_version 不受支持")
    entries = payload.get("questions")
    if not isinstance(entries, list):
        raise ValueError("题目登记文件 questions 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    for raw in entries:
        entry = validate_entry(raw)
        item_id = entry["item_id"]
        if item_id in result:
            raise ValueError(f"题目登记 ID 重复：{item_id}")
        fingerprint = entry["question_fingerprint"]
        previous = fingerprints.get(fingerprint)
        if previous and previous != item_id:
            raise ValueError(f"题目内容重复：{item_id} 与 {previous}")
        result[item_id] = entry
        fingerprints[fingerprint] = item_id
    return result


def serialize_registry(entries: Iterable[dict[str, Any]]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "questions": sorted(entries, key=lambda item: item["item_id"]),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def resolve_metadata(
    event: dict[str, Any], private_registry: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    item_id = str(event.get("item_id") or "")
    topic_id = str(event.get("topic_id") or "")
    registered = private_registry.get(item_id, {})
    fallback = (
        default_metadata(item_id, topic_id, event.get("facet"))
        if item_id and topic_id
        else {}
    )
    concept_id = (
        event.get("concept_id")
        or registered.get("concept_id")
        or fallback.get("concept_id")
    )
    registered_stem = registered.get("stem")
    if registered_stem and len(registered_stem) > 40:
        registered_stem = registered_stem[:39] + "…"
    return {
        "item_id": item_id,
        "topic_id": topic_id,
        "concept_id": concept_id,
        "question_family_id": event.get("question_family_id")
        or registered.get("question_family_id")
        or fallback.get("question_family_id"),
        "question_fingerprint": event.get("question_fingerprint")
        or registered.get("question_fingerprint"),
        "variant_of": event.get("variant_of") or registered.get("variant_of"),
        "stem": registered.get("stem"),
        "concept_label": (
            registered_stem
            or CONCEPT_LABELS.get(concept_id)
            or fallback.get("concept_label")
            or concept_id
        ),
    }
