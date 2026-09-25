#!/usr/bin/env python3
"""Verify that paper-layout normalization did not alter question content.

The normalizer deliberately changes Markdown scaffolding, but it must never
change the fields the coach serves to learners.  This checker parses the
current paper and the same path at a baseline Git revision with the *current*
strict parser (including its explicit legacy adapter), then compares every
item's stem, options, correct answer and explanation byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = REPO_ROOT / "past-papers" / "comprehensive-by-year"
SANITIZER_PATH = REPO_ROOT / "scripts" / "sanitize_bank.py"
FIELDS = ("stem", "options", "correct", "explanation")


def content_digest(item: Dict[str, Any]) -> str:
    """Pin all learner-visible fields of a reviewed paper repair exactly."""

    content = {field: item.get(field) for field in FIELDS}
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Reviewed against the local 2019/2022 source question PDFs and the 2022
# accompanying answer analysis. Unlike a broad baseline refresh, each digest
# approves exactly one repaired item, including its unchanged fields.
REVIEWED_ITEM_DIGESTS = {
    "past-papers/comprehensive-by-year/2019下.md": {
        (1, 1): "670b801435ff80dbf31159edd75011615e023bfafc36ddc3981f115063b61048",
        (2, 2): "05a8f9a23a9b27e29bf515fce2ee20b82f45c0d58828fba45b83cc15b5c207ae",
        (3, 3): "0511faa7d445f6f5ad3cacfab122a8ffea1ed1132b044922f73eaeb91d7ac577",
        (71, 71): "be9f6be44195c9e19a24fd54a7de13e193b7e21e7cac430d3eacae5d6c7e97f1",
        (72, 72): "d7ddfb92164465d534d902f78beb020f6acf99963381ccced394b5040d310fb3",
        (73, 73): "8ccabcb96d2da880c3844aa0a451fee3d95556baa4ee94102744c41628364920",
        (74, 74): "767f9c7d70161fff7b99e507ead2833d3d9af674aa5e8654fa4fe2b600fd7c78",
        (75, 75): "9c825cf91b36b39e82283284054ca21319e4a1c13b8080e2fa13da3b20aa059c",
    },
    "past-papers/comprehensive-by-year/2022.md": {
        (2, 2): "88d7f35e07815a2092977aa428d8683577e309887005327d6b0ce3a095f8fbf3",
        (8, 8): "d79bd6669b06404b2b379d52aaa8dfdb6b3d047128fcc76d112f6dc5ec9961df",
        (14, 14): "e6df1070420cc68ec8934e9c14e6d056435768f4463bdab241e2e6db67d2424d",
        (15, 15): "417e4999c9878e27ef89211013ea4d50666f647a111a8564a1948a918994ad6b",
        (26, 26): "c711074699a59cc9f6e2f489675d680bc268dcf86d6cc843e14faf7cb8194f2a",
        (30, 30): "b77a3e1ba3230b861c9c62652aa620cb9e265d6b03e263a19e332af515f115a5",
        (50, 50): "44c55566e82394ccbe4f4259c6ff9ced09b6007465227cbbf2df0c9caa40c1df",
        (70, 70): "74cce5c8f61b5875efc692b2fd42f91c626a6fb546d84530cc3949e90923cccd",
    },
}
# These source fragments were present in the baseline Markdown but could not
# produce a parser item because their scan lost required options/table text.
# Normalising their headers makes them visible to the quality gate; they must
# remain invalid and are never admitted to the quiz pool.  Keep this allowlist
# exact so a newly introduced usable question still fails the lossless check.
BASELINE_UNPARSEABLE_ADDITIONS = {
}
# The old transcript parser consumed following blocks into this explanation.
# The canonical split must remove only that trailing parser pollution; require
# the corrected text to be a non-empty prefix of the baseline value.
BASELINE_TRAILING_POLLUTION_FIELDS = {
}
# The source image remains in canonical Markdown, but the strict curated
# parser intentionally keeps raw asset paths out of learner-facing text.
BASELINE_RAW_ASSET_FIELD_EXCEPTIONS = {
}
# A reviewed group split rewrites one multi-blank block into a carrier plus one
# item per blank.  The carrier keeps the shared scenario and every child gets
# its own option bank and answer, so no exam text is invented or lost.  The
# check below still proves line-level preservation against the baseline.
REVIEWED_GROUP_SPLITS = {
    "past-papers/comprehensive-by-year/2019下.md": {
        (16, 17),
        (18, 19),
        (22, 23),
        (35, 37),
        (39, 40),
        (42, 43),
        (51, 53),
        (58, 63),
    },
    "past-papers/comprehensive-by-year/2023下.md": {
        (11, 12),
        (24, 25),
        (27, 28),
        (35, 36),
        (43, 44),
        (49, 50),
        (53, 54),
        (58, 59),
        (63, 64),
        (68, 69),
    },
    "past-papers/comprehensive-by-year/2025上.md": {
        (31, 35),
    },
}
GROUP_HEADING_RE = re.compile(r"^### (\d+)(?:-(\d+))?\.\s*$", re.MULTILINE)
GROUP_MARKER_LINE_RE = re.compile(r"^[（(]\s*\d{1,3}\s*[)）]$")
GROUP_ANSWER_LINE_RE = re.compile(r"^\*\*答案\*\*\s*[:：]")


def _block_text(text: str, start: int, end: int) -> str:
    """Return one ``### start-end.`` block, bounded by the next ``###`` line."""

    match = GROUP_HEADING_RE.search(text)
    while match:
        first = int(match.group(1))
        last = int(match.group(2) or match.group(1))
        if (first, last) == (start, end):
            following = text.find("\n### ", match.end())
            return text[match.start() : following if following != -1 else len(text)]
        match = GROUP_HEADING_RE.search(text, match.end())
    return ""


