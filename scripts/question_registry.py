"""Stable item identities, topic corrections, and private question deduplication."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1

# A source item may have a broad or incorrect heading. Only these verified
# exceptions override its public topic mapping.
ITEM_TOPIC_OVERRIDES = {
    "exam-bank/02-os-concepts.md#3": "K01.OS_MEMORY_KERNEL",
    "exam-bank/09-software-metrics.md#6": "K03.SOFTWARE_DESIGN_UML",
    "exam-bank/09-software-metrics.md#7": "K03.SOFTWARE_DESIGN_UML",
    "exam-bank/24-devops-serverless.md#13": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#14": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#15": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#16": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#17": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#18": "K27.EMERGING_TECH",
    "exam-bank/24-devops-serverless.md#19": "K23.PROJECT_MANAGEMENT_METRICS",
    "exam-bank/25-enterprise-integration.md#4": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#5": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#6": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#7": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#8": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#9": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#10": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#11": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#12": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/25-enterprise-integration.md#13": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#14": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#15": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#16": "K21.MESSAGING_CACHE",
    "exam-bank/25-enterprise-integration.md#17": "K26.ARCH_EVOLUTION",
    "exam-bank/26-soa-evolution.md#9": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/26-soa-evolution.md#10": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/26-soa-evolution.md#11": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/26-soa-evolution.md#12": "K12.PATTERNS_SOA_MICROSERVICES",
    "past-papers/comprehensive-by-year/2012下.md#17-17": "K18.COMPUTER_ARCH_STORAGE",
    "past-papers/comprehensive-by-year/2022.md#32": "K12.PATTERNS_SOA_MICROSERVICES",
    "past-papers/comprehensive-by-year/2024下.md#38": "K03.SOFTWARE_DESIGN_UML",
    "past-papers/comprehensive-by-year/2025上.md#23": "K01.OS_MEMORY_KERNEL",
}

# Correct historical events in memory without changing the original answer log.
PUBLIC_ITEM_CORRECTIONS = {
    "exam-bank/02-os-concepts.md#3": "K01.OS_MEMORY_KERNEL",
    "exam-bank/26-soa-evolution.md#10": "K12.PATTERNS_SOA_MICROSERVICES",
    "exam-bank/26-soa-evolution.md#12": "K12.PATTERNS_SOA_MICROSERVICES",
    "past-papers/comprehensive-by-year/2022.md#26": "K03.SOFTWARE_DESIGN_UML",
    "past-papers/comprehensive-by-year/2018下.md#23": "K06.DESIGN_DATA_VIEWS",
    "past-papers/comprehensive-by-year/2009下.md#21-21": "K06.DESIGN_DATA_VIEWS",
}


def topic_override(item_id: str) -> str | None:
    """Return the verified topic for a mislabeled public item, if any."""

    return PUBLIC_ITEM_CORRECTIONS.get(item_id) or ITEM_TOPIC_OVERRIDES.get(item_id)


def canonicalize_public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Project verified topic corrections without rewriting answer evidence."""

    topic_id = topic_override(str(event.get("item_id") or ""))
    if (
        not topic_id
        or event.get("topic_id") == topic_id
        or event.get("subject") not in (None, "comprehensive")
        or event.get("skill") not in (None, "recognition")
    ):
        return event
    return {**event, "topic_id": topic_id}


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


def registry_path(data_dir: Path) -> Path:
    return data_dir / "question-registry.json"


def validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("题目登记项必须是对象")
    for key in ("item_id", "topic_id", "stem"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"题目登记项缺少 {key}")
    options = entry.get("options")
    if not isinstance(options, list) or len(options) < 2 or any(
        not isinstance(option, str) or not option.strip() for option in options
    ):
        raise ValueError("题目登记项 options 至少包含两个非空字符串")
    normalized = {
        key: entry[key]
        for key in ("item_id", "topic_id", "stem", "options", "variant_of", "memory_hook")
        if key in entry
    }
    normalized["question_fingerprint"] = content_fingerprint(entry["stem"], options)
    for key in ("variant_of", "memory_hook"):
        value = normalized.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} 必须是非空字符串")
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
