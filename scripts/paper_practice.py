#!/usr/bin/env python3
"""Serve 案例分析 / 论文 past papers for practice, with answers held back.

案例分析 and 论文 training used to run only on the self-authored playbooks
(``case-types/`` / ``paper-topics/``). This script wires the real papers in
``past-papers/case-by-year/`` and ``past-papers/essay-by-year/`` into the same
kind of flow ``sanitize_bank.py`` gives the objective questions:

* pick by 题型/主题 tag, by year, or list what is available;
* for 案例, split the question from its 参考答案 and only hand the answer over
  when ``--reveal`` is passed (the learner writes first);
* skip questions whose figures were removed by default;
  ``--allow-missing-figures`` requires the learner to explicitly accept missing materials;
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
import hashlib
import re
import sys
from pathlib import Path
from typing import Sequence

import knowledge_taxonomy
import sanitize_bank

REPO_ROOT = Path(__file__).resolve().parents[1]
TAGGER_PATH = REPO_ROOT / "scripts" / "tag_case_essay.py"

ANSWER_MARKER_RE = re.compile(
    r"【\s*问题\s*\d+\s*解析\s*】|^#{0,4}[ \t]*参考答案[:：]?|^#{0,4}[ \t]*答案\s*[/／]\s*答案解析"
    r"|^#{0,4}[ \t]*答案解析[:：]?|^#{0,4}[ \t]*答案[:：]\s*$",
    re.MULTILINE,
)
# 注意：裸的「【解析】」不能当切分点——在 2009–2018 的答案详解转录版里，
# 它是"问题→答案→解析"结构中的解析标记，用它切分会把答案留在题干里。
QUESTION_BLOCK_RE = re.compile(r"^#{2,4}[ \t]*【?\s*问题\s*\d+(?!\d)(?![ \t]*解析)\s*】?[^\n]*$", re.MULTILINE)
TAG_LINE_RE = re.compile(r"^>[ \t]*\*\*(?:题型|主题)\*\*[:：][^\n]*$", re.MULTILINE)
APPENDIX_DIVIDER_RE = re.compile(r"^#{1,3}[ \t]*参考答案[^\n]*$", re.MULTILINE)
IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
REMOVED_FIGURE_MARK = "原图含机构广告或水印，已移除"
MISSING_FIGURE_NOTE = "原题必要插图缺失，默认不出题；明确接受缺图练习时请对照权威原卷，不得补造图意"
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
    body = re.sub(r"\*\*(参考答案|答案解析|答案)\*\*\s*[:：]", r"\1：", body)
    marker = ANSWER_MARKER_RE.search(body)
    if marker:
        stem = body[: marker.start()]
        answer = body[marker.end() :]
        if len(stem.strip()) > 5 and len(answer.strip()) > 5:
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
    body = re.sub(r"\*\*(参考答案|答案解析|答案)\*\*\s*[:：]", r"\1：", body)
    if is_answer_key(body):
        return "answer_key", None, body.strip()
    markers = list(ANSWER_MARKER_RE.finditer(body))
    if len(markers) == 1 and "参考答案" in markers[0].group(0):
        split = split_question_from_answer(body)
        if split:
            return "blind", split[0].strip(), split[1].strip()
    headings = list(QUESTION_BLOCK_RE.finditer(body))
    if headings:
        stems = [body[:headings[0].start()].strip()]
        answers = []
        for index, heading in enumerate(headings):
            end = headings[index+1].start() if index+1 < len(headings) else len(body)
            split = split_question_from_answer(body[heading.end():end])
            if split is None:
                break
            stems.append(heading.group(0) + "\n" + split[0].strip())
            answers.append(heading.group(0) + "\n" + split[1].strip())
        else:
            return "blind", "\n\n".join(stems).strip(), "\n\n".join(answers).strip()
        # A single answer section after all questions is also a safe boundary.
        marker = ANSWER_MARKER_RE.search(body)
        if marker and marker.start() < headings[-1].start():
            return "read_only", body.strip(), None
    split = split_question_from_answer(body)
    if split:
        return "blind", split[0].strip(), split[1].strip()
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


def assess_materials(item: dict) -> dict:
    """Blind separation, complete material and topic mapping are distinct gates."""
    stem = item.get("stem", "")
    issues = []
    if item.get("practice_mode") != "blind":
        issues.append("not_blind")
    if re.search(r"暂缺|待补(?:充|齐)?|尚未还原|未还原|不得据以出题|选项内容[.。]空|题干[、与及].{0,15}缺失", stem):
        issues.append("incomplete_question")
    if re.search(r"[…]{2,}|\.{4,}", stem):
        issues.append("incomplete_source_text")
    if item.get("missing_figure") and (item["subject"] == "case" or re.search(r"如图|见图|下图|图中|下表", stem)):
        issues.append("missing_required_figure")
    root = REPO_ROOT / "past-papers" / "assets" / item["year"]
    if any(not (root / name).is_file() for name in item.get("figures", [])):
        issues.append("missing_figure_asset")
    if not item.get("figures") and re.search(r"如图|见图|图\s*\d+[-－]\d+|在图中|填写图中", stem):
        issues.append("missing_required_figure")
    if re.search(r"(?:表\s*\d+|下表|表中).{0,35}(?:空白|填|所示)|填.{0,20}表\s*\d+", stem) and not sanitize_bank.MARKDOWN_TABLE_RE.search(stem) and not item.get("figures"):
        issues.append("missing_required_table")
    if item["subject"] == "case":
        question_headers = re.findall(r"^[ \t]*(?:#{1,4}[ \t]*)?(?:【)?问题[ \t]*\d+(?!\d)(?![ \t]*解析)[^\n]*", stem, re.M)
        scores = [re.search(r"[（(](\d+)[ \t]*分", heading) for heading in question_headers]
        if scores and all(scores) and sum(int(score.group(1)) for score in scores) != 25:
            issues.append("incomplete_scoring_material")

        if not item.get("figures") and re.search(r"(?:效用树|架构图|序列图|状态图|活动图|用例图).{0,15}(?:填空|空白|填入)|(?:完成|补充|填写).{0,20}(?:图|效用树)", stem):
            issues.append("missing_required_figure")
    if item["subject"] == "essay" and (len(stem) < 150 or not re.search(r"请围绕|依次|分别|概要|论述|讨论", stem)):
        issues.append("incomplete_essay_prompt")
    if str(item.get("tag", "")).endswith("00"):
        issues.append("unclassified")
    metadata = knowledge_taxonomy.subjective_metadata(item["id"])
    if metadata:
        item = {**item, "topic_id": metadata["topic_id"], "covered_topic_ids": metadata.get("covered_topic_ids", []),
                "subquestions": metadata.get("subquestions", []),
                "canonical_item_id": metadata.get("canonical_item_id", item["id"])}
        if hashlib.sha256(stem.encode()).hexdigest() != metadata.get("content_fingerprint"):
            issues.append("classification_content_changed")
    elif item.get("practice_mode") == "blind":
        issues.append("missing_module_mapping")
    return {**item, "quality_issues": sorted(set(issues)),
            "quality_status": "invalid" if issues else "ready", "complete": not issues}


def eligible(item: dict, *, allow_missing_figures: bool = False) -> bool:
    if item.get("practice_mode") != "blind":
        return False
    issues = set(item.get("quality_issues", []))
    if allow_missing_figures:
        issues -= {"missing_required_figure", "missing_figure_asset"}
    return not issues and (allow_missing_figures or item.get("subject") == "essay" or not item.get("missing_figure"))


def _stem_figures(stem: str, figures: list[str]) -> tuple[str, list[str]]:
    numbers = list(dict.fromkeys(int(n) for n in re.findall(r"【图 (\d+)】", stem)))
    selected = [figures[n-1] for n in numbers if 0 < n <= len(figures)]
    mapping = {old: i+1 for i, old in enumerate(numbers)}
    return re.sub(r"【图 (\d+)】", lambda m: f"【图 {mapping[int(m.group(1))]}】", stem), selected


def build_case_items() -> list[dict]:
    """Every 案例 question with its practice mode and (held back) answer."""
    items: list[dict] = []
    answer_keys: dict[str, dict[str, str]] = {}
    tag_map = tagger.load_tag_map().get("case") or {}
    for path in tagger.iter_papers("case"):
        text = path.read_text(encoding="utf-8")
        verbatim_paper = path.stem.endswith("-原卷")
        base_year = path.stem.split("-原卷")[0] if verbatim_paper else path.stem
        tags = tag_map.get(base_year) or []
        # 整卷唯一的「参考答案与解析」分隔标题属于卷末附录；每道题各有一个
        # 「参考答案」标题的文件（如 2026 上）不能裁，否则会切掉答案标记
        appendix_divider = len(APPENDIX_DIVIDER_RE.findall(text)) == 1
        for index, record in enumerate(tagger.split_question_records(text)):
            numeral, title, body, batch = record["number"], record["title"], record["body"], record["batch"]
            tag = tags[index] if index < len(tags) else {"tag": "", "label": ""}
            if appendix_divider:
                body = APPENDIX_DIVIDER_RE.split(body)[0]
            stem_raw, figures, missing_figure = normalise_body(body)
            mode, stem_text, answer_text = classify(stem_raw)
            if verbatim_paper and mode == "read_only":
                # 原卷（题干与答案天然分离）卷面本身不含答案，可直接盲练
                mode, stem_text, answer_text = "blind", stem_raw, None
            item = {
                "id": f"past-papers/case-by-year/{path.stem}.md#" + (f"批次{batch}-" if batch else "") + f"试题{numeral}",
                "batch": batch,
                "subject": "case",
                "year": base_year,
                "selection_only": bool(re.fullmatch(r"20(?:09|1[0-7])下", base_year)),
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
            item["stem"], item["figures"] = _stem_figures(stem_text or "", figures)
            item["answer"] = answer_text
            if mode == "answer_key":
                item["id"] = f"{item['id']}-答案区"
                answer_keys.setdefault(path.stem, {})[numeral] = answer_text or ""
                item["note"] = "这是卷末的参考答案区，不作为练习题目"
            elif mode == "read_only":
                item["note"] = "题干与参考答案混排且无标记，仅用于研读；盲练请换用 practice_mode=blind 的题"
            elif answer_text is None:
                item["note"] = "该卷面未收录参考答案，作答后按评分点自行估分"
            if verbatim_paper:
                item["answer_source"] = f"past-papers/case-by-year/{path.stem.split('-原卷')[0]}.md#{item['id'].split('#')[1]}"
                item["note"] = "题干取自原卷（无答案）；作答后到 answer_source 指向的研读版文件取参考答案再估分"
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
    return [assess_materials(item) for item in items]


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
            answer = None
            study_note = re.search(r"^#{1,4}\s*回忆稿附带的参考要点", stem, re.M)
            if study_note:
                stem, answer = stem[:study_note.start()].strip(), stem[study_note.end():].strip()
            separated = split_question_from_answer(stem)
            if separated:
                stem, answer = (part.strip() for part in separated)
            looks_like_question = bool(
                re.search(r"请围绕|依次从以下|进行论述", stem) or title.startswith("论")
            )
            items.append(
                {
                    "id": f"past-papers/essay-by-year/{path.stem}.md#试题{numeral}",
                    "subject": "essay",
                    "year": path.stem,
                    "selection_only": bool(re.fullmatch(r"20(?:09|1[0-7])下", path.stem)),
                    "numeral": numeral,
                    "title": title,
                    "tag": tag.get("tag", ""),
                    "tag_label": tag.get("label", ""),
                    "source_type": "recalled_real" if path.stem in RECALL_YEARS else "real",
                    "practice_mode": "blind" if looks_like_question else "read_only",
                    "stem": stem,
                    "figures": figures,
                    "missing_figure": missing_figure,
                    "answer": answer,
                }
            )
            if not looks_like_question:
                items[-1]["note"] = "不是独立题目（多为正文子标题），不作为练习题目"
    return [assess_materials(item) for item in items]


def select(items: list[dict], *, tag: str | None, year: str | None, numeral: str | None,
           blind_only: bool, skip_missing_figures: bool, batch: str | None = None,
           item_id: str | None = None) -> list[dict]:
    chosen = []
    for item in items:
        if item_id and item["id"] != item_id:
            continue
        if batch and item.get("batch") != batch:
            continue
        if tag and not item["tag"].startswith(tag):
            continue
        if year and item["year"] != year:
            continue
        if numeral and item["numeral"] != numeral:
            continue
        if blind_only and not eligible(item, allow_missing_figures=not skip_missing_figures):
            continue
        # An explicit partial-material request never authorizes invented figures.
        if skip_missing_figures and item["subject"] == "case" and item["missing_figure"]:
            continue
        chosen.append(item)
    if year and numeral and not batch and not item_id and len({i.get("batch") for i in chosen}) > 1:
        raise ValueError("该考期题号存在多个批次；请传 --batch 或 --item-id")
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
    parser.add_argument("--batch", help="多批次试卷的批次号，如 1")
    parser.add_argument("--item-id", help="精确题目身份，推荐用于揭示答案")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reveal", action="store_true", help="作答后取参考答案")
    parser.add_argument("--include-readonly", action="store_true")
    parser.add_argument("--allow-missing-figures", action="store_true", help="明确接受缺图题时使用")
    parser.add_argument(
        "--skip-missing-figures",
        action="store_true",
        help="跳过插图被移除的案例题（默认已跳过，保留兼容）",
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
        batch=args.batch,
        item_id=args.item_id,
        blind_only=not args.include_readonly,
        skip_missing_figures=args.skip_missing_figures or not args.allow_missing_figures,
    )
    if args.limit is not None:
        chosen = chosen[: args.limit]
    if not chosen:
        print("没有匹配的真题（检查 --type/--topic/--year/--numeral，或该题缺图/不可盲练）", file=sys.stderr)
        return 1

    for item in chosen:
        if not args.reveal:
            item.pop("answer", None)
        elif not item.get("answer") and item.get("answer_source"):
            reference = next((source for source in items if source["id"] == item["answer_source"]), None)
            if reference:
                item["answer"] = reference.get("answer") or reference.get("stem")
                item["answer_format"] = "reference_source_excerpt"
                item["answer_note"] = "关联研读版原文，可能含题干；按小问提取采分点，不冒充官方唯一措辞"
            else:
                item["answer_note"] = "未找到关联答案材料，不能伪造评分依据"
        item["coach_note"] = (
            "作答前只呈现 stem；【图 N】对应 figures 中的插图，不要贴文件路径；"
            "学员作答后再用 --reveal 取参考答案并按评分点估分"
            if item["practice_mode"] == "blind"
            else "题干与参考答案混排，仅作研读材料，不得用于盲练"
        )
    print(json.dumps(chosen, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
