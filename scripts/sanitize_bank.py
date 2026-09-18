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
INLINE_OPTION_START_RE = re.compile(r"(?<!\S)([A-D])[.．、]\s*")
CURATED_HEADER_RE = re.compile(
    r"^###\s*(\d+)(?:\s*[-–—]\s*(\d+))?[.、]\s*", re.MULTILINE
)
CURATED_ANSWER_RE = re.compile(
    r"^\*\*\s*答案\s*[:：]\s*([A-Z](?:\s*[、,，]?\s*[A-Z])*)\s*\*\*"
    r"(?:\s*\|\s*\*\*\s*考点\s*\*\*\s*[:：]\s*(§[0-9]+(?:\.[0-9]+)?)\s*(.*))?\s*$",
    re.MULTILINE,
)
IMAGE_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
QUESTION_GROUP_HEADER_RE = re.compile(r"^##\s*第\s*\d+(?:\s*[-–—]\s*\d+)?\s*题")
PASSAGE_HEADER_RE = re.compile(r"^##\s+(Passage\s+\d+[^\n]*)\s*$", re.MULTILINE)
TRAILING_OPTIONS_LABEL_RE = re.compile(r"(?:\s|^)(?:选项(?:如下)?|options?)\s*[:：]\s*$", re.IGNORECASE)
PLACEHOLDER_STEM_RE = re.compile(r"^[（(]\s*\d{1,3}\s*[)）]$")
PRIOR_CONTEXT_RE = re.compile(
    r"^\s*(?:(?:接|承|同|见)?上题|将上题|接前题|(?:在|基于)\s*第\s*\d+\s*题\s*(?:的)?基础(?:上)?)"
)
QUALITY_EXCLUSIONS_PATH = REPO_ROOT / "scripts" / "quiz_quality_exclusions.json"
# 题干里真正指向“缺失图表”的指代。裸的“XX图中”多是术语（用例图/数据流图/视图），
# 由 FIGURE_TERM_PREFIXES 排除，避免把“用例图中，…”误判成缺图。
FIGURE_DEICTIC_RE = re.compile(r"(?:如下|见下|以下|下面|如|见|下|上|该|本|此)\s*图")
FIGURE_CONTENT_RE = re.compile(
    r"图\s*中\s*(?:标出|给出|与|的|箭头|各|所|[①-⑩])|图\s*中\s*[，,]|图\s*略"
)
FIGURE_TERM_PREFIXES = (
    "视",
    "用例",
    "数据流",
    "状态",
    "顺序",
    "活动",
    "部署",
    "组件",
    "对象",
    "前趋",
    "流程",
    "网络",
    "结构",
    "关系",
    "架构",
    "执行",
    "因果",
    "时序",
    "协作",
    "通信",
    "类",
    "包",
)
TABLE_REF_RE = re.compile(
    r"(?:如下|见下|以下|下面|下|上|该|本|此)\s*表|表\s*中\s*(?:给|标|所|[，,])|见表|此表"
)
MARKDOWN_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
ANSWER_LEAK_RE = re.compile(r"✅|(?:\*\*)?(?:答案|解析|考点)(?:\*\*)?\s*[:：]|【(?:答案|解析)】")
EXPLANATION_LEAK_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:答案|考点|解析)\s*(?:\*\*)?\s*[:：]", re.MULTILINE
)
NEXT_QUESTION_HEADER_RE = re.compile(
    r"^(?:---\s*\n+)?#{2,3}\s*(?:第\s*)?\d+(?:\s*[-–—]\s*\d+)?(?:[.、]|题)?(?:\s|$)",
    re.MULTILINE,
)
RESOURCE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
OPTION_LABELS = ("A", "B", "C", "D")
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

