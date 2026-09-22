#!/usr/bin/env python3
"""Build an auditable, report-only frequency snapshot from the quiz pool.

Only quality-gated real or recalled-real questions that map uniquely to one
stable comprehensive topic are counted.  Each paper is normalised to a
75-question equivalent before the stable (80%) and recent-trend (20%) layers
are combined.  The runtime keeps using the curated curriculum values until
both mapping-coverage gates are met.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tutor  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "tutor" / "frequency-snapshot.json"
STABLE_WEIGHT = 0.8
RECENT_WEIGHT = 0.2
STABLE_COVERAGE_GATE = 0.9
RECENT_COVERAGE_GATE = 0.8


def _eligible_topics(curriculum: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        topic
        for topic in curriculum["topics"]
        if topic["id"].startswith("K")
        and "comprehensive" in topic.get("subjects", [])
        and "recognition" in topic.get("skills", [])
    ]


def _paper_rows(
    pool: list[dict[str, Any]], topics: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    papers: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"ready": 0, "mapped": 0, "topic_counts": Counter()}
    )
    for raw in pool:
        source_type = raw.get("source_type")
        year = raw.get("year")
        if (
            raw.get("quality_status") != "ready"
            or raw.get("teaching_status") != "ready"
            or source_type not in {
            "real",
            "recalled_real",
            }
        ):
            continue
        if not isinstance(year, str) or not year:
            continue
        paper = papers[(source_type, year)]
        paper["ready"] += 1
        matches = []
        for topic in topics:
            candidate = tutor.quiz_question_for_topic(raw, topic, {})
            if candidate is not None:
                matches.append(topic["id"])
        if len(matches) != 1:
            continue
        paper["mapped"] += 1
        paper["topic_counts"][matches[0]] += 1

    rows: list[dict[str, Any]] = []
    normalised: dict[str, dict[str, float]] = defaultdict(dict)
    for (source_type, year), paper in sorted(papers.items()):
        ready = int(paper["ready"])
        mapped = int(paper["mapped"])
        cohort = "stable" if source_type == "real" else "recent"
        rows.append(
            {
                "year": year,
                "source_type": source_type,
                "cohort": cohort,
                "ready_questions": ready,
                "mapped_questions": mapped,
                "unmapped_questions": ready - mapped,
                "coverage": round(mapped / ready, 4) if ready else 0.0,
            }
        )
        if ready:
            scale = 75.0 / ready
            for topic_id, count in paper["topic_counts"].items():
                normalised[cohort][topic_id] = (
                    normalised[cohort].get(topic_id, 0.0) + count * scale
                )
    return rows, normalised


def build_snapshot(as_of: str) -> dict[str, Any]:
    curriculum = tutor.load_curriculum()
    topics = _eligible_topics(curriculum)
    pool = tutor.load_quiz_question_pool(curriculum)
    papers, totals = _paper_rows(pool, topics)
    cohort_papers = Counter(row["cohort"] for row in papers)

    coverage: dict[str, dict[str, Any]] = {}
    for cohort in ("stable", "recent"):
        selected = [row for row in papers if row["cohort"] == cohort]
        ready = sum(row["ready_questions"] for row in selected)
        mapped = sum(row["mapped_questions"] for row in selected)
        coverage[cohort] = {
            "papers": len(selected),
            "ready_questions": ready,
            "mapped_questions": mapped,
            "unmapped_questions": ready - mapped,
            "ratio": round(mapped / ready, 4) if ready else 0.0,
        }

    topic_rows = []
    for topic in topics:
        topic_id = topic["id"]
        stable_rate = (
            totals["stable"].get(topic_id, 0.0)
            / max(cohort_papers["stable"], 1)
        )
        recent_rate = (
            totals["recent"].get(topic_id, 0.0)
            / max(cohort_papers["recent"], 1)
        )
        topic_rows.append(
            {
                "topic_id": topic_id,
                "stable_questions_per_paper": round(stable_rate, 4),
                "recent_questions_per_paper": round(recent_rate, 4),
                "weighted_questions_per_paper": round(
                    STABLE_WEIGHT * stable_rate + RECENT_WEIGHT * recent_rate,
                    4,
                ),
            }
        )
    topic_rows.sort(key=lambda row: row["topic_id"])

    digest_input = json.dumps(
        {"papers": papers, "topics": topic_rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    runtime_ready = (
        coverage["stable"]["ratio"] >= STABLE_COVERAGE_GATE
        and coverage["recent"]["ratio"] >= RECENT_COVERAGE_GATE
    )
    return {
        "schema_version": 1,
        "as_of": as_of,
        "mode": "runtime_ready" if runtime_ready else "report_only",
        "weights": {"stable": STABLE_WEIGHT, "recent": RECENT_WEIGHT},
        "activation_thresholds": {
            "stable_coverage": STABLE_COVERAGE_GATE,
            "recent_coverage": RECENT_COVERAGE_GATE,
        },
        "coverage": coverage,
        "papers": papers,
        "topics": topic_rows,
        "source_digest": hashlib.sha256(digest_input).hexdigest(),
    }


def rendered(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def display_path(path: Path) -> str:
    """Render repository paths compactly without rejecting an external output."""

    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    existing: dict[str, Any] = {}
    if args.output.exists():
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    as_of = args.as_of or existing.get("as_of") or date.today().isoformat()
    content = rendered(build_snapshot(as_of))
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != content:
            print(
                f"error: {args.output.relative_to(REPO_ROOT)} is out of date; "
                "run scripts/build_frequency_snapshot.py --write",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.write:
        args.output.write_text(content, encoding="utf-8")
        print(f"wrote {display_path(args.output)}")
        return 0
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
