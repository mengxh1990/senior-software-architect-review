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
    "past-papers/comprehensive-by-year/2009下.md": {
        (7, 8),
        (9, 10),
        (26, 27),
        (28, 29),
        (33, 34),
        (35, 37),
        (51, 52),
        (57, 59),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2010下.md": {
        (3, 4),
        (6, 7),
        (26, 27),
        (29, 30),
        (33, 34),
        (36, 37),
        (42, 43),
        (46, 47),
        (53, 54),
        (55, 57),
        (62, 63),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2011下.md": {
        (18, 19),
        (20, 21),
        (27, 28),
        (29, 30),
        (33, 34),
        (35, 36),
        (44, 45),
        (46, 48),
        (56, 57),
        (58, 60),
        (62, 63),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2012下.md": {
        (1, 2),
        (5, 6),
        (7, 8),
        (19, 20),
        (22, 23),
        (27, 28),
        (29, 30),
        (32, 34),
        (39, 41),
        (42, 43),
        (44, 48),
        (49, 50),
        (51, 53),
        (56, 61),
        (62, 63),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2013下.md": {
        (5, 6),
        (7, 8),
        (16, 17),
        (19, 21),
        (22, 23),
        (29, 30),
        (31, 32),
        (33, 34),
        (35, 36),
        (40, 42),
        (45, 46),
        (47, 51),
        (52, 56),
        (57, 63),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2014下.md": {
        (3, 4),
        (6, 8),
        (10, 11),
        (16, 17),
        (19, 21),
        (22, 23),
        (27, 28),
        (33, 34),
        (35, 36),
        (42, 43),
        (44, 46),
        (47, 48),
        (49, 50),
        (51, 52),
        (54, 59),
        (60, 61),
        (62, 63),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2015下.md": {
        (3, 4),
        (13, 14),
        (18, 19),
        (22, 24),
        (34, 35),
        (38, 39),
        (44, 45),
        (51, 52),
        (53, 55),
        (56, 61),
        (62, 63),
        (67, 68),
        (71, 75),
    },
    "past-papers/comprehensive-by-year/2016下.md": {
        (7, 8),
        (10, 11),
        (16, 17),
        (18, 19),
        (20, 21),
        (27, 28),
        (29, 30),
        (31, 33),
        (36, 37),
        (39, 40),
        (42, 43),
        (45, 46),
        (47, 48),
        (49, 50),
        (54, 57),
        (58, 63),
        (71, 75),
    },
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
    "past-papers/comprehensive-by-year/2017下.md": {
        (1, 2),
        (7, 8),
        (9, 10),
        (16, 17),
        (18, 19),
        (20, 21),
        (26, 27),
        (32, 34),
        (37, 38),
        (39, 40),
        (42, 43),
        (44, 46),
        (48, 50),
        (54, 57),
        (58, 63),
        (69, 70),
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
    # 2016 下 / 2017 下的这 8 道题在随卷答案详解里只有答案字母、解析为空，
    # 门禁因此长期拦截。补写的是模型解析（正文以【AI 补写】标注），题干、
    # 选项、答案和考点逐字未动，故在此登记批准值。
    "past-papers/comprehensive-by-year/2016下.md": {
        (26, 26): {
            "explanation": (
                "【AI 补写】螺旋模型是生命周期模型与原型模型的结合，它在原型模型（快速原型）的"
                "基础上扩展而成，并把整个开发流程划分为多个螺旋周期，每个周期都由目标设定、"
                "风险分析、开发和有效性验证、评审四部分组成。瀑布模型只提供了阶段间顺序推进的"
                "思路，本身并不包含原型迭代与风险分析机制，不能作为螺旋模型的扩展基础；"
                "快速模型、面向对象模型也不是过程模型的扩展基础。"
            ),
        },
    },
    "past-papers/comprehensive-by-year/2017下.md": {
        (24, 24): {
            "explanation": (
                "【AI 补写】本题考查需求陈述的基本要求。需求陈述要求每一项需求完整、准确地描述"
                "即将开发的功能，能够在系统及其运行环境的能力和约束条件内实现，并且必须反映"
                "用户真正的需要。需求还应按重要程度区分优先级、区别对待，把全部需求视为同等"
                "重要会使项目失去重点、无法合理分配资源，故该项描述不正确。"
            ),
        },
        (28, 28): {
            "explanation": (
                "【AI 补写】敏捷型方法有两个基本特征：一是“适应性”而非“预设性”，欢迎需求变化；"
                "二是“面向人的”而非“面向过程的”，强调发挥开发人员的创造力。极限编程（XP）是"
                "著名的敏捷开发方法，敏捷方法也都采用迭代增量式的开发方式，B、C、D 三项叙述"
                "均正确。A 项把敏捷方法的思考角度说成“面向开发过程”，与“面向人”的特征相悖，"
                "故为不正确的叙述。"
            ),
        },
        (31, 31): {
            "explanation": (
                "【AI 补写】结构化程序设计的基本思想是自顶向下、逐步求精和模块化，任何单入口"
                "单出口的程序都可以由顺序、分支（选择）和循环三种基本控制结构组合而成。嵌套"
                "只是一种结构的组合方式，跳转会破坏程序单入口单出口的特性，并发也不是程序的"
                "控制结构，故含有这些成分的选项都不成立。"
            ),
        },
        (51, 51): {
            "explanation": (
                "【AI 补写】规则系统风格把业务规则从程序代码中分离出来，以规则库加规则引擎的"
                "方式支持规则的灵活定义和随时修改，业务规则变化时只调整规则库而不改动系统结构。"
                "本题中 VIP 会员的审核标准和折扣标准需要随商场活动不定期变化，正是规则频繁变动"
                "的典型场景，采用规则系统风格最为合适。过程控制、分层和管道-过滤器风格分别关注"
                "流程驱动、层次划分和数据流处理，都无法直接承载可动态调整的业务规则。"
            ),
        },
        (66, 66): {
            "explanation": (
                "【AI 补写】本题考查美术作品原件转让后的权利归属。著作权法规定，美术等作品原件"
                "所有权的转移不视为作品著作权的转移，作品著作权仍由原作者享有；但美术作品原件的"
                "展览权由原件所有人享有。因此王某买入原件后取得的是原件的所有权及其展览权，"
                "而不是作品的著作权。"
            ),
        },
        (67, 67): {
            "explanation": (
                "【AI 补写】本题考查商标注册的申请在先原则及其例外。两个或者两个以上的申请人，"
                "在同一种商品或者类似商品上以相同或者近似的商标申请注册，初步审定并公告申请在先"
                "的商标；同一天申请的，初步审定并公告使用在先的商标；同日使用或者均未使用的，"
                "由各申请人协商，协商不成的由商标局通知申请人自行抽签确定，不愿抽签或者抽签不能"
                "确定的由商标局裁定。本题中两商标近似，且申请日与首次使用日期均相同，无法区分"
                "先后，只能由甲、乙协商或抽签确定归属。"
            ),
        },
        (68, 68): {
            "explanation": (
                "【AI 补写】本题考查《计算机软件保护条例》对侵权复制品提供者与持有者责任的区分。"
                "软件复制品的出版者、制作者不能证明其出版、制作有合法授权的，或者发行者、出租者"
                "不能证明其发行、出租的复制品有合法来源的，应当承担法律责任；而不知道也没有合理"
                "理由应当知道所持软件是侵权复制品的持有人，不承担赔偿责任，只负有停止使用、销毁"
                "该侵权复制品的义务。本题中提供者不能证明其提供的复制品有合法来源，属于应当承担"
                "法律责任的一方，不知情的持有者并不承担赔偿责任。"
            ),
        },
    },
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