# Older papers carry only domain-level tags such as ``§4`` and ``§6``.  Their
# human-readable labels are much more specific, so use those labels to avoid
# advertising every topic in the same domain as an equally valid candidate.
DOMAIN_LABEL_TOPIC_RULES = {
    "§1": (
        ("K07.REALTIME_EMBEDDED", ("嵌入式", "实时系统", "实时操作系统", "rtos")),
        ("K02.NETWORK_PROTOCOLS", ("网络", "协议", "通信", "域名解析")),
        ("K14.OS_SCHEDULING_FILES", ("磁盘调度", "文件系统", "文件索引", "调度算法")),
        ("K01.OS_MEMORY_KERNEL", ("操作系统", "进程", "线程", "死锁", "pv操作", "页式", "存储管理")),
        ("K18.COMPUTER_ARCH_STORAGE", ("计算机系统", "存储系统", "处理器", "cpu", "mips", "指令", "总线", "性能")),
    ),
    "§2": (("K24.INFORMATION_SYSTEMS", ("信息系统", "erp", "crm", "电子政务", "商业智能", "企业信息", "系统集成")),),
    "§3": (("K20.SECURITY_FOUNDATIONS", ("信息安全", "安全", "加密", "认证", "pki", "kerberos", "攻击")),),
    "§4": (
        ("K03.SOFTWARE_DESIGN_UML", ("uml", "面向对象", "内聚", "耦合", "类图", "用例")),
        ("K05.TEST_CMMI_PATTERNS", ("测试", "cmmi", "质量保证", "设计模式")),
        ("K15.STRUCTURED_ANALYSIS_DFD", ("结构化", "dfd", "数据流图")),
        ("K16.REQUIREMENTS_MANAGEMENT", ("需求", "基线", "变更控制")),
        ("K23.PROJECT_MANAGEMENT_METRICS", ("项目", "范围", "进度", "成本", "挣值", "配置项", "活动定义", "度量")),
        ("K08.SOFTWARE_PROCESS_MODELS", ("软件过程", "开发模型", "rup", "敏捷", "原型", "迭代")),
        ("K06.DESIGN_DATA_VIEWS", ("概要设计", "数据设计", "界面设计", "输入设计", "输出设计", "详细设计")),
    ),
    "§5": (("K10.DATABASE_MODELING", ("数据库", "关系", "范式", "sql", "事务", "索引")),),
    "§6": (
        ("K12.PATTERNS_SOA_MICROSERVICES", ("微服务", "soa", "esb", "web服务", "soap", "wsdl", "设计模式")),
        ("K21.MESSAGING_CACHE", ("消息", "缓存", "中间件")),
        ("K26.ARCH_EVOLUTION", ("演化", "迁移", "维护")),
        ("K11.COMPONENTS_4PLUS1", ("构件", "4+1")),
        ("K13.VIEWS_SOA_LAYERING", ("分层", "层次", "架构视图")),
        ("K04.ARCH_STYLES_ABSD", ("架构风格", "absd", "dssa", "架构需求", "架构设计", "架构复审", "架构定义", "架构作用", "架构描述")),
    ),
    "§7": (
        ("K19.ATAM_TACTICS", ("atam", "saam", "架构评估", "四类点", "敏感点", "权衡点")),
        ("K09.QUALITY_SCENARIOS", ("质量属性", "质量场景", "质量战术", "质量策略")),
    ),
    "§8": (("K25.RELIABILITY_ENGINEERING", ("可靠性", "容错", "故障")),),
    "§9": (("K26.ARCH_EVOLUTION", ("演化", "迁移", "维护", "遗留")),),
    "§10": (("K27.EMERGING_TECH", ("人工智能", "云计算", "物联网", "区块链", "边缘计算", "数字孪生", "cps", "大模型")),),
    "§11": (("K17.IP_COPYRIGHT", ("知识产权", "标准", "著作权", "商标", "专利", "商业秘密")),),
    "§12": (("K28.MATH_OPERATIONS", ("应用数学", "概率", "图论", "运筹", "线性规划", "决策")),),
    "§13": (("K22.ENGLISH_READING", ("专业英语", "英语", "阅读")),),
}

