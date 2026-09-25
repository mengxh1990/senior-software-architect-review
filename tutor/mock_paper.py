"""A deterministic 75-question mock with explicit curriculum coverage.

Only material that passes the same classification and quality gates as quizzes
can enter the paper. The public payload never contains answers or point names.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import tutor

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_ID = "curriculum-mock-01-v4"
BLUEPRINT_PATH = REPO_ROOT / "tutor/mock-blueprint.json"


PAPER_IDS = tuple(f"curriculum-mock-{n:02d}-v4" for n in range(1, 4))
FORMS_PATH = REPO_ROOT / "tutor/mock-forms.json"


def _selected(paper_id: str = PAPER_ID) -> list[dict[str, Any]]:
    if paper_id not in PAPER_IDS:
        raise ValueError("未知模拟卷")
    curriculum = tutor.load_curriculum()
    topics = tutor.topic_map(curriculum)
    form = json.loads(FORMS_PATH.read_text(encoding="utf-8"))["papers"][paper_id]
    pool = {item["id"]: item for item in tutor.load_quiz_question_pool(curriculum)}
    selected, used = [], set()
    for entry in form["items"]:
        raw = pool.get(entry["item_id"])
        item = tutor.quiz_question_for_topic(raw, topics[raw["topic_id"]]) if raw and raw.get("topic_id") in topics else None
        if item is None or item["question_fingerprint"] != entry["question_fingerprint"] or len(item["correct"]) != 1:
            raise ValueError(f"模拟卷题面需复核：{entry['item_id']}")
        if item["question_fingerprint"] in used:
            raise ValueError("模拟卷包含重复题面")
        used.add(item["question_fingerprint"])
        selected.append(item)
    counts = {tid: sum(q["topic_id"] == tid for q in selected) for tid in topics if tid.startswith("K")}
    blueprint = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
    if len(selected) != 75 or counts != {row["topic_id"]: row["count"] for row in blueprint["topics"]}:
        raise ValueError("模拟卷必须符合75题全模块蓝图")
    return selected


def exposed_identities(data_dir: Path, attempts: list[dict[str, Any]], state: dict[str, Any] | None = None) -> set[str]:
    """Both answered and already served quiz content disqualify blind measurement."""
    seen = {value for event in attempts for value in
            (event.get("item_id"), tutor.enrich_record_event(event).get("question_fingerprint")) if value}
    for path in sorted(tutor.quiz_sessions_dir(data_dir).glob("*.json")):
        manifest = tutor.load_json(path, "客观题会话")
        served = list(manifest.get("questions", []))
        served += [row["variant_question"] for row in (manifest.get("result") or {}).get("results", [])
                   if isinstance(row.get("variant_question"), dict)]
        for item in served:
            seen.update(value for value in (item.get("item_id"), item.get("question_fingerprint")) if value)
    for topic in (state or {}).get("topics", {}).values():
        for record in topic.get("mastery", {}).values():
            seen.update(record.get("attempted_items", []))
    # Resolve aliases to current content identities as well as the logged version.
    fingerprints = tutor.public_question_fingerprints()
    seen.update(fingerprints[item_id] for item_id in list(seen) if item_id in fingerprints)
    return seen


def availability(data_dir: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    attempts = tutor.load_attempts(tutor.state_paths(data_dir)["attempts"])
    seen = exposed_identities(data_dir, attempts, state)
    measured = {event.get("item_id") for event in attempts if event.get("event_type") == "mock"}
    pool = tutor.load_quiz_question_pool(tutor.load_curriculum())
    blocked = tutor.quarantined_item_ids(data_dir, pool)
    blocked_fingerprints = {q.get("question_fingerprint") for q in pool if q["id"] in blocked}
    rows = []
    for paper_id in PAPER_IDS:
        reasons = []
        exposed = 0
        try:
            items = private_items(paper_id)
        except (KeyError, ValueError) as error:
            reasons.append("quality_unavailable")
            detail = str(error)
        else:
            detail = None
            exposed = sum(q["item_id"] in seen or q["question_fingerprint"] in seen for q in items)
            if exposed:
                reasons.append("previously_exposed_items")
            if any(q["item_id"] in blocked or q["question_fingerprint"] in blocked_fingerprints for q in items):
                reasons.append("quarantined_items")
        if paper_id in measured:
            reasons.append("repeated_paper")
        rows.append({"paper_id": paper_id, "available": not reasons, "prior_exposure_count": exposed,
                     "reasons": reasons, "detail": detail})
    chosen = next((row["paper_id"] for row in rows if row["available"]), None)
    return {"available": chosen is not None, "paper_id": chosen, "papers": rows,
            "reason": None if chosen else "没有未练过且通过门禁的独立模拟卷；可继续模块训练或显式进行同卷复习"}


def private_items(paper_id: str = PAPER_ID) -> list[dict[str, Any]]:
    result = []
    for number, item in enumerate(_selected(paper_id), 1):
        result.append(
            {
                "number": number,
                "item_id": item["item_id"],
                "topic_id": item["topic_id"],
                "question_fingerprint": item["question_fingerprint"],
                "source_type": item["source_type"],
                "source": item["source"],
                "question": {
                    "id": item["item_id"],
                    "topic_name": item["topic_name"],
                    "stem": item["stem"],
                    "options": [
                        {"key": o["label"], "text": o["text"]} for o in item["options"]
                    ],
                    "answer": item["correct"][0],
                    "explanation": item.get("explanation", ""),
                    "context_id": item.get("context_id"),
                    "context": item.get("context"),
                },
            }
        )
    return result


def public_payload(paper_id: str = PAPER_ID) -> dict[str, Any]:
    source = _selected(paper_id)
    items = [
        {
            "number": i["number"],
            "id": i["question"]["id"],
            "topic_name": i["question"]["topic_name"],
            "stem": i["question"]["stem"],
            "options": copy.deepcopy(i["question"]["options"]),
        }
        for i in private_items(paper_id)
    ]
    contexts = {}
    for number, item in enumerate(source, 1):
        if item.get("context"):
            key = item.get("context_id") or str(number)
            contexts.setdefault(key, []).append(number)
    passages = []
    for key, numbers in contexts.items():
        item = source[numbers[0] - 1]
        # A context is attached to its precise questions; unrelated intervening questions never inherit it.
        passages.append(
            {
                "start": min(numbers),
                "end": max(numbers),
                "question_numbers": numbers,
                "title": item.get("context_title") or "题目材料",
                "text": item["context"],
            }
        )
    passages.sort(key=lambda row: (row["start"] != 71, row["start"]))
    return {
        "paper_id": paper_id,
        "title": json.loads(FORMS_PATH.read_text(encoding="utf-8"))["papers"][paper_id]["title"],
        "source_type": "simulation",
        "question_count": 75,
        "duration_seconds": 9000,
        "pass_line": 45,
        "items": items,
        "passages": passages,
        "coverage": {
            "modules": len({q["topic_id"] for q in source}),
            "total_modules": len(
                [
                    t
                    for t in tutor.load_curriculum()["topics"]
                    if t["id"].startswith("K")
                ]
            ),
        },
        "measurement_note": "本卷覆盖全部学习模块，模块内为抽样；一次成绩不能证明模块全部内容掌握，也不是通过概率。",
    }
