"""Canonical high-frequency comprehensive mock paper for the local tutor UI.

The public browser payload deliberately excludes answers and explanations.
Answers remain on the local server until the learner submits the paper.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import question_registry
import sanitize_bank

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGLISH_SOURCE_PATH = REPO_ROOT / "exam-bank" / "23-english-reading.md"
PAPER_ID = "hf-pass-mock-01-v2"
PAPER_TITLE = "综合知识高频过线模拟卷 01"

# item_id and stable topic_id for each question.
_ITEMS: tuple[tuple[str, str], ...] = (
    *((f"07-software-engineering-{i:02d}", "K08.SOFTWARE_PROCESS_MODELS") for i in range(1, 6)),
    ("07-software-engineering-06", "K05.TEST_CMMI_PATTERNS"),
    *((f"07-software-engineering-{i:02d}", "K16.REQUIREMENTS_MANAGEMENT") for i in range(7, 10)),
    ("07-software-engineering-10", "K05.TEST_CMMI_PATTERNS"),
    *((f"05-uml-{i:02d}", "K03.SOFTWARE_DESIGN_UML") for i in range(1, 6)),
    *((f"10-architecture-styles-{i:02d}", "K04.ARCH_STYLES_ABSD") for i in range(1, 9)),
    ("14-absd-views-01", "K04.ARCH_STYLES_ABSD"),
    ("14-absd-views-02", "K04.ARCH_STYLES_ABSD"),
    *((f"14-absd-views-{i:02d}", "K11.COMPONENTS_4PLUS1") for i in range(3, 5)),
    *((f"15-microservice-cloud-native-{i:02d}", "K12.PATTERNS_SOA_MICROSERVICES") for i in range(1, 3)),
    *((f"02-os-concepts-{i:02d}", "K01.OS_MEMORY_KERNEL") for i in range(1, 5)),
    *((f"01-computer-systems-{i:02d}", "K18.COMPUTER_ARCH_STORAGE") for i in range(1, 3)),
    *((f"04-networking-{i:02d}", "K02.NETWORK_PROTOCOLS") for i in range(1, 4)),
    *((f"22-embedded-{i:02d}", "K07.REALTIME_EMBEDDED") for i in range(1, 3)),
    *((f"11-quality-attributes-{i:02d}", "K09.QUALITY_SCENARIOS") for i in range(1, 5)),
    *((f"12-atam-evaluation-{i:02d}", "K19.ATAM_TACTICS") for i in range(1, 4)),
    *((f"03-database-{i:02d}", "K10.DATABASE_MODELING") for i in range(1, 8)),
    *((f"06-ip-and-standards-{i:02d}", "K17.IP_COPYRIGHT") for i in range(1, 5)),
    *((f"21-security-{i:02d}", "K20.SECURITY_FOUNDATIONS") for i in range(1, 5)),
    *((f"16-middleware-{i:02d}", "K21.MESSAGING_CACHE") for i in range(1, 4)),
    *((f"18-cache-{i:02d}", "K21.MESSAGING_CACHE") for i in range(1, 3)),
    *((f"13-design-patterns-{i:02d}", "K12.PATTERNS_SOA_MICROSERVICES") for i in range(1, 4)),
    *((f"23-english-reading-{i:02d}", "K22.ENGLISH_READING") for i in range(1, 6)),
)

assert len(_ITEMS) == 75


@lru_cache(maxsize=1)
def _questions() -> dict[str, dict[str, Any]]:
    """Parse only the source Markdown files used by this paper.

    The answer-bearing bank stays in ``exam-bank/`` rather than being copied
    into the browser's static directory or requiring a generated JSON file.
    """
    topic_files = sorted({item_id.rsplit("-", 1)[0] for item_id, _ in _ITEMS})
    questions: dict[str, dict[str, Any]] = {}
    for topic_file in topic_files:
        source_path = REPO_ROOT / "exam-bank" / f"{topic_file}.md"
        first_heading = next((line[2:] for line in source_path.read_text(encoding="utf-8").splitlines()
                              if line.startswith("# ")), topic_file)
        topic_name = re.sub(r"\s*·.*$", "", first_heading).strip()
        for item in sanitize_bank.parse_exam_bank(source_path):
            if len(item.get("correct", [])) != 1:
                continue
            number = int(item["id"].rsplit("#", 1)[1])
            question_id = f"{topic_file}-{number:02d}"
            questions[question_id] = {
                "id": question_id, "topic_file": topic_file, "topic_name": topic_name,
                "stem": item["stem"], "options": [{"key": option["label"], "text": option["text"]}
                                                for option in item["options"]],
                "answer": item["correct"][0], "explanation": item.get("explanation", ""),
            }
    return questions


def private_items() -> list[dict[str, Any]]:
    questions = _questions()
    missing = [item_id for item_id, _ in _ITEMS if item_id not in questions]
    if missing:
        raise ValueError("模拟卷题目未在题库中找到：" + ", ".join(missing))
    items = []
    for number, (question_id, topic_id) in enumerate(_ITEMS, 1):
        question = questions[question_id]
        source_item_id = (
            f"exam-bank/{question['topic_file']}.md#"
            f"{int(question['id'].rsplit('-', 1)[1])}"
        )
        items.append(
            {
                "number": number,
                "question": question,
                "topic_id": question_registry.topic_override(source_item_id) or topic_id,
                "item_id": source_item_id,
                "question_fingerprint": question_registry.content_fingerprint(
                    question["stem"], [option["text"] for option in question["options"]]
                ),
            }
        )
    return items


def public_payload() -> dict[str, Any]:
    items = []
    for item in private_items():
        question = item["question"]
        items.append(
            {
                "number": item["number"],
                "id": question["id"],
                "topic_name": question["topic_name"],
                "stem": question["stem"],
                "options": question["options"],
            }
        )
    return {
        "paper_id": PAPER_ID,
        "title": PAPER_TITLE,
        "source_type": "simulation",
        "question_count": len(items),
        "duration_seconds": 150 * 60,
        "pass_line": 45,
        "passages": [{"start": 71, "end": 75, "title": "英语阅读", "text": _english_passage()}],
        "items": items,
    }


def _english_passage() -> str:
    raw = ENGLISH_SOURCE_PATH.read_text(encoding="utf-8")
    section = raw.split("## Passage 1 — Cloud Computing & Service Models", 1)[1]
    section = section.split("### 1. (1)", 1)[0]
    section = section.replace("\n---", "").strip()
    return section.replace("**", "")
