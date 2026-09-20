#!/usr/bin/env python3
"""Apply §N.M topic tags to transcribed 综合知识 papers.

The 2009–2017 transcripts keep the original stem/options/answer/explanation
layout but carry no knowledge tags, while the curated 2018+ papers are tagged
with the official 13-domain scheme (``§1`` … ``§13``). Tagging is done per
**question group**, mirroring the curated files' ``## 第 1-4 题：[§1 …]``
headers, because 综合知识 questions arrive in topic runs.

This script does the mechanical half only:

* ``--preview`` splits a transcript into answer blocks and prints the stem,
  the source's own "本题考查…" hint and the question range each block covers,
  so tags can be decided from evidence rather than guessed;
* ``--apply`` reads a reviewed tag map and writes the group header plus a
  ``**考点**：§…`` line after every ``【答案】`` line.

Tag decisions live in ``scripts/comprehensive_topic_tags.json`` so they can be
reviewed and corrected independently of the code.

Usage::

    python3 scripts/tag_comprehensive_questions.py --preview --year 2009下
    python3 scripts/tag_comprehensive_questions.py --apply
    python3 scripts/tag_comprehensive_questions.py --check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
YEAR_DIR = REPO_ROOT / "past-papers" / "comprehensive-by-year"
TAG_MAP_PATH = REPO_ROOT / "scripts" / "comprehensive_topic_tags.json"

ANSWER_RE = re.compile(r"^【\s*答案\s*】.*$", re.MULTILINE)
EXPLANATION_RE = re.compile(r"【\s*解析\s*】")
OPTION_ANCHOR_RE = re.compile(r"^[（(]?\s*(\d{1,3})\s*[)）]?\s*A[.．、]", re.MULTILINE)
HINT_RE = re.compile(r"本题(?:主要)?考查[^\n]{0,60}")
TAG_LINE_RE = re.compile(r"^\*\*考点\*\*：", re.MULTILINE)
TAG_NOTE = "> **知识点标签**：题组级 `§N` 标签，依据题干与原文解析中的考查提示标注，细分到考期题组而非逐题子编号"
IMAGE_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
EXAM_TOTAL_QUESTIONS = 75


@dataclass
class QuestionBlock:
    """One answer block: a stem plus every blank it covers."""

    index: int
    start: int
    end: int
    stem: str
    hint: str
    answer_line: str

    @property
    def range(self) -> list[int]:
        return [self.start, self.end]


def _stem_from(window: str) -> str:
    """Last substantive paragraph before the first option line is the stem."""
    anchors = list(OPTION_ANCHOR_RE.finditer(window))
    zone = window[: anchors[0].start()] if anchors else window
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", zone) if p.strip()]
    paragraphs = [p for p in paragraphs if not IMAGE_ONLY_RE.match(p) and len(p) > 4]
    if not paragraphs:
        return ""
    return re.sub(r"\s+", " ", paragraphs[-1])


def parse_blocks(text: str) -> list[QuestionBlock]:
    """Split a transcript into answer blocks with their question ranges."""
    answers = list(ANSWER_RE.finditer(text))
    blocks: list[QuestionBlock] = []
    for position, match in enumerate(answers):
        window_start = answers[position - 1].end() if position else 0
        window = text[window_start : match.start()]
        anchors = [int(m.group(1)) for m in OPTION_ANCHOR_RE.finditer(window)]
        if not anchors:
            continue
        hint_match = HINT_RE.search(text, match.end())
        next_answer = answers[position + 1].start() if position + 1 < len(answers) else len(text)
        explanation_window = text[match.end() : next_answer]
        hint_match = HINT_RE.search(explanation_window)
        blocks.append(
            QuestionBlock(
                index=len(blocks) + 1,
                start=min(anchors),
                end=max(anchors),
                stem=_stem_from(window),
                hint=re.sub(r"\s+", " ", hint_match.group(0)) if hint_match else "",
                answer_line=match.group(0).strip(),
            )
        )
    for position, block in enumerate(blocks):
        if position + 1 < len(blocks):
            block.end = max(block.start, blocks[position + 1].start - 1)
    return blocks


def preview(text: str, year: str, width: int = 118) -> str:
    """Human-readable listing used to decide the tags."""
    lines = [f"===== {year} ====="]
    for block in parse_blocks(text):
        span = f"{block.start}" if block.start == block.end else f"{block.start}-{block.end}"
        lines.append(f"[{block.index:>2}] 第{span}题 | {block.stem[:width]}")
        if block.hint:
            lines.append(f"      └ {block.hint[:70]}")
    return "\n".join(lines)


def load_tag_map(path: Path = TAG_MAP_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def validate_tag_map(tag_map: dict) -> list[str]:
    """Every year must cover questions 1..75 exactly once and in order."""
    problems: list[str] = []
    for year, groups in sorted(tag_map.items()):
        cursor = 1
        for position, group in enumerate(groups):
            start, end = group["range"]
            if start != cursor:
                problems.append(f"{year} 第 {position + 1} 组起始 {start}，期望 {cursor}")
            if end < start:
                problems.append(f"{year} 第 {position + 1} 组区间非法：{start}-{end}")
            if not group.get("tag", "").startswith("§"):
                problems.append(f"{year} 第 {position + 1} 组缺少 § 标签")
            cursor = end + 1
        if cursor - 1 != EXAM_TOTAL_QUESTIONS:
            problems.append(f"{year} 共覆盖到第 {cursor - 1} 题，期望 {EXAM_TOTAL_QUESTIONS}")
    return problems


def apply_tags(text: str, groups: Sequence[dict]) -> str:
    """Insert group headers and per-question 考点 lines into a transcript."""
    lines = text.splitlines()
    answer_lines = [idx for idx, line in enumerate(lines) if ANSWER_RE.match(line.strip())]

    # Keep one anchor slot per answer line.  Some source transcripts contain a
    # question whose options are only present in an image; dropping that slot
    # would shift every later ``**考点**`` line onto the preceding question.
    answer_anchors: list[tuple[int, int] | None] = []
    for position, answer_idx in enumerate(answer_lines):
        previous = answer_lines[position - 1] if position else -1
        anchor: tuple[int, int] | None = None
        for idx in range(previous + 1, answer_idx):
            match = OPTION_ANCHOR_RE.match(lines[idx].strip())
            if match:
                anchor = (idx, int(match.group(1)))
                break
        answer_anchors.append(anchor)

    block_anchors = [anchor for anchor in answer_anchors if anchor is not None]

    def paragraph_start(anchor_line: int) -> int:
        """Walk back to the first line of the paragraph above the anchor."""
        idx = anchor_line - 1
        while idx >= 0 and (not lines[idx].strip() or IMAGE_ONLY_RE.match(lines[idx].strip())):
            idx -= 1
        while idx > 0 and lines[idx - 1].strip() and not IMAGE_ONLY_RE.match(lines[idx - 1].strip()):
            idx -= 1
        return max(0, idx)

    tag_for: dict[int, str] = {}
    group_of_number: dict[int, str] = {}
    headers: list[tuple[int, str]] = []  # (line index, header text)
    for group in groups:
        start, end = group["range"]
        label = group.get("label", "").strip()
        text_tag = f"{group['tag']} {label}".strip()
        for number in range(start, end + 1):
            tag_for[number] = text_tag
            group_of_number[number] = text_tag
        anchor = next((line for line, number in block_anchors if number == start), None)
        if anchor is None:
            continue
        span = f"{start}" if start == end else f"{start}-{end}"
        headers.append((paragraph_start(anchor), f"## 第 {span} 题：[{group['tag']} {label}]"))

    insertions: dict[int, list[str]] = {}
    for line, header in headers:
        insertions.setdefault(line, []).extend([header, ""])
    for position, answer_idx in enumerate(answer_lines):
        anchor = answer_anchors[position]
        # A figure-only question has no reliable number anchor.  Do not emit a
        # guessed tag: guessing here would corrupt every subsequent mapping.
        if anchor is None:
            continue
        number = anchor[1]
        tag = tag_for.get(number, group_of_number.get(number, "§? 待人工标注"))
        insertions.setdefault(answer_idx + 1, []).extend(["", f"**考点**：{tag}"])

    # every position was computed on the original line numbers, so apply the
    # insertions from the bottom up to keep the earlier indices valid
    for index in sorted(insertions, reverse=True):
        lines[index:index] = insertions[index]

    if TAG_NOTE not in lines:
        for index, line in enumerate(lines):
            if line.startswith("> **完整性**"):
                lines.insert(index + 1, TAG_NOTE)
                break

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=YEAR_DIR)
    parser.add_argument("--tag-map", type=Path, default=TAG_MAP_PATH)
    parser.add_argument("--year", action="append", default=None)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    tag_map = load_tag_map(args.tag_map)
    years = args.year or sorted(tag_map) or [p.stem for p in sorted(args.directory.glob("*.md"))]

    if args.check:
        problems = validate_tag_map(tag_map)
        if problems:
            print("\n".join(problems))
            return 1
        print(f"标签表校验通过：{len(tag_map)} 个考期，各覆盖第 1-{EXAM_TOTAL_QUESTIONS} 题")
        return 0

    if args.preview:
        for year in years:
            path = args.directory / f"{year}.md"
            if not path.exists():
                print(f"跳过 {year}：文件不存在", file=sys.stderr)
                continue
            print(preview(path.read_text(encoding="utf-8"), year))
            print()
        return 0

    if args.apply:
        problems = validate_tag_map(tag_map)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        for year, groups in sorted(tag_map.items()):
            path = args.directory / f"{year}.md"
            text = path.read_text(encoding="utf-8")
            if TAG_LINE_RE.search(text):
                print(f"{year}: 已含标签，跳过")
                continue
            path.write_text(apply_tags(text, groups), encoding="utf-8")
            print(f"{year}: 已写入 {len(groups)} 个题组的标签")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
