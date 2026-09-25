#!/usr/bin/env python3
"""Count reviewed questions without extrapolating incomplete papers to 75 items."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import knowledge_taxonomy
import tutor

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "tutor/frequency-snapshot.json"
STABLE_WEIGHT = 0.8
RECENT_WEIGHT = 0.2
MIN_PAPER_COVERAGE = 0.8
MIN_PAPERS = 3


def build_snapshot(as_of: str) -> dict:
    curriculum = tutor.load_curriculum()
    pool = tutor.load_quiz_question_pool(curriculum)
    groups = defaultdict(list)
    for item in pool:
        if item.get("source_type") in {"real", "recalled_real"} and item.get("year"):
            groups[(item["source_type"], item["year"])].append(item)
    papers = []
    totals = defaultdict(Counter)
    cohort_counts = Counter()
    for (source, year), items in sorted(groups.items()):
        ready = [
            item
            for item in items
            if item.get("quality_status") == "ready"
            and item.get("teaching_status") == "ready"
        ]
        mapped = [
            item
            for item in ready
            if item.get("classification_status") == "reviewed"
            and len(item.get("candidate_topics", [])) == 1
        ]
        cohort = "stable" if source == "real" else "recent"
        complete_enough = len(ready) / 75 >= MIN_PAPER_COVERAGE
        papers.append(
            {
                "year": year,
                "source_type": source,
                "cohort": cohort,
                "ready_questions": len(ready),
                "mapped_questions": len(mapped),
                "unmapped_questions": len(ready) - len(mapped),
                "coverage": round(len(mapped) / max(1, len(ready)), 4),
                "expected_questions": 75,
                "paper_coverage": round(len(ready) / 75, 4),
                "frequency_eligible": complete_enough,
                "exclusion_reason": None
                if complete_enough
                else "insufficient_paper_coverage",
            }
        )
        if not complete_enough:
            continue
        cohort_counts[cohort] += 1
        # Missing questions retain unknown mass. A 14-question extract never becomes a full paper.
        totals[cohort].update(item["candidate_topics"][0] for item in mapped)
    coverage = {}
    for cohort in ("stable", "recent"):
        rows = [paper for paper in papers if paper["cohort"] == cohort]
        ready = sum(row["ready_questions"] for row in rows)
        mapped = sum(row["mapped_questions"] for row in rows)
        coverage[cohort] = {
            "papers": len(rows),
            "eligible_papers": cohort_counts[cohort],
            "ready_questions": ready,
            "mapped_questions": mapped,
            "unmapped_questions": ready - mapped,
            "ratio": round(mapped / max(1, ready), 4),
        }

    def rates(ids, counts, key):
        result = []
        for ident in sorted(ids):
            stable = counts["stable"][ident] / max(1, cohort_counts["stable"])
            recent = counts["recent"][ident] / max(1, cohort_counts["recent"])
            result.append(
                {
                    key: ident,
                    "stable_questions_per_paper": round(stable, 4),
                    "recent_questions_per_paper": round(recent, 4),
                    "weighted_questions_per_paper": round(
                        STABLE_WEIGHT * stable + RECENT_WEIGHT * recent, 4
                    ),
                }
            )
        return result

    ready = all(
        coverage[c]["ratio"] >= 0.99 and cohort_counts[c] >= MIN_PAPERS
        for c in ("stable", "recent")
    )
    return {
        "schema_version": 1,
        "policy_version": 3,
        "taxonomy_version": knowledge_taxonomy.TAXONOMY_VERSION,
        "as_of": as_of,
        "mode": "runtime_ready" if ready else "report_only",
        "weights": {"stable": STABLE_WEIGHT, "recent": RECENT_WEIGHT},
        "activation_thresholds": {
            "stable_coverage": 0.99,
            "recent_coverage": 0.99,
            "minimum_paper_coverage": MIN_PAPER_COVERAGE,
            "minimum_eligible_papers_per_cohort": MIN_PAPERS,
        },
        "coverage": coverage,
        "papers": papers,
        "topics": rates(
            (t["id"] for t in curriculum["topics"] if t["id"].startswith("K")),
            totals,
            "topic_id",
        ),
        "source_digest": knowledge_taxonomy.source_digest(),
    }


def rendered(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def display_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--as-of")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    existing = json.loads(args.output.read_text()) if args.output.exists() else {}
    content = rendered(
        build_snapshot(args.as_of or existing.get("as_of") or date.today().isoformat())
    )
    if args.check:
        if not args.output.exists() or args.output.read_text() != content:
            print(
                f"error: {display_path(args.output)} is out of date; run scripts/build_frequency_snapshot.py --write"
            )
            return 1
        return 0
    if args.write:
        args.output.write_text(content, encoding="utf-8")
        print(f"wrote {display_path(args.output)}")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
