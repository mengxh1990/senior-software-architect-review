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
import importlib.util
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
# These source fragments were present in the baseline Markdown but could not
# produce a parser item because their scan lost required options/table text.
# Normalising their headers makes them visible to the quality gate; they must
# remain invalid and are never admitted to the quiz pool.  Keep this allowlist
# exact so a newly introduced usable question still fails the lossless check.
BASELINE_UNPARSEABLE_ADDITIONS = {
    "past-papers/comprehensive-by-year/2012下.md": {
        (69, 69): "baseline_missing_required_options",
    },
    "past-papers/comprehensive-by-year/2013下.md": {
        (47, 51): "baseline_html_table_option_set_unparseable",
    },
}
# The old transcript parser consumed following blocks into this explanation.
# The canonical split must remove only that trailing parser pollution; require
# the corrected text to be a non-empty prefix of the baseline value.
BASELINE_TRAILING_POLLUTION_FIELDS = {
    "past-papers/comprehensive-by-year/2013下.md": {
        (45, 46): {"explanation": "baseline_consumed_following_question_blocks"},
    },
    "past-papers/comprehensive-by-year/2011下.md": {
        (1, 1): {"explanation": "baseline_consumed_following_figure_intro"},
        (70, 70): {"explanation": "baseline_consumed_following_english_passage"},
    },
}
# The source image remains in canonical Markdown, but the strict curated
# parser intentionally keeps raw asset paths out of learner-facing text.
BASELINE_RAW_ASSET_FIELD_EXCEPTIONS = {
    "past-papers/comprehensive-by-year/2011下.md": {
        (2, 4): {"stem": "baseline_embedded_raw_figure_path"},
        (69, 69): {"stem": "baseline_embedded_raw_figure_path"},
    },
}
# A reviewed group split rewrites one multi-blank block into a carrier plus one
# item per blank.  The carrier keeps the shared scenario and every child gets
# its own option bank and answer, so no exam text is invented or lost.  The
# check below still proves line-level preservation against the baseline.
REVIEWED_GROUP_SPLITS = {
    "past-papers/comprehensive-by-year/2009下.md": {(57, 59), (71, 75)},
    "past-papers/comprehensive-by-year/2010下.md": {(55, 57), (71, 75)},
    "past-papers/comprehensive-by-year/2011下.md": {(71, 75),},
    "past-papers/comprehensive-by-year/2012下.md": {(44, 48), (56, 61), (71, 75)},
    "past-papers/comprehensive-by-year/2013下.md": {
        (40, 42),
        (52, 56),
        (57, 63),
        (71, 75),
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
            if child is None or child.get("quality_status") != "ready":
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
