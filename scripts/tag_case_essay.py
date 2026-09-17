#!/usr/bin/env python3
"""Tag 案例分析 and 论文 papers with the repo's题型/主题 numbering.

``past-papers/case-by-year/`` and ``past-papers/essay-by-year/`` hold the raw
papers; ``past-papers/case-types/`` and ``past-papers/paper-topics/`` hold the
numbered playbooks (案例题型 01–13 / 论文主题 01–13). This script links the two
by writing a tag line under every ``## 试题N`` heading:

    ## 试题一
    > **题型**：案例 01 · 架构评估（ATAM）

    ## 试题一：论软件维护及其应用
    > **主题**：论文 09 · 软件架构演化与维护

Tag decisions live in ``scripts/case_essay_tags.json`` so they can be reviewed
and corrected independently of the code.

Usage::

    python3 scripts/tag_case_essay.py --preview          # 打印待标注题目供复核
    python3 scripts/tag_case_essay.py --check            # 校验覆盖完整性
    python3 scripts/tag_case_essay.py --apply            # 落盘标签
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPERS = {
    "case": REPO_ROOT / "past-papers" / "case-by-year",
    "essay": REPO_ROOT / "past-papers" / "essay-by-year",
}
TAG_MAP_PATH = REPO_ROOT / "scripts" / "case_essay_tags.json"
# [ \t] instead of \s: a permissive \s* would let the pattern swallow the
# following blank line and pick up "【说明】" as the question title.
# 版式在各考期之间并不统一，这里一并兼容：
#   "## 试题一" / "### 2.1. 试题一：质量属性" / "# 试题1" / 裸行 "试题二 论软件设计模式。"
HEADING_RE = re.compile(
    r"^(#{1,4})?[ \t]*(?:\d+(?:\.\d+)*[.、]?[ \t]*)?"
    r"试题[ \t]*([一二三四五六七八九十]|\d{1,2})[ \t]*[:：]?[ \t]*(.*)$",
    re.MULTILINE,
)
TITLE_STOP_CHARS = set("是由中的和与为在可需应必要将会不有从对及或均都指属包含以其若如则即并又且")
# 只把真正的句读符号当作"这是散文不是标题"的信号：标题里出现冒号、
# 括号、破折号都很常见（"试题一（必答题）：软件架构设计与评估"）。
SENTENCE_PUNCTUATION = "。？！，、；…"
ARABIC = {str(i): numeral for i, numeral in enumerate("一二三四五六七八九十", start=1)}
TAG_LINE_RE = re.compile(r"^>\s*\*\*(?:题型|主题)\*\*[:：]", re.MULTILINE)
IMAGE_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
HEADING_LABEL = {"case": "题型", "essay": "主题"}
INSTRUCTION_PREFIXES = ("阅读以下", "阅读下列", "请阅读", "## 【说明】", "【说明】")


def iter_papers(kind: str) -> Iterable[Path]:
    return sorted(path for path in PAPERS[kind].glob("*.md") if path.name != "README.md")


def parse_heading(line: str) -> tuple[str, str] | None:
    """Return ``(numeral, title)`` when the line really is a question heading.

    Prose such as ``试题一是必答题，每题15分。`` must not become a heading, so a
    title carrying sentence punctuation (other than a trailing full stop) or
    starting with a connective is rejected.
    """
    match = HEADING_RE.match(line.strip())
    if not match:
        return None
    numeral, title = match.group(2), match.group(3).strip()
    numeral = ARABIC.get(numeral, numeral)
    title = title.rstrip("。")
    if title:
        if any(char in title for char in SENTENCE_PUNCTUATION):
            return None
        if title[0] in TITLE_STOP_CHARS:
            return None
    return numeral, title


def split_questions(text: str) -> list[tuple[str, str, str]]:
    """Return ``(numeral, title, body)`` for every question heading."""
    matches: list[tuple[re.Match[str], tuple[str, str]]] = []
    for match in HEADING_RE.finditer(text):
        parsed = parse_heading(match.group(0))
        if parsed:
            matches.append((match, parsed))
    out: list[tuple[str, str, str]] = []
    for position, (match, (numeral, title)) in enumerate(matches):
        end = matches[position + 1][0].start() if position + 1 < len(matches) else len(text)
        out.append((numeral, title, text[match.end() : end]))
    return out


def first_meaningful_line(body: str, limit: int = 88) -> str:
    """First substantive paragraph, skipping the generic '阅读以下…' instruction."""
    fallback = ""
    for block in re.split(r"\n\s*\n", body):
        candidate = block.strip()
        if not candidate or IMAGE_ONLY_RE.match(candidate):
            continue
        candidate = re.sub(r"^#+\s*", "", candidate)
        candidate = re.sub(r"^【[^】]*】\s*", "", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if len(candidate) <= 6:
            continue
        if candidate.startswith(INSTRUCTION_PREFIXES):
            fallback = fallback or candidate[:limit]
            continue
        return candidate[:limit]
    return fallback


def preview(kind: str) -> str:
    lines = [f"===== {kind} ====="]
    for path in iter_papers(kind):
        for numeral, title, body in split_questions(path.read_text(encoding="utf-8")):
            headline = title or first_meaningful_line(body)
            lines.append(f"[{path.stem:<8}] 试题{numeral} | {headline}")
    return "\n".join(lines)


def load_tag_map(path: Path = TAG_MAP_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def validate_tag_map(tag_map: dict) -> list[str]:
    """Every paper must have one tag per 试题, matching the file's heading count."""
    problems: list[str] = []
    for kind in ("case", "essay"):
        entries = tag_map.get(kind) or {}
        files = {path.stem for path in iter_papers(kind)}
        missing_files = sorted(files - set(entries))
        if missing_files:
            problems.append(f"{kind} 缺少标签：{', '.join(missing_files)}")
        for year, questions in sorted(entries.items()):
            path = PAPERS[kind] / f"{year}.md"
            if not path.exists():
                problems.append(f"{kind}/{year}: 文件不存在")
                continue
            expected = split_questions(path.read_text(encoding="utf-8"))
            if len(questions) != len(expected):
                problems.append(
                    f"{kind}/{year}: 标签 {len(questions)} 条，实际试题 {len(expected)} 道"
                )
            for index, question in enumerate(questions):
                if not str(question.get("tag", "")).startswith(("案例", "论文")):
                    problems.append(f"{kind}/{year} 第 {index + 1} 条标签格式错误")
    return problems


