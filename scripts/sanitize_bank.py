#!/usr/bin/env python3
"""
Sanitize exam-bank blocks for coach-driven quiz sessions.

Reads an exam-bank markdown file, extracts the question blocks whose header
starts with ``### N.`` for the given N values, strips inline answer markers
(``✅``, bold correct-option markers, ``**答案**:`` / ``**答案**：`` lines, and
the trailing ``**解析**:`` / ``**解析**：`` section), and prints a JSON list
to stdout. Each item has::

    {
        "id": "exam-bank/<file>.md#<N>",
        "stem": "<question stem, single line>",
        "options": [{"label": "A", "text": "..."}, ...],
        "correct": ["B"],   # coach-only, DO NOT echo to the learner
        "explanation": "..." # coach-only, only for post-answer feedback
    }

Usage::

    python3 scripts/sanitize_bank.py <path-to-exam-bank.md> <N> [<N> ...]

Example::

    python3 scripts/sanitize_bank.py exam-bank/07-software-engineering.md 1 4 6

历年真题（``past-papers/comprehensive-by-year/*.md``）走同一套脱敏契约，但按
考点/年份抽题，并给出该题对应的 tutor 考点建议：

    python3 scripts/sanitize_bank.py past-papers/comprehensive-by-year/2013下.md --tag §5 --limit 3
    python3 scripts/sanitize_bank.py --topic K10.DATABASE_MODELING --year 2013下 --limit 3
    python3 scripts/sanitize_bank.py --tag §6 --list

真题返回的每项额外带 ``tag``（§N 考点）、``range``（题号区间）、
``candidate_topics``（由 curriculum.json 的 raw_tags 推出的 tutor 考点编号）。

Supported option-line formats (learners will never see the raw form):

  * ``A. text`` / ``B. text`` / ``C. text`` / ``D. text``  (bare)
  * ``- A. text``                                           (bulleted)
  * ``✅ **D. text**``                                       (correct marker)
  * ``- **B. text**``                                       (correct via bold)
  * option text may contain inline ``**bold**`` for emphasis
  * an explicit ``**答案**: X`` / ``**答案**：X`` line, when present, wins over
    ``✅`` heuristics.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
CURRICULUM_PATH = REPO_ROOT / "tutor" / "curriculum.json"
PAPER_DIR = REPO_ROOT / "past-papers" / "comprehensive-by-year"

# ---------------------------------------------------------------- past papers
# 2009–2017 转录版：题干段落 + (N) A. … 选项 + 【答案】X + **考点**：§… + 【解析】…
# 2018 起整理版：### N. 【题干】 + 选项 + **答案：X** ｜ **考点**：§… + **解析**：
PAPER_ANSWER_RE = re.compile(r"^【\s*答案\s*】\s*(.*)$", re.MULTILINE)
PAPER_EXPLAIN_RE = re.compile(r"【\s*解析\s*】")
PAPER_TAG_RE = re.compile(r"^\*\*考点\*\*[:：]\s*(§[0-9]+(?:\.[0-9]+)?)\s*(.*)$", re.MULTILINE)
PAPER_OPTION_ANCHOR_RE = re.compile(r"^[（(]?\s*(\d{1,3})\s*[)）]?\s*A[.．、]", re.MULTILINE)
PAPER_OPTION_SPLIT_RE = re.compile(r"(?:^|\s{2,})(?=[A-D][.．、]\s*\S)")
CURATED_HEADER_RE = re.compile(r"^###\s*(\d+)[.、]\s*", re.MULTILINE)
CURATED_ANSWER_RE = re.compile(
    r"^\*\*\s*答案\s*[:：]\s*([A-Z](?:\s*[、,，]?\s*[A-Z])*)\s*\*\*"
    r"(?:\s*\|\s*\*\*\s*考点\s*\*\*\s*[:：]\s*(§[0-9]+(?:\.[0-9]+)?)\s*(.*))?\s*$",
    re.MULTILINE,
)
IMAGE_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
DOMAIN_NAMES = {
    1: "计算机系统",
    2: "信息系统",
    3: "信息安全",
    4: "软件工程",
    5: "数据库",
    6: "系统架构",
    7: "质量属性与评估",
    8: "软件可靠性",
    9: "架构演化",
    10: "未来信息综合技术",
    11: "标准化与知识产权",
    12: "应用数学",
    13: "专业英语",
}


def load_topic_tags(curriculum_path: Path = CURRICULUM_PATH) -> Dict[str, List[str]]:
    """Return ``topic id -> [§tags]`` from ``curriculum.json``."""
    if not curriculum_path.exists():
        return {}
    data = json.loads(curriculum_path.read_text(encoding="utf-8"))
    mapping: Dict[str, List[str]] = {}
    for topic in data.get("topics", []):
        tags = [t for t in (topic.get("raw_tags") or []) if t.startswith("§")]
        if tags:
            mapping[topic["id"]] = tags
    return mapping


def candidate_topics(tag: str, topic_tags: Dict[str, List[str]]) -> List[str]:
    """Tutor topics that cover a paper's ``§N[.M]`` tag (domain fallback)."""
    if not tag:
        return []
    domain = tag.split(".")[0]
    exact, prefix = [], []
    for topic_id, tags in topic_tags.items():
        for candidate in tags:
            if candidate == tag:
                exact.append(topic_id)
            elif candidate.startswith(f"{domain}.") or candidate == domain:
                prefix.append(topic_id)
    ordered = sorted(set(exact)) + sorted(set(prefix) - set(exact))
    return ordered