ITEM_TOPIC_OVERRIDES = {
    "past-papers/comprehensive-by-year/2012下.md#17-17": "K18.COMPUTER_ARCH_STORAGE",
    "past-papers/comprehensive-by-year/2022.md#32": "K12.PATTERNS_SOA_MICROSERVICES",
    "past-papers/comprehensive-by-year/2024下.md#1": "K20.SECURITY_FOUNDATIONS",
    "past-papers/comprehensive-by-year/2024下.md#38": "K03.SOFTWARE_DESIGN_UML",
    "past-papers/comprehensive-by-year/2025上.md#23": "K01.OS_MEMORY_KERNEL",
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


def candidate_topics(
    tag: str,
    topic_tags: Dict[str, List[str]],
    *,
    label: str = "",
    item_id: str = "",
) -> List[str]:
    """Tutor topics for a paper item, preferring precise item/label evidence."""
    override = ITEM_TOPIC_OVERRIDES.get(item_id)
    if override:
        return [override]
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
    normalized_label = normalize_label(label)
    for topic_id, keywords in DOMAIN_LABEL_TOPIC_RULES.get(domain, ()):
        if any(keyword.casefold() in normalized_label for keyword in keywords):
            return [topic_id]
    if normalized_label:
        # A detailed source label that cannot be mapped is unknown, not proof
        # that the question belongs to every tutor topic in the same domain.
        return []
    exact_topics = sorted(set(exact))
    if "." in tag and exact_topics:
        return exact_topics
    return exact_topics + sorted(set(prefix) - set(exact_topics))


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _parse_options(lines: Iterable[str]) -> List[Dict[str, str]]:
    """Collect A–D options from either one-line or multi-line layouts."""
    options: List[Dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or IMAGE_ONLY_RE.match(stripped):
            continue
        for chunk in _split_option_line(stripped):
            probe = chunk.strip()
            if probe.startswith(CHECK_MARK):
                probe = probe[len(CHECK_MARK) :].lstrip()
            probe = _strip_option_markdown_wrapper(probe)
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
    return clean_stem(paragraphs[-1]) if paragraphs else ""


def clean_stem(value: str) -> str:
    """Normalize a learner-facing stem without keeping layout scaffolding."""

    cleaned = _clean_text(value)
    cleaned = re.sub(r"^(?:【\s*解析\s*】|\*\*\s*解析\s*\*\*\s*[:：])\s*", "", cleaned)
    cleaned = re.sub(r"(?:\s|^)---\s*$", "", cleaned)
    return TRAILING_OPTIONS_LABEL_RE.sub("", cleaned).strip()


def _transcript_stem(lines: Sequence[str]) -> str:
    """Recover a full transcript-group stem instead of its last paragraph.

    Older papers place a ``## 第 N 题`` heading before a multi-paragraph
    scenario.  The previous parser kept only the final paragraph, turning
    valid prompts into fragments such as ``个顺序执行的阶段。``.
    """

    heading_indexes = [
        index for index, line in enumerate(lines) if QUESTION_GROUP_HEADER_RE.match(line)
    ]
    if not heading_indexes:
        return _stem_before(lines)
    candidate_lines = lines[heading_indexes[-1] + 1 :]
    paragraphs = [
        paragraph.strip()
        for paragraph in "\n".join(candidate_lines).split("\n\n")
        if paragraph.strip()
    ]
    paragraphs = [
        paragraph
        for paragraph in paragraphs
        if not IMAGE_ONLY_RE.match(paragraph) and not paragraph.startswith("**考点**")
    ]
    return clean_stem(" ".join(paragraphs)) if paragraphs else ""


def _transcript_source_fragment(lines: Sequence[str]) -> str:
    """Return the source portion belonging to the current transcript group."""

    heading_indexes = [
        index for index, line in enumerate(lines) if QUESTION_GROUP_HEADER_RE.match(line)
    ]
    relevant = lines[heading_indexes[-1] + 1 :] if heading_indexes else lines
    return "\n".join(relevant)


def _ordered_inline_option_chunks(value: str) -> List[str]:
    """Split compact ``A. ... B. ...`` layouts only when order is credible."""

    matches = list(INLINE_OPTION_START_RE.finditer(value))
    if len(matches) < 2:
        return [value]
    selected = [matches[0]]
    for match in matches[1:]:
        previous = selected[-1].group(1)
        if OPTION_LABELS.index(match.group(1)) == OPTION_LABELS.index(previous) + 1:
            selected.append(match)
        else:
            break
    if len(selected) < 2:
        return [value]
    return [
        value[match.start() : selected[index + 1].start() if index + 1 < len(selected) else len(value)].strip()
        for index, match in enumerate(selected)
    ]


def _split_option_line(line: str) -> List[str]:
    """Split option lines while preserving literal option text such as ``*``."""

    stripped = line.strip().lstrip("-*").strip()
    parts = [part.strip() for part in PAPER_OPTION_SPLIT_RE.split(stripped) if part.strip()]
    if len(parts) >= 2:
        return parts
    fallback = _ordered_inline_option_chunks(stripped)
    return fallback if len(fallback) >= 2 else (parts or [stripped])


def _strip_option_markdown_wrapper(value: str) -> str:
    """Remove only an outer emphasis wrapper, never a literal ``*`` answer."""

    stripped = value.strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    return stripped


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
        pre_option_lines = window[: first_anchor.start()].splitlines()
        source_fragment = _transcript_source_fragment(pre_option_lines)
        stem = _transcript_stem(pre_option_lines)
        options = _parse_options(window[first_anchor.start() :].splitlines())
        answer_sequence = re.findall(r"[A-D]", answer.group(1))
        correct = sorted(set(answer_sequence))

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
            explanation = clean_explanation(text[explain_match.end() : next_start])

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
                "explanation": explanation,
                "question_count": len(anchors),
                "answer_sequence": answer_sequence,
                "source_figure_status": (
                    "figure_not_renderable"
                    if RESOURCE_LINK_RE.search(source_fragment)
                    else (
                        "missing_required_figure"
                        if "原图含机构广告或水印，已移除" in source_fragment
                        else None
                    )
                ),
                "source_requires_table": bool(
                    re.search(r"<table\b", source_fragment, re.IGNORECASE)
                ),
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
        first_number = int(header.group(1))
        last_number = int(header.group(2) or header.group(1))
        number = (
            f"{first_number}-{last_number}"
            if first_number != last_number
            else str(first_number)
        )
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
            explanation = clean_explanation(
                explain_match.group(1) + " " + block[explain_match.end() :]
            )

        items.append(
            {
                "id": f"past-papers/comprehensive-by-year/{year}.md#{number}",
                "year": year,
                "range": [first_number, last_number],
                "tag": tag,
                "tag_label": label,
                "stem": clean_stem(" ".join(stem_lines)),
                "options": options,
                "correct": correct,
                "explanation": explanation,
            }
        )
    return items


def _attach_curated_followup_context(items: List[Dict]) -> None:
    """Recover the safe subset of curated follow-up questions.

    ``上题对应架构策略`` follows a complete, standalone quality-attribute
    scenario in the immediately preceding item.  Carry that scenario as a
    structured context instead of asking the learner to reconstruct it. Other
    follow-ups (LRU traces, omitted SQL diagrams, process figures) remain
    untrusted and are filtered by ``assess_quality``.
    """

    for index, item in enumerate(items):
        stem = str(item.get("stem") or "")
        if not re.match(r"^\s*上题对应架构策略\s*[:：]?\s*$", stem):
            continue
        if index == 0:
            continue
        previous = items[index - 1]
        previous_stem = str(previous.get("stem") or "").strip()
        if not previous_stem or PRIOR_CONTEXT_RE.match(previous_stem):
            continue
        item["context_id"] = f"{item['id']}#previous"
        item["context_title"] = "关联题干"
        item["context"] = previous_stem


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
    if items is curated_items:
        _attach_curated_followup_context(items)
    topic_tags = load_topic_tags()
    exclusions = load_quality_exclusions()
    for item in items:
        item["candidate_topics"] = candidate_topics(
            item.get("tag", ""),
            topic_tags,
            label=item.get("tag_label", ""),
            item_id=item["id"],
        )
        item.update(assess_quality(item, source_path=path, exclusions=exclusions))
    return items


def _passage_contexts(text: str, source: Path) -> Dict[str, Dict[str, str]]:
    """Map numbered fill-in questions to their shared reading passage."""

    result: Dict[str, Dict[str, str]] = {}
    headings = list(PASSAGE_HEADER_RE.finditer(text))
    try:
        source_id = source.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        source_id = source.as_posix()
    for index, heading in enumerate(headings, 1):
        segment_end = headings[index].start() if index < len(headings) else len(text)
        questions = list(BLOCK_HEADER.finditer(text, heading.end(), segment_end))
        if not questions:
            continue
        context = clean_stem(text[heading.end() : questions[0].start()])
        if not context:
            continue
        context_id = f"{source_id}#passage-{index}"
        for question in questions:
            result[question.group(1)] = {
                "context_id": context_id,
                "context_title": heading.group(1).strip(),
                "context": context,
            }
    return result


def parse_exam_bank(path: Path) -> List[Dict]:
    """Parse an authored bank file and attach any shared passage context."""

    text = path.read_text(encoding="utf-8")
    contexts = _passage_contexts(text, path)
    exclusions = load_quality_exclusions()
    items: List[Dict] = []
    try:
        source_id = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        source_id = path.as_posix()
    for number, raw in split_blocks(text).items():
        item = parse_block(raw)
        item["id"] = f"{source_id}#{number}"
        item.update(contexts.get(number, {}))
        item.update(assess_quality(item, source_path=path, exclusions=exclusions))
        items.append(item)
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


def clean_explanation(value: str | None) -> str | None:
    """Return an explanation that is safe to render verbatim.

    Raw transcripts leak the next question's header, answer lines and asset
    paths into the explanation. Everything after the next question header is
    dropped, and the remaining text keeps only prose.
    """

    if not value:
        return None
    text = value
    next_header = NEXT_QUESTION_HEADER_RE.search(text)
    if next_header:
        text = text[: next_header.start()]
    text = re.sub(r"【\s*答案\s*】[^\n]*", " ", text)
    text = re.sub(
        r"^\s*(?:\*\*)?\s*(?:答案|考点)\s*(?:\*\*)?\s*[:：][^\n]*$",
        " ",
        text,
        flags=re.MULTILINE,
    )
    text = RESOURCE_LINK_RE.sub(" ", text)
    text = re.sub(r"【\s*解析\s*】", " ", text)
    text = re.sub(r"^\s*(?:\*\*)?\s*解析\s*(?:\*\*)?\s*[:：]", " ", text, flags=re.MULTILINE)
    text = re.sub(r"(?m)^---\s*$", " ", text)
    text = _clean_text(text)
    return text or None


def _is_compound_figure(stem: str, index: int) -> bool:
    """True when the ``图`` at ``index`` belongs to a term like 用例图/视图."""

    return any(
        stem[max(0, index - len(prefix)) : index] == prefix
        for prefix in FIGURE_TERM_PREFIXES
    )


def references_figure(stem: str) -> bool:
    """True when the stem points at a figure the learner must see."""

    for pattern in (FIGURE_DEICTIC_RE, FIGURE_CONTENT_RE):
        for match in pattern.finditer(stem or ""):
            figure_index = (match.start() + match.group(0).rfind("图"))
            if not _is_compound_figure(stem, figure_index):
                return True
    return False


def references_table(stem: str) -> bool:
    """True when the stem points at a table the learner must see."""

    return bool(TABLE_REF_RE.search(stem or ""))


def load_quality_exclusions(path: Path | None = None) -> Dict[str, str]:
    """Load the maintainer deny-list of items that must never be quizzed."""

    target = path or QUALITY_EXCLUSIONS_PATH
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"题目质量排除表损坏：{target}") from error
    entries = payload.get("exclusions") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("题目质量排除表需要 exclusions 数组")
    result: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("题目质量排除项必须是对象")
        item_id = entry.get("item_id")
        reason = entry.get("reason")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("题目质量排除项缺少 item_id")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"题目质量排除项 {item_id} 缺少 reason")
        result[item_id] = reason
    return result


