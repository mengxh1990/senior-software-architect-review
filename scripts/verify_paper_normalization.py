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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = REPO_ROOT / "past-papers" / "comprehensive-by-year"
SANITIZER_PATH = REPO_ROOT / "scripts" / "sanitize_bank.py"
FIELDS = ("stem", "options", "correct", "explanation")


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
    baseline = index_items(parse_revision(module, read_revision(path, base_ref), path.stem))
    current = index_items(module.parse_paper(path))
    issues: List[str] = []

    if baseline.keys() != current.keys():
        issues.append(
            f"{path.relative_to(REPO_ROOT)}：题号范围不一致，"
            f"基线={sorted(baseline)}，当前={sorted(current)}"
        )
        return issues

    for key in sorted(baseline):
        for field in FIELDS:
            if baseline[key].get(field) != current[key].get(field):
                issues.append(
                    f"{path.relative_to(REPO_ROOT)}#{key[0]}-{key[1]}：{field} 内容发生变化"
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