def _split_option_line(line: str) -> List[str]:
    """Split ``A. 甲  B. 乙`` into individual option strings."""
    stripped = line.strip().lstrip("-*").strip()
    parts = [p.strip() for p in PAPER_OPTION_SPLIT_RE.split(stripped) if p.strip()]
    return parts or [stripped]


def _parse_options(lines: Iterable[str]) -> List[Dict[str, str]]:
    """Collect A–D options from either one-line or multi-line layouts."""
    options: List[Dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or IMAGE_ONLY_RE.match(stripped):
            continue
        for chunk in _split_option_line(stripped):
            probe = re.sub(r"^\*+|\*+$", "", chunk).strip()
            # transcript layout prefixes the first option with its blank number: "(5) A. …"
            probe = re.sub(r"^[（(]\s*\d{1,3}\s*[)）]\s*", "", probe).strip()
            match = OPTION_LINE.match(probe)
            if not match:
                continue
            label = match.group(1)
            if any(option["label"] == label for option in options):
                continue
            options.append({"label": label, "text": _clean_text(match.group(2))})
    return sorted(options, key=lambda item: item["label"])


def _stem_before(lines: Sequence[str]) -> str:
    """Last substantive paragraph above the option block."""
    paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
    paragraphs = [p for p in paragraphs if not IMAGE_ONLY_RE.match(p) and len(p) > 4]
    return _clean_text(paragraphs[-1]) if paragraphs else ""


def parse_paper_transcript(text: str, year: str) -> List[Dict]:
    """Parse the 2009–2017 transcript layout (【答案】/【解析】 blocks)."""
    answers = list(PAPER_ANSWER_RE.finditer(text))
    items: List[Dict] = []
    for position, answer in enumerate(answers):
        window_start = answers[position - 1].end() if position else 0
        window = text[window_start : answer.start()]
        anchors = [int(m.group(1)) for m in PAPER_OPTION_ANCHOR_RE.finditer(window)]
        if not anchors:
            continue
        start, end = min(anchors), max(anchors)
        first_anchor = PAPER_OPTION_ANCHOR_RE.search(window)
        stem = _stem_before(window[: first_anchor.start()].splitlines())
        options = _parse_options(window[first_anchor.start() :].splitlines())
        correct = sorted(set(re.findall(r"[A-D]", answer.group(1))))

        explanation = ""
        explain_match = PAPER_EXPLAIN_RE.search(text, answer.end())
        if explain_match:
            next_start = len(text)
            if position + 1 < len(answers):
                next_window_start = answer.end()
                next_window = text[next_window_start : answers[position + 1].start()]
                next_anchor = PAPER_OPTION_ANCHOR_RE.search(next_window)
                if next_anchor:
                    stem_lines = next_window[: next_anchor.start()].splitlines()
                    paragraphs = [p for p in "\n".join(stem_lines).split("\n\n") if p.strip()]
                    if paragraphs:
                        next_start = next_window_start + next_window.rfind(paragraphs[-1].strip())
            explanation = _clean_text(text[explain_match.end() : next_start])

        tag_match = PAPER_TAG_RE.search(text, answer.end())
        tag = tag_match.group(1) if tag_match and tag_match.start() < (answers[position + 1].start() if position + 1 < len(answers) else len(text)) else ""
        label = tag_match.group(2).strip() if tag_match and tag else ""
        items.append(
            {
                "id": f"past-papers/comprehensive-by-year/{year}.md#{start}-{end}",
                "year": year,
                "range": [start, end],
                "tag": tag,
                "tag_label": label,
                "stem": stem,
                "options": options,
                "correct": correct,
                "explanation": explanation or None,
            }
        )
    return items


def parse_paper_curated(text: str, year: str) -> List[Dict]:
    """Parse the 2018+ curated layout (``### N.`` headers)."""
    headers = list(CURATED_HEADER_RE.finditer(text))
    items: List[Dict] = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        block = text[header.start() : end]
        number = header.group(1)
        lines = block.splitlines()
        header_text = CURATED_HEADER_RE.sub("", lines[0]).strip()
        header_text = re.sub(r"^【题干】\s*", "", header_text).strip()

        answer_match = CURATED_ANSWER_RE.search(block)
        correct = sorted(set(re.findall(r"[A-Z]", answer_match.group(1)))) if answer_match else []
        tag = answer_match.group(2) if answer_match and answer_match.group(2) else ""
        label = (answer_match.group(3) or "").strip() if answer_match else ""

        body_lines = lines[1:]
        option_start = next(
            (i for i, line in enumerate(body_lines) if OPTION_LINE.match(line.strip().lstrip("-*").strip())),
            len(body_lines),
        )
        stem_lines = [header_text] + [line for line in body_lines[:option_start] if line.strip()]
        options = _parse_options(body_lines[option_start:])

        explanation = ""
        explain_match = EXPLAIN_LINE.search(block)
        if explain_match:
            # group(1) is the text on the 解析 line itself; the rest follows it
            explanation = _clean_text(explain_match.group(1) + " " + block[explain_match.end() :])

        items.append(
            {
                "id": f"past-papers/comprehensive-by-year/{year}.md#{number}",
                "year": year,
                "range": [int(number), int(number)],
                "tag": tag,
                "tag_label": label,
                "stem": _clean_text(" ".join(stem_lines)),
                "options": options,
                "correct": correct,
                "explanation": explanation or None,
            }
        )
    return items


def parse_paper(path: Path) -> List[Dict]:
    """Parse either past-paper layout, detecting the format automatically.

    Detection compares both parsers instead of trusting a single heading probe:
    transcript papers sometimes contain an unrelated ``###`` line, and curated
    papers sometimes lack ``【答案】`` blocks entirely.
    """
    text = path.read_text(encoding="utf-8")
    year = path.stem
    transcript_items = parse_paper_transcript(text, year)
    curated_items = parse_paper_curated(text, year) if CURATED_HEADER_RE.search(text) else []
    items = curated_items if len(curated_items) > len(transcript_items) else transcript_items
    topic_tags = load_topic_tags()
    for item in items:
        item["candidate_topics"] = candidate_topics(item.get("tag", ""), topic_tags)
    return items


BLOCK_HEADER = re.compile(r"^###\s+(\d+)[.\s]", re.MULTILINE)
# Match option lines in all supported shapes:
#   "A. text", "- A. text", "* A. text", "**A. text**"
# Correct-answer prefixes (``✅`` and outer ``**``) are stripped from the
# probe string before matching so the same regex handles both plain and marked
# lines.
OPTION_LINE = re.compile(
    r"""^\s*
        (?:[-*]\s+)?           # optional bullet marker
        ([A-Z])                # 1: option letter
        [\.\)、]                # separator: dot, right paren, or Chinese enum comma
        \s*
        (.+?)                  # 2: option text (non-greedy)
        \s*$""",
    re.VERBOSE,
)
BOLD_MARK = re.compile(r"\*\*(.+?)\*\*")
ANSWER_LINE = re.compile(
    r"^\s*\*\*\s*答\s*案\s*\*\*\s*[:：]\s*(.+?)\s*$", re.MULTILINE
)
EXPLAIN_LINE = re.compile(
    r"^\s*\*\*\s*解\s*析\s*\*\*\s*[:：]\s*(.*)$", re.MULTILINE
)
SEPARATOR_LINE = re.compile(r"^\s*-{3,}\s*$")
CHECK_MARK = "✅"


def split_blocks(text: str) -> Dict[str, str]:
    """Return a mapping of question number -> raw block text (with header)."""
    blocks: Dict[str, str] = {}
    positions = [(m.start(), m.group(1)) for m in BLOCK_HEADER.finditer(text)]
    for i, (start, num) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        blocks[num] = text[start:end]
    return blocks


def _clean_text(value: str) -> str:
    """Remove residual markers and collapse whitespace."""
    value = value.replace(CHECK_MARK, "")
    value = BOLD_MARK.sub(r"\1", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _probe_option(stripped: str):
    """Return (letter, body, is_marked_correct) or None if not an option line.

    Peels off ``✅`` and outer ``**...**`` wrappers (both of which signal the
    correct answer in exam-bank markdown) before attempting to match the
    option pattern. ``is_marked_correct`` is True when any such marker was
    present.
    """
    marked = False
    probe = stripped

    # Peel ✅ prefix (may be followed by whitespace or **)
    if probe.startswith(CHECK_MARK):
        marked = True
        probe = probe[len(CHECK_MARK):].lstrip()

    # Peel a leading bullet marker so we can inspect the payload
    bullet = ""
    if probe.startswith(("- ", "* ")):
        bullet = probe[:2]
        probe = probe[2:].lstrip()

    # Peel outer ** ... ** wrapping the entire option payload
    if probe.startswith("**") and probe.endswith("**") and len(probe) >= 4:
        marked = True
        probe = probe[2:-2].strip()

    # Restore bullet so OPTION_LINE can still recognise the shape uniformly
    probe = f"{bullet}{probe}" if bullet else probe

    m = OPTION_LINE.match(probe)
    if not m:
        return None
    return m.group(1), m.group(2).strip(), marked


def parse_block(raw: str) -> Dict:
    """Parse one raw block into a structured item."""
    lines = raw.splitlines()
    header = lines[0] if lines else ""
    header_body = re.sub(r"^###\s+\d+[.\s]\s*", "", header).strip()
    header_body = _clean_text(header_body)

    stem_parts: List[str] = [header_body] if header_body else []
    options: List[Dict[str, str]] = []
    correct_from_marker: List[str] = []
    answer_from_line: List[str] = []
    explanation_lines: List[str] = []
    in_explanation = False
    seen_first_option = False

    for line in lines[1:]:
        if SEPARATOR_LINE.match(line):
            in_explanation = False
            continue

        m_ans = ANSWER_LINE.match(line)
        if m_ans:
            answer_from_line = re.findall(r"[A-Z]", m_ans.group(1))
            in_explanation = False
            continue

        m_exp = EXPLAIN_LINE.match(line)
        if m_exp:
            in_explanation = True
            first = m_exp.group(1).strip()
            if first:
                explanation_lines.append(first)
            continue

        if in_explanation:
            if line.strip():
                explanation_lines.append(line.strip())
            continue

        stripped = line.strip()
        if not stripped:
            continue

        probed = _probe_option(stripped)
        if probed is not None:
            label, body, is_marked = probed
            if is_marked:
                correct_from_marker.append(label)
            options.append({"label": label, "text": _clean_text(body)})
            seen_first_option = True
            continue

        # Not an option line. Before the first option → stem continuation.
        # After options started → likely stray/answer hint; drop silently.
        if not seen_first_option:
            stem_parts.append(_clean_text(stripped))

    correct = answer_from_line or correct_from_marker
    return {
        "stem": " ".join(p for p in stem_parts if p).strip(),
        "options": options,
        "correct": sorted(set(correct)),
        "explanation": " ".join(explanation_lines).strip() or None,
    }


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    tag_filter = topic_filter = year_filter = None
    limit: int | None = None
    list_only = False
    positional: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--tag", "--topic", "--year", "--limit"}:
            if index + 1 >= len(args):
                print(f"error: {token} 需要一个值", file=sys.stderr)
                return 2
            value = args[index + 1]
            if token == "--tag":
                tag_filter = value
            elif token == "--topic":
                topic_filter = value
            elif token == "--year":
                year_filter = value
            else:
                try:
                    limit = int(value)
                except ValueError:
                    print("error: --limit 需要整数", file=sys.stderr)
                    return 2
            index += 2
            continue
        if token == "--list":
            list_only = True
            index += 1
            continue
        positional.append(token)
        index += 1

    if list_only:
        topic_tags = load_topic_tags()
        for paper in sorted(PAPER_DIR.glob("*.md")):
            items = parse_paper(paper)
            domains = sorted({item["tag"] for item in items if item.get("tag")})
            print(f"{paper.stem}\t{len(items)} 题块\t{' '.join(domains)}")
        return 0

    # 真题模式：显式给出 past-papers 文件，或用 --topic/--year 跨文件抽题
    wants_paper = bool(topic_filter or year_filter) or (positional and "past-papers" in positional[0])
    if wants_paper:
        paths = [Path(positional[0])] if positional and "past-papers" in positional[0] else sorted(PAPER_DIR.glob("*.md"))
        selected: List[Dict] = []
        for paper in paths:
            if not paper.exists():
                print(f"error: file not found: {paper}", file=sys.stderr)
                return 2
            if year_filter and paper.stem != year_filter:
                continue
            for item in parse_paper(paper):
                if tag_filter and not (item.get("tag") or "").startswith(tag_filter):
                    continue
                if topic_filter and topic_filter not in item.get("candidate_topics", []):
                    continue
                if not item.get("options") or not item.get("correct"):
                    continue
                selected.append(item)
        if limit is not None:
            selected = selected[:limit]
        if not selected:
            print("没有匹配的真题（检查 --tag/--topic/--year 或该年是否缺题）", file=sys.stderr)
            return 1
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return 0

    if len(positional) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(positional[0])
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    numbers = [n.strip() for n in positional[1:] if n.strip()]
    text = path.read_text(encoding="utf-8")
    blocks = split_blocks(text)
    items: List[Dict] = []
    missing: List[str] = []
    for n in numbers:
        raw = blocks.get(n)
        if raw is None:
            missing.append(n)
            continue
        item = parse_block(raw)
        item["id"] = f"{path.as_posix()}#{n}"
        items.append(item)
    if missing:
        print(
            f"warning: missing question numbers in {path}: {', '.join(missing)}",
            file=sys.stderr,
        )
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