def assess_quality(
    item: Dict,
    *,
    source_path: Path | None = None,
    exclusions: Dict[str, str] | None = None,
) -> Dict:
    """Judge whether one parsed item may be shown as a blind quiz question.

    The gate is deliberately conservative: an item that references a figure or
    table it does not carry, leaks an answer marker, or has an unusable answer
    key is reported with a stable issue id instead of being quietly repaired.
    """

    issues: List[str] = []
    stem = str(item.get("stem") or "").strip()
    options = item.get("options") or []
    correct = [letter for letter in (item.get("correct") or []) if letter]
    labels = [str(option.get("label") or "") for option in options]
    texts = [str(option.get("text") or "").strip() for option in options]
    explanation = item.get("explanation")
    has_context = bool(str(item.get("context") or "").strip())
    image_links = RESOURCE_LINK_RE.findall(stem)
    requires_figure = references_figure(stem) or bool(image_links)
    requires_table = references_table(stem) or bool(MARKDOWN_TABLE_RE.search(stem))

    if not stem:
        issues.append("empty_stem")
    if len(options) < 2 or any(not text for text in texts):
        issues.append("missing_options")
    if labels != list(OPTION_LABELS):
        issues.append("incomplete_option_set")
    if len(set(labels)) != len(labels):
        issues.append("duplicate_options")
    if not correct or any(letter not in labels for letter in correct):
        issues.append("answer_not_in_options")
    if ANSWER_LEAK_RE.search(stem) or any(ANSWER_LEAK_RE.search(text) for text in texts):
        issues.append("answer_marker_leak")
    if (PLACEHOLDER_STEM_RE.fullmatch(stem) or PRIOR_CONTEXT_RE.match(stem)) and not has_context:
        issues.append("missing_required_context")
    if int(item.get("question_count", 1) or 1) > 1:
        # The objective-quiz runtime currently records one answer per visible
        # item. A transcript block with several independent blanks would lose
        # answer order and option ownership, so keep it out until it has a
        # first-class subquestion representation.
        issues.append("multi_question_group")
    source_figure_status = item.get("source_figure_status")
    if source_figure_status:
        issues.append(str(source_figure_status))
    if item.get("source_requires_table"):
        issues.append("missing_required_table")
    if image_links:
        # quiz-prepare currently exposes a text-only public contract. A raw
        # repository-relative Markdown path is not a renderable learner asset,
        # so keep image-dependent questions out until the runtime can return a
        # structured figure or an approved textual substitute.
        issues.append("figure_not_renderable")
    elif requires_figure:
        issues.append("missing_required_figure")
    if requires_table and not MARKDOWN_TABLE_RE.search(stem):
        issues.append("missing_required_table")
    for link in image_links:
        target = link.split("#", 1)[0].strip()
        if target.startswith(("http://", "https://")):
            continue
        base = source_path.parent if source_path is not None else REPO_ROOT
        if not (base / target).resolve().exists():
            issues.append("missing_figure_asset")
            break
    if explanation and (
        NEXT_QUESTION_HEADER_RE.search(explanation)
        or EXPLANATION_LEAK_RE.search(explanation)
        or RESOURCE_LINK_RE.search(explanation)
    ):
        issues.append("explanation_leak")

    item_id = str(item.get("id") or "")
    excluded_reason = (exclusions or {}).get(item_id)
    if excluded_reason:
        issues.append(f"excluded:{excluded_reason}")
    return {
        "quality_status": "ready" if not issues else "invalid",
        "quality_issues": sorted(set(issues)),
        "requires_figure": requires_figure,
        "requires_table": requires_table,
        "requires_context": has_context,
    }


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
    header_body = clean_stem(header_body)

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
        "stem": clean_stem(" ".join(p for p in stem_parts if p)),
        "options": options,
        "correct": sorted(set(correct)),
        "explanation": clean_explanation(" ".join(explanation_lines)),
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
    parsed_by_number = {
        item["id"].rsplit("#", 1)[1]: item for item in parse_exam_bank(path)
    }
    items: List[Dict] = []
    missing: List[str] = []
    for n in numbers:
        item = parsed_by_number.get(n)
        if item is None:
            missing.append(n)
            continue
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
