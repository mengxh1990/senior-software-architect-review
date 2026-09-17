#!/usr/bin/env python3
"""Serve 案例分析 / 论文 past papers for practice, with answers held back.

案例分析 and 论文 training used to run only on the self-authored playbooks
(``case-types/`` / ``paper-topics/``). This script wires the real papers in
``past-papers/case-by-year/`` and ``past-papers/essay-by-year/`` into the same
kind of flow ``sanitize_bank.py`` gives the objective questions:

* pick by 题型/主题 tag, by year, or list what is available;
* for 案例, split the question from its 参考答案 and only hand the answer over
  when ``--reveal`` is passed (the learner writes first);
* flag (but still serve) questions whose figures were removed during the
  ad/watermark cleanup, so the coach can describe the figure or point at the
  original PDF; ``--skip-missing-figures`` drops them instead;
* replace ``![](../assets/...)`` links with ``【图 N】`` markers so no file path
  is ever shown to the learner.

Items that cannot be split mechanically (题干与参考答案混排，多见于 2009–2018
的答案详解转录版) are reported as ``practice_mode: read_only`` and excluded
from blind practice by default: they are fine for 研读, not for 盲练.

Usage::

    python3 scripts/paper_practice.py --subject case --type 01 --limit 2
    python3 scripts/paper_practice.py --subject case --year 2013下 --numeral 一 --reveal
    python3 scripts/paper_practice.py --subject essay --topic 06 --limit 4
    python3 scripts/paper_practice.py --subject case --list
    python3 scripts/paper_practice.py --subject case --type 08 --skip-missing-figures
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
TAGGER_PATH = REPO_ROOT / "scripts" / "tag_case_essay.py"

ANSWER_MARKER_RE = re.compile(
    r"【\s*问题\s*\d+\s*解析\s*】|^#{0,4}[ \t]*参考答案[:：]?|^#{0,4}[ \t]*答案\s*[/／]\s*答案解析"
    r"|^#{0,4}[ \t]*答案解析[:：]?|^#{0,4}[ \t]*答案[:：]\s*$",
    re.MULTILINE,
)
# 注意：裸的「【解析】」不能当切分点——在 2009–2018 的答案详解转录版里，
# 它是"问题→答案→解析"结构中的解析标记，用它切分会把答案留在题干里。
QUESTION_BLOCK_RE = re.compile(r"^#{2,4}[ \t]*【?\s*问题\s*\d+\s*】?[^\n]*$", re.MULTILINE)
TAG_LINE_RE = re.compile(r"^>[ \t]*\*\*(?:题型|主题)\*\*[:：][^\n]*$", re.MULTILINE)
IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
REMOVED_FIGURE_MARK = "原图含机构广告或水印，已移除"
MISSING_FIGURE_NOTE = "原题含插图，该图在广告/水印清理时被移除；出题时请用文字描述图意，或提示学员对照原始 PDF"
RECALL_YEARS = {"2023下", "2024上", "2024下", "2025上", "2025下", "2026上"}


def _load_tagger():
    spec = importlib.util.spec_from_file_location("tag_case_essay", TAGGER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tag_case_essay"] = module
    spec.loader.exec_module(module)
    return module


tagger = _load_tagger()


def split_question_from_answer(body: str) -> tuple[str, str] | None:
    """Return ``(stem, answer)`` when the answer boundary can be trusted."""
    marker = ANSWER_MARKER_RE.search(body)
    if marker:
        stem = body[: marker.start()]
        answer = body[marker.end() :]
        if len(stem.strip()) > 20 and len(answer.strip()) > 20:
            return stem, answer
    return None


ANSWER_FIRST_RE = re.compile(r"参考答案|答案解析|答案\s*[/／]\s*答案解析|^#{0,4}[ \t]*答案[:：]")


def is_answer_key(body: str) -> bool:
    """True when the block *is* the answer key (2022 的附录、部分回忆版)."""
    text = body.strip()
    marker = ANSWER_FIRST_RE.search(text)
    if not marker:
        return False
    # 答案标记必须紧跟在标题之后（前面没有实质题干），否则就是"题干→答案"的正常版式
    return marker.start() < 40


def classify(body: str) -> tuple[str, str | None, str | None]:
    """Return ``(mode, stem, answer)`` for one 试题 body.

    保守策略：只有"答案边界有明确标记"或"卷面确认不含答案"才允许盲练。
    2009–2018 的答案详解转录版、2020、2024 下、2025 下等把答案直接接在
    题干后面且没有标记，一律标为研读材料，避免把参考答案当题干发出去。
    """
    if is_answer_key(body):
        return "answer_key", None, body.strip()
    # 一道试题可能含多个小问，逐个切分后再拼回去，避免只切到第一个小问
    blocks = QUESTION_BLOCK_RE.split(body)
    if len(blocks) > 1:
        stems: list[str] = [blocks[0].strip()]
        answers: list[str] = []
        all_split = True
        for block in blocks[1:]:
            heading = block.split("\n", 1)[0].strip()
            split = split_question_from_answer(block)
            if split is None:
                # 只要有一个小问切不开，就不能保证题干里没有混入答案
                all_split = False
                break
            stem_part, answer_part = split
            stems.append(f"### {heading}\n{stem_part.strip()}" if heading else stem_part.strip())
            answers.append(f"### {heading}\n{answer_part.strip()}" if heading else answer_part.strip())
        if all_split and answers:
            stem_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in stems if p)).strip()
            answer_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(answers)).strip()
            return "blind", stem_text, answer_text
    split = split_question_from_answer(body)
    if split:
        stem, answer = split
        return "blind", re.sub(r"\n{3,}", "\n\n", stem).strip(), re.sub(r"\n{3,}", "\n\n", answer).strip()
    return "read_only", body.strip(), None


def normalise_body(body: str) -> tuple[str, list[str], bool]:
    """Strip the tag line, extract figure paths, flag removed figures."""
    text = TAG_LINE_RE.sub("", body)
    figures = [Path(target).name for target in IMAGE_LINK_RE.findall(text)]
    counter = {"value": 0}

    def replace(match: re.Match[str]) -> str:
        counter["value"] += 1
        return f"【图 {counter['value']}】"

    text = IMAGE_LINK_RE.sub(replace, text)
    missing_figure = REMOVED_FIGURE_MARK in text
    text = text.replace(f"*（{REMOVED_FIGURE_MARK}）*", "").replace(REMOVED_FIGURE_MARK, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip(), figures, missing_figure


def build_case_items() -> list[dict]:
    """Every 案例 question with its practice mode and (held back) answer."""
    items: list[dict] = []
    answer_keys: dict[str, dict[str, str]] = {}
    tag_map = tagger.load_tag_map().get("case") or {}
    for path in tagger.iter_papers("case"):
        text = path.read_text(encoding="utf-8")
        tags = tag_map.get(path.stem) or []
        for index, (numeral, title, body) in enumerate(tagger.split_questions(text)):
            tag = tags[index] if index < len(tags) else {"tag": "", "label": ""}
            stem_raw, figures, missing_figure = normalise_body(body)
            mode, stem_text, answer_text = classify(stem_raw)
            item = {
                "id": f"past-papers/case-by-year/{path.stem}.md#试题{numeral}",
                "subject": "case",
                "year": path.stem,
                "numeral": numeral,
                "title": title,
                "tag": tag.get("tag", ""),
                "tag_label": tag.get("label", ""),
                "source_type": "recalled_real" if path.stem in RECALL_YEARS else "real",
                "figures": figures,
                "missing_figure": missing_figure,
                "questions": len(QUESTION_BLOCK_RE.findall(stem_raw)) or 1,
            }
            if missing_figure:
                item["figure_note"] = MISSING_FIGURE_NOTE
            item["practice_mode"] = mode
            item["stem"] = stem_text or ""
            item["answer"] = answer_text
            if mode == "answer_key":
                item["id"] = f"{item['id']}-答案区"
                answer_keys.setdefault(path.stem, {})[numeral] = answer_text or ""
                item["note"] = "这是卷末的参考答案区，不作为练习题目"
            elif mode == "read_only":
                item["note"] = "题干与参考答案混排且无标记，仅用于研读；盲练请换用 practice_mode=blind 的题"
            elif answer_text is None:
                item["note"] = "该卷面未收录参考答案，作答后按评分点自行估分"
            items.append(item)

    # 同一份卷子里有独立答案区时（2022 原卷 + 附录），说明同号试题的正文
    # 就是不含答案的原卷题干：放出来盲练，并把答案区的内容挂回去
    for item in items:
        if item["practice_mode"] == "answer_key":
            continue
        key = answer_keys.get(item["year"], {}).get(item["numeral"])
        if not key:
            continue
        if item["practice_mode"] == "read_only":
            item["practice_mode"] = "blind"
            item.pop("note", None)
        item["answer"] = key
        item["note"] = "参考答案取自同卷的答案区"
    return items


def practice_items(items: list[dict]) -> list[dict]:
    """Only the items that may be served to a learner."""
    return [item for item in items if item["practice_mode"] in {"blind", "read_only"}]


def build_essay_items() -> list[dict]:
    """Every 论文 question (题干 + 小问, no answers in the source)."""
    items: list[dict] = []
    tag_map = tagger.load_tag_map().get("essay") or {}
    for path in tagger.iter_papers("essay"):
        text = path.read_text(encoding="utf-8")
        tags = tag_map.get(path.stem) or []
        for index, (numeral, title, body) in enumerate(tagger.split_questions(text)):
            tag = tags[index] if index < len(tags) else {"tag": "", "label": ""}
            stem, figures, missing_figure = normalise_body(body)
            looks_like_question = bool(
                re.search(r"请围绕|依次从以下|进行论述", stem) or title.startswith("论")
            )
            items.append(
                {
                    "id": f"past-papers/essay-by-year/{path.stem}.md#试题{numeral}",
                    "subject": "essay",
                    "year": path.stem,
                    "numeral": numeral,
                    "title": title,
                    "tag": tag.get("tag", ""),
                    "tag_label": tag.get("label", ""),
                    "source_type": "recalled_real" if path.stem in RECALL_YEARS else "real",
                    "practice_mode": "blind" if looks_like_question else "read_only",
                    "stem": stem,
                    "figures": figures,
                    "missing_figure": missing_figure,
                    "answer": None,
                }
            )
            if not looks_like_question:
                items[-1]["note"] = "不是独立题目（多为正文子标题），不作为练习题目"
    return items


def select(items: list[dict], *, tag: str | None, year: str | None, numeral: str | None,
           blind_only: bool, skip_missing_figures: bool) -> list[dict]:
    chosen = []
    for item in items:
        if tag and not item["tag"].startswith(tag):
            continue
        if year and item["year"] != year:
            continue
        if numeral and item["numeral"] != numeral:
            continue
        if blind_only and item["practice_mode"] != "blind":
            continue
        # 缺图默认照常出题（教练用文字描述图意），需要完整插图时才跳过
        if skip_missing_figures and item["subject"] == "case" and item["missing_figure"]:
            continue
        chosen.append(item)
    return chosen


def report(subject: str, items: list[dict]) -> None:
    pool = practice_items(items)
    blind = [i for i in pool if i["practice_mode"] == "blind"]
    missing = [i for i in blind if i["subject"] == "case" and i["missing_figure"]]
    readonly = [i for i in pool if i["practice_mode"] == "read_only"]
    answer_keys = [i for i in items if i["practice_mode"] == "answer_key"]
    print(f"===== {subject} =====")
    print(
        f"  可盲练 {len(blind)} 道（其中缺图 {len(missing)} 道，出题时需文字描述图意）"
        f" | 仅研读 {len(readonly)} 道"
        f" | 答案区 {len(answer_keys)} 段 | 合计 {len(items)} 道"
    )
    by_tag: dict[str, int] = {}
    for item in blind:
        by_tag[item["tag"]] = by_tag.get(item["tag"], 0) + 1
    for tag, count in sorted(by_tag.items()):
        label = next((i["tag_label"] for i in blind if i["tag"] == tag), "")
        print(f"    {tag} {label}：{count} 道")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", choices=("case", "essay"), default="case")
    parser.add_argument("--type", dest="tag", default=None, help="案例题型，如 案例 01 / 01")
    parser.add_argument("--topic", dest="topic", default=None, help="论文主题，如 论文 06 / 06")
    parser.add_argument("--year", default=None)
    parser.add_argument("--numeral", default=None, help="试题号，如 一")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reveal", action="store_true", help="作答后取参考答案")
    parser.add_argument("--include-readonly", action="store_true")
    parser.add_argument(
        "--skip-missing-figures",
        action="store_true",
        help="跳过插图被移除的案例题（默认照常出题，仅给出 figure_note 提示）",
    )
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    items = build_case_items() if args.subject == "case" else build_essay_items()

    if args.list:
        for subject in ("case", "essay"):
            report(subject, build_case_items() if subject == "case" else build_essay_items())
        return 0

    tag = args.topic if args.subject == "essay" else args.tag
    if tag and not tag.startswith(("案例", "论文")):
        tag = f"{'论文' if args.subject == 'essay' else '案例'} {tag}"
    chosen = select(
        practice_items(items),
        tag=tag,
        year=args.year,
        numeral=args.numeral,
        blind_only=not args.include_readonly,
        skip_missing_figures=args.skip_missing_figures,
    )
    if args.limit is not None:
        chosen = chosen[: args.limit]
    if not chosen:
        print("没有匹配的真题（检查 --type/--topic/--year/--numeral，或该题缺图/不可盲练）", file=sys.stderr)
        return 1

    for item in chosen:
        if not args.reveal:
            item.pop("answer", None)
        item["coach_note"] = (
            "作答前只呈现 stem；【图 N】对应 figures 中的插图，不要贴文件路径；"
            "学员作答后再用 --reveal 取参考答案并按评分点估分"
            if item["practice_mode"] == "blind"
            else "题干与参考答案混排，仅作研读材料，不得用于盲练"
        )
    print(json.dumps(chosen, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