def apply_tags(kind: str, text: str, questions: Sequence[dict]) -> str:
    """Insert the tag line under each question heading.

    Bare ``试题二 论…`` lines (2016 论文卷) are promoted to ``## 试题二：论…``
    first, so every question carries a heading plus its tag.
    """
    lines = text.splitlines()
    headings = [i for i, line in enumerate(lines) if parse_heading(line)]
    if len(headings) != len(questions):
        raise ValueError(f"{kind}: 标题 {len(headings)} 个，标签 {len(questions)} 条，无法一一对应")
    for index in range(len(headings) - 1, -1, -1):
        question = questions[index]
        label = question.get("label", "").strip()
        tag = question["tag"]
        rendered = f"> **{HEADING_LABEL[kind]}**：{tag}" + (f" · {label}" if label else "")
        parsed = parse_heading(lines[headings[index]])
        assert parsed is not None
        numeral, title = parsed
        if not lines[headings[index]].lstrip().startswith("#"):
            lines[headings[index]] = f"## 试题{numeral}" + (f"：{title}" if title else "")
        lines.insert(headings[index] + 1, rendered)
        lines.insert(headings[index] + 2, "")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tag-map", type=Path, default=TAG_MAP_PATH)
    args = parser.parse_args(argv)

    tag_map = load_tag_map(args.tag_map)

    if args.preview:
        for kind in ("case", "essay"):
            print(preview(kind))
            print()
        return 0

    if args.check:
        problems = validate_tag_map(tag_map)
        if problems:
            print("\n".join(problems))
            return 1
        counts = {kind: sum(len(v) for v in (tag_map.get(kind) or {}).values()) for kind in ("case", "essay")}
        print(f"标签表校验通过：案例 {counts['case']} 道、论文 {counts['essay']} 道，覆盖全部考期")
        return 0

    if args.apply:
        problems = validate_tag_map(tag_map)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        for kind in ("case", "essay"):
            for year, questions in sorted((tag_map.get(kind) or {}).items()):
                path = PAPERS[kind] / f"{year}.md"
                text = path.read_text(encoding="utf-8")
                # 有的卷首说明也写着“题型：5 道大题”，只按数量判断是否已标注
                if len(TAG_LINE_RE.findall(text)) >= len(questions):
                    print(f"{kind}/{year}: 已含标签，跳过")
                    continue
                path.write_text(apply_tags(kind, text, questions), encoding="utf-8")
                print(f"{kind}/{year}: 写入 {len(questions)} 条标签")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