def _preserved_lines(block: str) -> List[str]:
    """Lines that a split must carry over verbatim.

    ``(N)`` scaffolding and the single ``**答案**`` line are rewritten by
    design; every other non-empty line -- stem, options, tag, explanation --
    has to survive somewhere in the rewritten region.
    """

    kept = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if GROUP_MARKER_LINE_RE.match(stripped) or GROUP_ANSWER_LINE_RE.match(stripped):
            continue
        kept.append(stripped)
    return kept


# A reviewed content correction made on ``dev`` from the authoritative paper
# is not layout drift.  Store the exact approved value so the exception cannot
# silently absorb a later, different edit to the same field.
REVIEWED_CONTENT_FIXES = {
    # 6d934d6 reviewed three conflicting answer keys against their stems and
    # replaced the accompanying explanations. Pin both fields exactly so any
    # subsequent change still fails the lossless source check.
    "past-papers/comprehensive-by-year/2022.md": {
        (4, 4): {
            "correct": ["C"],
            "explanation": (
                "先从 20 号柱面移至最近的 21 号柱面，依次处理 ④、⑥；再处理 22 号柱面的 ⑨；"
                "随后处理 18 号柱面的 ⑤、⑦、①；最后处理 16 号柱面的 ②、⑧、③。"
                "因此响应序列为 ④⑥⑨⑤⑦①②⑧③，选 C。"
            ),
        },
    },
    "past-papers/comprehensive-by-year/2025下.md": {
        (1, 1): {
            "correct": ["A"],
            "explanation": (
                "根据约束条件枚举可知，A22 只能为 6：此时 A12=3、A23=7、A33=8，且 A11、A21、A31 "
                "可分别为 2、4、5 或 4、2、1，均满足全部约束。A22 取 1、2、3、4、8 时都会导致"
                "重复值或无法满足“一倍关系”，故选 A。"
            ),
        },
    },
    # 2016 下 / 2017 下的这 8 道题在随卷答案详解里只有答案字母、解析为空，
    # 门禁因此长期拦截。补写的是模型解析（正文以【AI 补写】标注），题干、
    # 选项、答案和考点逐字未动，故在此登记批准值。
    "past-papers/comprehensive-by-year/2021.md": {
        (4, 4): {
            # The baseline transcript pasted the answer edge set into the stem,
            # which is the same string as option C: the question leaked its own
            # answer and never carried the precedence graph it references.
            "stem": (
                "前趋图（Precedence Graph） 题图给出了进程 P1–P8 之间的前趋关系"
                "（原卷插图未随转录保留），那么前趋图可记为（ ）。"
            ),
        },
    },
    "past-papers/comprehensive-by-year/2025上.md": {
        (30, 30): {
            "stem": "在数据流图中，描述数据流可以产生 b 数据和 c 数据，但是不能同时产生 b 和 c 数据的是（）符号。",
            "explanation": (
                "⊕ 是异或（XOR）符号，表示互斥选择，两个数据只能择一产生、不能同时成立，"
                "符合题意。＋ 表示多个数据流汇合后共同输入同一处理过程，与互斥无关；"
                "＊ 在数据流图中无明确标准含义（可能表示重复或连接）；"
                "○ 代表数据存储（文件、数据库），与数据流的逻辑关系无关。"
            ),
        },
    },
}


# Existing reviewed repairs: bb3ea99 restores the assignment table (also asserted
# in test_sanitize_bank); b897fb3 restores superscript exponents in directory sizes.
# Keep exact field expectations instead of changing the normalization baseline.
REVIEWED_CONTENT_FIXES.update({'past-papers/comprehensive-by-year/2018下.md': {(69, 69): {'stem': '某企业准备将四个工人甲、乙、丙、丁分配在 A、B、C、D '
                                                                   '四个岗位。每个工人由于技术水平不同，在不同岗位上每天完成任务所需的工时见下表。适当安排岗位，可使四个工人以最短的总工时（69）全部完成每天的任务。\n'
                                                                   '\n'
                                                                   '| 工人 | A | B | C | D |\n'
                                                                   '|---|---:|---:|---:|---:|\n'
                                                                   '| 甲 | 7 | 5 | 2 | 3 |\n'
                                                                   '| 乙 | 9 | 4 | 3 | 7 |\n'
                                                                   '| 丙 | 5 | 4 | 7 | 5 |\n'
                                                                   '| 丁 | 4 | 6 | 5 | 6 |'}},
 'past-papers/comprehensive-by-year/2026上.md': {(30, 30): {'options': [{'label': 'A', 'text': '2¹²⁸'},
                                                                       {'label': 'B', 'text': '2³²'},
                                                                       {'label': 'C', 'text': '2⁶⁴'},
                                                                       {'label': 'D', 'text': '2¹⁶'}]}}})


def load_sanitizer() -> Any:
    spec = importlib.util.spec_from_file_location("sanitize_bank", SANITIZER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载解析器：{SANITIZER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_revision(path: Path, base_ref: str) -> str:
    relative = path.relative_to(REPO_ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"{base_ref}:{relative}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"无法读取基线 {base_ref}:{relative}：{message}")
    return result.stdout


def parse_revision(module: Any, text: str, year: str) -> List[Dict[str, Any]]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / f"{year}.md"
        source.write_text(text, encoding="utf-8")
        return module.parse_paper(source)


def item_key(item: Dict[str, Any]) -> Tuple[int, int]:
    item_range = item.get("range") or []
    if len(item_range) != 2:
        raise ValueError(f"题目缺少稳定题号范围：{item.get('id')}")
    return int(item_range[0]), int(item_range[1])


def retired_by_maintainer(item: Dict[str, Any]) -> bool:
    """True when the maintainer deny-list is an item's only gate failure.

    A reviewed split may deliberately retire one of its blanks through
    ``scripts/quiz_quality_exclusions.json``.  The exclusion has to be the one
    and only reason the child is unusable, so a genuinely broken child cannot
    hide behind a deny-list entry.
    """

    if item.get("quality_status") == "ready":
        return False
    issues = [str(issue) for issue in (item.get("quality_issues") or [])]
    return len(issues) == 1 and issues[0].startswith("excluded:")


def index_items(items: Iterable[Dict[str, Any]]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    indexed: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for item in items:
        key = item_key(item)
        if key in indexed:
            raise ValueError(f"题号范围重复：{key}")
        indexed[key] = item
    return indexed


def compare_paper(module: Any, path: Path, base_ref: str) -> List[str]:
    baseline_text = read_revision(path, base_ref)
    current_text = path.read_text(encoding="utf-8")
    baseline = index_items(parse_revision(module, baseline_text, path.stem))
    current = index_items(module.parse_paper(path))
    issues: List[str] = []

    relative = path.relative_to(REPO_ROOT).as_posix()
    allowed_additions = BASELINE_UNPARSEABLE_ADDITIONS.get(relative, {})
    allowed_polluted_fields = BASELINE_TRAILING_POLLUTION_FIELDS.get(relative, {})
    allowed_asset_fields = BASELINE_RAW_ASSET_FIELD_EXCEPTIONS.get(relative, {})
    reviewed_content_fixes = REVIEWED_CONTENT_FIXES.get(relative, {})
    split_keys = REVIEWED_GROUP_SPLITS.get(relative, set())
    split_additions = {
        (number, number)
        for start, end in split_keys
        for number in range(start, end + 1)
    }
    unexpected_current = (
        set(current) - set(baseline) - set(allowed_additions) - split_additions
    )
    missing_current = set(baseline) - set(current)
    if unexpected_current or missing_current:
        issues.append(
            f"{relative}：题号范围不一致，"
            f"基线={sorted(baseline)}，当前={sorted(current)}"
        )
        return issues
    for start, end in sorted(split_keys):
        for number in range(start, end + 1):
            child = current.get((number, number))
            if child is None or (
                child.get("quality_status") != "ready"
                and not retired_by_maintainer(child)
            ):
                issues.append(f"{relative}#{number}-{number}：拆分出的子题未通过门禁")
        baseline_block = _block_text(baseline_text, start, end)
        region = "\n".join(
            [_block_text(current_text, start, end)]
            + [_block_text(current_text, number, number) for number in range(start, end + 1)]
        )
        for line in _preserved_lines(baseline_block):
            if line not in region:
                issues.append(
                    f"{relative}#{start}-{end}：拆分后遗失内容 {line[:40]!r}"
                )
                break
    for key, reason in allowed_additions.items():
        item = current.get(key)
        if item is None:
            continue
        if item.get("quality_status") == "ready":
            issues.append(
                f"{relative}#{key[0]}-{key[1]}：基线不可解析块意外变为可用题（{reason}）"
            )

    for key in sorted(baseline):
        if key in split_keys:
            # Reviewed split: the carrier keeps the scenario as shared reading
            # material, and the per-blank fields are asserted above.
            continue
        for field in FIELDS:
            if baseline[key].get(field) != current[key].get(field):
                exception = allowed_polluted_fields.get(key, {}).get(field)
                baseline_value = baseline[key].get(field)
                current_value = current[key].get(field)
                if (
                    exception
                    and isinstance(baseline_value, str)
                    and isinstance(current_value, str)
                    and current_value
                    and baseline_value.startswith(current_value)
                ):
                    continue
                asset_exception = allowed_asset_fields.get(key, {}).get(field)
                if asset_exception and isinstance(baseline_value, str) and isinstance(current_value, str):
                    stripped_baseline = re.sub(r"!?\[[^\]]*\]\([^)]+\)", "", baseline_value)
                    normalized_baseline = re.sub(r"\s+", " ", stripped_baseline).strip()
                    normalized_current = re.sub(r"\s+", " ", current_value).strip()
                    if normalized_baseline == normalized_current:
                        continue
                approved_value = reviewed_content_fixes.get(key, {}).get(field)
                if approved_value is not None and current_value == approved_value:
                    continue
                approved_digest = REVIEWED_ITEM_DIGESTS.get(relative, {}).get(key)
                if approved_digest and content_digest(current[key]) == approved_digest:
                    continue
                issues.append(
                    f"{relative}#{key[0]}-{key[1]}：{field} 内容发生变化"
                )
    return issues


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="3b9fa0d",
        help="规范化前的 Git 基线提交（默认：3b9fa0d）",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="只校验指定真题文件（相对于仓库根目录；可重复）",
    )
    parser.add_argument("--check", action="store_true", help="发现内容变化时以非零状态退出")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    paths = (
        [REPO_ROOT / relative for relative in args.paths]
        if args.paths
        else sorted(PAPER_DIR.glob("*.md"))
    )
    missing = [path for path in paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"文件不存在：{path}", file=sys.stderr)
        return 2

    module = load_sanitizer()
    issues: List[str] = []
    for path in paths:
        try:
            issues.extend(compare_paper(module, path, args.base_ref))
        except (RuntimeError, ValueError) as error:
            issues.append(str(error))

    if issues:
        print("题库规范化内容校验失败：", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1 if args.check else 0

    print(f"题库规范化内容校验通过：{len(paths)} 个文件、字段 {', '.join(FIELDS)} 均未变化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
