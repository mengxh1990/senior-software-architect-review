#!/usr/bin/env python3
"""Import LAS-parsed past papers into the repo's Markdown layout.

``byted-las-document-parse`` converts the scanned papers into Markdown. This
script performs the deterministic part of the import:

* strip watermark / advertising / page-number boilerplate,
* split one parsed document into 综合知识 / 案例分析 / 论文 sections,
* drop advertiser logos and repeated stamp images while keeping exam figures,
* write ``past-papers/<dimension>-by-year/<label>.md`` with a source header,
* copy kept figures into ``past-papers/assets/<label>/`` and rewrite links.

Exam text (stems, options, answers, explanations) is copied verbatim from the
parser output. The importer never paraphrases or "fixes" content, so anything
that reaches the repo is traceable to the source PDF.

Run order matters: this script rewrites the 综合知识 transcripts from scratch,
so after a re-import run ``python3 scripts/tag_comprehensive_questions.py
--apply`` to restore the §N topic tags.

Usage::

    python3 scripts/import_las_papers.py \
        --las-root /tmp/las_batch/out \
        --manifest scripts/las_import_manifest.json \
        [--only 2009_答案详解] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAS_ROOT = Path("/tmp/las_batch/out")
DEFAULT_MANIFEST = REPO_ROOT / "scripts" / "las_import_manifest.json"

# Lines that are pure boilerplate in the purchased scans. Patterns are matched
# against a stripped line and must not swallow exam content.
WATERMARK_LINE_PATTERNS: tuple[str, ...] = (
    r"^软考达人\s*[-–—]?\s*高效提分的软考题库$",
    r"^软考达人$",
    r"^[-–—\s]*软考达人\s*$",
    r"^ruankaodaren\.com$",
    r"^软考达人\s*ruankaodaren\.com$",
    r"^微信搜一搜.*$",
    r"^大软考题库.*$",
    r"^.*免费题库.*$",
    r"^.*备考题库.*$",
    r"^扫描全能王\s*.*$",
    r"^CamScanner\s*.*$",
    r"^第\s*\d+\s*页\s*(,|，|/|\()?\s*共\s*\d+\s*页\s*\)?$",
    r"^\d{4}\s*年.*试卷\s*第\s*\d+\s*页.*$",
    r"^\d{4}\s*年?.*第\s*\d+\s*页.*$",
    r"^==+\s*\d+\s*==+$",
    r"^\d+\s*/\s*\d+$",
)

IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
IMAGE_OUTPUT_SUFFIX = ".webp"
HTML_WRAPPER_RE = re.compile(r"^\s*<(p|div|span)\b[^>]*>(.*?)</\1>\s*$", re.IGNORECASE | re.DOTALL)
CASE_QUESTION_LINE_RE = re.compile(r"^【\s*问题\s*([0-9０-９]+)\s*】\s*(.*)$")
HEADING_CANDIDATE_RE = re.compile(r"^(?:#{1,4}\s*)?试题\s*([一二三四五六])\s*[:：]?\s*(.{0,30})$")
SENTENCE_PUNCTUATION = "。？！，、；：“”‘’…—"
TITLE_STOP_CHARS = set("是由中的和与为在可需应必要将会不有从对及或均都指属包含以其若如则即并又且则")
CASE_NUMBERED_START_RE = re.compile(r"^\s*(\d{1,2})\s*、\s*阅读")
CASE_PLAIN_START_RE = re.compile(r"^(\d{1,2})\s*、\s*(阅读.{0,80})$")
CASE_EXPLANATION_RE = re.compile(r"^【\s*问题\s*([0-9０-９]+)\s*解析\s*】\s*(.*)$")
NUMERALS = "一二三四五六七八九十"
NUMBERED_HEADING_RE = re.compile(r"^#{1,3}\s*([1-9])[.、．]\s*(.{2,60})$")
NUMBERED_HEADING_SKIP_WORDS = ("摘要", "正文", "要点", "解析", "说明", "评分")
RECALL_HEADINGS = {
    "综合知识": "comprehensive",
    "案例分析": "case",
    "论文写作": "essay",
}


@dataclass
class ImageDecision:
    """Keep/drop verdict for one extracted figure."""

    name: str
    width: int
    height: int
    keep: bool
    reason: str = ""


@dataclass
class SplitResult:
    """Sections detected in one parsed document."""

    comprehensive: str = ""
    case: str = ""
    essay: str = ""
    markers: dict[str, str] = field(default_factory=dict)


def is_watermark_line(line: str) -> bool:
    """Return True when ``line`` is scan boilerplate rather than exam content."""
    stripped = line.strip()
    if not stripped:
        return False
    if any(re.match(pattern, stripped) for pattern in WATERMARK_LINE_PATTERNS):
        return True
    wrapper = HTML_WRAPPER_RE.match(stripped)
    if wrapper:
        inner = re.sub(r"<[^>]+>", "", wrapper.group(2)).strip()
        return bool(inner) and any(re.match(pattern, inner) for pattern in WATERMARK_LINE_PATTERNS)
    return False


def strip_boilerplate(text: str) -> str:
    """Remove watermark/ad/page-number lines and collapse blank runs."""
    kept: list[str] = []
    for raw in text.splitlines():
        if is_watermark_line(raw):
            continue
        kept.append(raw.rstrip())
    out: list[str] = []
    blank = 0
    for line in kept:
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _first_index(lines: Sequence[str], predicate) -> int | None:
    for idx, line in enumerate(lines):
        if predicate(line.strip()):
            return idx
    return None


def split_sections(text: str) -> SplitResult:
    """Split a parsed paper into the three exam dimensions.

    Handles both layouts shipped by LAS:

    * recall documents with ``# 综合知识`` / ``# 案例分析`` / ``# 论文写作``,
    * answer books where 综合 runs first, 案例 starts at a bare ``试题一`` line
      and 论文 starts at a heading such as ``# 试题一 论基于 DSSA ...``.
    """
    lines = text.splitlines()
    recall_positions: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        dimension = RECALL_HEADINGS.get(title)
        if dimension:
            recall_positions.append((idx, dimension))
    if recall_positions:
        result = SplitResult()
        result.markers = {dim: "recall-heading" for _, dim in recall_positions}
        for position, (idx, dimension) in enumerate(recall_positions):
            end = recall_positions[position + 1][0] if position + 1 < len(recall_positions) else len(lines)
            body = "\n".join(lines[idx + 1 : end]).strip("\n")
            setattr(result, dimension, body)
        return result

    headings = heading_indices(lines)
    case_start = headings[0][0] if headings else None
    # Answer books restart the numbering for the essay section, so the second
    # "试题一" marker is where 论文 begins.
    first_questions = [idx for idx, numeral, _ in headings if numeral == "一"]
    essay_start = first_questions[1] if len(first_questions) >= 2 else None
    if case_start is None:
        numbered = _first_index(lines, lambda line: bool(CASE_NUMBERED_START_RE.match(line)))
        if numbered is not None:
            result = SplitResult()
            result.comprehensive = "\n".join(lines[:numbered]).strip("\n")
            result.case = "\n".join(lines[numbered:]).strip("\n")
            result.markers = {"comprehensive": "preamble", "case": "numbered-阅读"}
            return result

    result = SplitResult()
    if case_start is None and essay_start is None:
        result.comprehensive = text.strip("\n")
        result.markers["comprehensive"] = "whole-document"
        return result
    if case_start is None:
        result.comprehensive = "\n".join(lines[:essay_start]).strip("\n")
        result.essay = "\n".join(lines[essay_start:]).strip("\n")
        result.markers = {"comprehensive": "answer-book", "essay": "heading"}
        return result
    result.comprehensive = "\n".join(lines[:case_start]).strip("\n")
    if essay_start is None:
        result.case = "\n".join(lines[case_start:]).strip("\n")
        result.markers = {"comprehensive": "answer-book", "case": "bare-试题"}
    else:
        result.case = "\n".join(lines[case_start:essay_start]).strip("\n")
        result.essay = "\n".join(lines[essay_start:]).strip("\n")
        result.markers = {"comprehensive": "answer-book", "case": "bare-试题", "essay": "heading"}
    return result


def classify_image(width: int, height: int, duplicate_count: int = 1) -> ImageDecision:
    """Decide whether an extracted image is exam content or scan decoration."""
    if width <= 0 or height <= 0:
        return ImageDecision("", width, height, False, "invalid-size")
    if width < 120 or height < 40:
        return ImageDecision("", width, height, False, "too-small")
    ratio = width / height
    if height <= 110 and width <= 330 and ratio >= 2.2:
        return ImageDecision("", width, height, False, "advertiser-banner")
    if height <= 120 and duplicate_count >= 2:
        return ImageDecision("", width, height, False, "repeated-stamp")
    return ImageDecision("", width, height, True)


def perceptual_signature(path: Path, size: int = 24) -> tuple[float, ...]:
    """Cheap grayscale signature used to spot repeated stamps."""
    from PIL import Image

    with Image.open(path) as image:
        gray = image.convert("L").resize((size, size))
        return tuple(float(v) for v in gray.tobytes())


def signature_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Mean absolute difference between two signatures (0 identical, 255 opposite)."""
    if len(left) != len(right) or not left:
        return 255.0
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def plan_images(
    image_paths: Iterable[Path],
    distance_threshold: float = 6.0,
    extra_drops: dict[str, str] | None = None,
) -> dict[str, ImageDecision]:
    """Classify every extracted image, dropping banners and repeated stamps.

    ``extra_drops`` carries reviewed removals from the manifest: images that
    survived the shape/duplicate rules but still carry a watermark or an
    advertiser's promotion (verified by OCR). Their file name maps to the
    reason that is recorded in the import report.
    """
    from PIL import Image

    paths = [p for p in image_paths if p.exists()]
    sizes: dict[str, tuple[int, int]] = {}
    signatures: dict[str, tuple[float, ...]] = {}
    for path in paths:
        try:
            with Image.open(path) as image:
                sizes[path.name] = image.size
            signatures[path.name] = perceptual_signature(path)
        except Exception:  # pragma: no cover - unreadable figure, keep untouched
            sizes[path.name] = (0, 0)

    duplicates: dict[str, int] = {}
    names = list(sizes)
    for idx, left in enumerate(names):
        if not signatures.get(left):
            continue
        for right in names[idx + 1 :]:
            if not signatures.get(right):
                continue
            if signature_distance(signatures[left], signatures[right]) <= distance_threshold:
                duplicates[left] = duplicates.get(left, 1) + 1
                duplicates[right] = duplicates.get(right, 1) + 1

    decisions: dict[str, ImageDecision] = {}
    for name, (width, height) in sizes.items():
        decision = classify_image(width, height, duplicates.get(name, 1))
        decision.name = name
        decisions[name] = decision
    for name, reason in (extra_drops or {}).items():
        if name in decisions:
            decisions[name].keep = False
            decisions[name].reason = f"reviewed-removal: {reason}"
        else:
            decisions[name] = ImageDecision(name, 0, 0, False, f"reviewed-removal: {reason}")
    return decisions


def extract_image_names(text: str) -> list[str]:
    """Return image file names referenced by a Markdown body, in order."""
    names: list[str] = []
    for match in IMAGE_LINK_RE.finditer(text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://")):
            continue
        name = Path(target).name
        if name not in names:
            names.append(name)
    return names


def parse_heading(line: str) -> tuple[str, str] | None:
    """Parse a ``试题N`` section marker.

    Returns ``(numeral, title)`` when the line really is a section boundary.
    Prose such as ``试题一是必答题，每题15分。`` is rejected so a stray
    sentence never becomes a heading.
    """
    match = HEADING_CANDIDATE_RE.match(line.strip())
    if not match:
        return None
    numeral, title = match.group(1), match.group(2).strip()
    if not title:
        return (numeral, "")
    if any(char in title for char in SENTENCE_PUNCTUATION):
        return None
    if title[0] in TITLE_STOP_CHARS:
        return None
    return (numeral, title)


def heading_indices(lines: Sequence[str]) -> list[tuple[int, str, str]]:
    """Return ``(line index, numeral, title)`` for every section marker."""
    found: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        parsed = parse_heading(line)
        if parsed is not None:
            found.append((idx, parsed[0], parsed[1]))
    return found


def rewrite_image_links(text: str, kept: dict[str, str], noted: dict[str, str] | None = None) -> str:
    """Point Markdown image links at their new repo-relative location.

    Names in ``noted`` are replaced by a visible note instead of being dropped
    silently, because the reader needs to know a figure existed there.
    """
    noted = noted or {}

    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        name = Path(target).name
        replacement = kept.get(name)
        if replacement is None:
            return noted.get(name, "")
        return f"![{name}]({replacement})"

    rewritten = IMAGE_LINK_RE.sub(replace, text)
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten)
    return rewritten


def store_image(source: Path, target: Path) -> None:
    """Copy a figure into the repo as a lossless WebP.

    Scan PNGs are large because of paper texture and a faint print watermark;
    lossless WebP keeps every pixel while cutting the repo footprint to about a
    third. The print watermark is suppressed first when the figure is a
    near-grayscale scan (see :func:`suppress_print_watermark`).
    """
    from PIL import Image

    target = target.with_suffix(IMAGE_OUTPUT_SUFFIX)
    with Image.open(source) as image:
        cleaned = suppress_print_watermark(image)
        cleaned.save(target, "WEBP", lossless=True, method=6)


def colored_pixel_ratio(image, sample: int = 400) -> float:
    """Share of pixels that carry noticeable hue (0.0 = pure grayscale scan)."""
    rgb = image.convert("RGB")
    if max(rgb.size) > sample:
        scale = sample / max(rgb.size)
        rgb = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))))
    data = rgb.tobytes()
    total = len(data) // 3
    colored = 0
    for idx in range(0, len(data), 3):
        r, g, b = data[idx], data[idx + 1], data[idx + 2]
        if max(r, g, b) - min(r, g, b) > 28:
            colored += 1
    return colored / max(1, total)


def suppress_print_watermark(image, hue_gap: int = 14, brightness: int = 130, max_share: float = 0.2):
    """Whiten a light pink print watermark on near-grayscale scan figures.

    Only figures that are essentially grayscale are touched: coloured diagrams
    are returned untouched so no content hue is lost. Within a grayscale figure
    only light pixels with a pink cast are whitened, which leaves dark text,
    line art and dark annotations exactly as scanned.
    """
    from PIL import Image, ImageChops

    rgb = image.convert("RGB")
    if colored_pixel_ratio(rgb) >= 0.12:
        return rgb
    red, green, blue = rgb.split()
    pink = ImageChops.logical_and(
        ImageChops.subtract(red, green).point(lambda v: 255 if v > hue_gap else 0).convert("1", dither=Image.NONE),
        ImageChops.subtract(red, blue).point(lambda v: 255 if v > hue_gap else 0).convert("1", dither=Image.NONE),
    )
    bright = rgb.convert("L").point(lambda v: 255 if v > brightness else 0).convert("1", dither=Image.NONE)
    mask = ImageChops.logical_and(pink, bright).convert("L")
    if sum(1 for value in mask.tobytes() if value) / max(1, rgb.width * rgb.height) > max_share:
        return rgb
    return Image.composite(Image.new("RGB", rgb.size, (255, 255, 255)), rgb, mask)


def normalize_section_headings(body: str, dimension: str) -> str:
    """Promote section markers to Markdown headings for the case/essay files.

    Only unambiguous boundary lines are touched: bare ``试题N`` lines in case
    papers and the ``试题N 标题`` headings in essay papers. Question stems are
    left exactly as parsed.
    """
    if dimension not in {"case", "essay"}:
        return body
    if dimension == "essay":
        body = _normalize_numbered_essay_headings(body)
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        heading = parse_heading(stripped)
        if heading and not stripped.startswith("###"):
            numeral, title = heading
            rendered = f"## 试题{numeral}"
            if title and re.fullmatch(r"[（(].{0,12}[)）]", title):
                rendered = f"{rendered}{title}"  # keep a 分值 suffix as-is
            elif title:
                rendered = f"{rendered}：{title}"
            out.extend(["", rendered, ""])
            continue
        if dimension == "case":
            numbered = CASE_PLAIN_START_RE.match(stripped)
            if numbered:
                ordinal = int(numbered.group(1))
                numeral = NUMERALS[ordinal - 1] if 0 < ordinal <= len(NUMERALS) else str(ordinal)
                out.extend(["", f"## 试题{numeral}", "", numbered.group(2).strip(), ""])
                continue
            explanation = CASE_EXPLANATION_RE.match(stripped)
            if explanation:
                suffix = f" {explanation.group(2).strip()}" if explanation.group(2).strip() else ""
                out.extend(["", f"#### 【问题{explanation.group(1)}解析】{suffix}", ""])
                continue
            question = CASE_QUESTION_LINE_RE.match(stripped)
            if question:
                suffix = f" {question.group(2).strip()}" if question.group(2).strip() else ""
                out.extend(["", f"### 【问题{question.group(1)}】{suffix}", ""])
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def _normalize_numbered_essay_headings(body: str) -> str:
    """Map ``## 1. 论负载均衡…`` style headings onto the 试题N convention.

    Some essay booklets number the four topics instead of using 试题一…四.
    Only the first four plausible topic headings are rewritten, and headings
    such as 摘要 / 正文 / 要点 are left alone.
    """
    existing = len(re.findall(r"^#{1,4}\s*试题\s*[一二三四]", body, flags=re.MULTILINE))
    if existing >= 4:
        return body
    out: list[str] = []
    converted = 0
    for line in body.splitlines():
        stripped = line.strip()
        match = NUMBERED_HEADING_RE.match(stripped)
        if match and converted < 4:
            title = match.group(2).strip()
            if not any(word in title for word in NUMBERED_HEADING_SKIP_WORDS):
                ordinal = converted + 1
                out.extend(["", f"## 试题{NUMERALS[ordinal - 1]}：{title}", ""])
                converted += 1
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def summarize(body: str, dimension: str) -> dict[str, int]:
    """Count the markers used to sanity-check an imported section."""
    anchors = [int(n) for n in re.findall(r"[（(]\s*(\d{1,3})\s*[)）]\s*[A-D][.．、]", body)]
    case_headings = len(re.findall(r"^##\s*试题[一二三四五六]", body, flags=re.MULTILINE))
    essay_headings = len(re.findall(r"^##\s*试题[一二三四]", body, flags=re.MULTILINE))
    stats = {
        "chars": len(body),
        "answers": len(re.findall(r"【\s*答案\s*】", body)),
        "explanations": len(re.findall(r"【\s*解析\s*】", body)),
        "questions": len(re.findall(r"【\s*问题\s*[0-9０-９]+\s*】", body)),
        "cases": case_headings if dimension == "case" else 0,
        "essays": essay_headings if dimension == "essay" else 0,
        "images": len(IMAGE_LINK_RE.findall(body)),
        "max_blank_no": max(anchors) if anchors else 0,
    }
    return stats


def render_header(meta: dict, dimension: str, title: str, subtitle: str) -> str:
    """Build the source header that every imported file carries."""
    lines = [f"# {title}", "", f"> {subtitle}", ">", f"> **来源**：{meta['source']}",
             f"> **来源类型**：`{meta['source_type']}`", f"> **解析方式**：{meta['parse_note']}",
             f"> **答案与解析**：{meta['answer_note']}", f"> **使用建议**：{meta['usage']}"]
    if meta.get("coverage_note"):
        lines.append(f"> **完整性**：{meta['coverage_note']}")
    lines.extend(["", ""])
    return "\n".join(lines)


@dataclass
class ParsedDocument:
    """One parsed LAS document, ready to be sliced into repo files."""

    las_key: str
    text: str
    sections: SplitResult
    decisions: dict[str, ImageDecision]
    images_dir: Path


def load_document(
    las_root: Path, las_key: str, extra_drops: dict[str, str] | None = None
) -> ParsedDocument | None:
    """Read a parsed LAS document and classify its figures."""
    source_dir = las_root / las_key
    result_md = source_dir / "result.md"
    if not result_md.exists():
        return None
    text = strip_boilerplate(result_md.read_text(encoding="utf-8"))
    images_dir = source_dir / "images"
    image_paths = sorted(p for p in images_dir.glob("*.png")) if images_dir.exists() else []
    return ParsedDocument(
        las_key=las_key,
        text=text,
        sections=split_sections(text),
        decisions=plan_images(image_paths, extra_drops=extra_drops) if image_paths else {},
        images_dir=images_dir,
    )


def whole_document_body(text: str) -> str:
    """Return a single-dimension document body, dropping the cover preamble."""
    lines = text.splitlines()
    headings = heading_indices(lines)
    start = headings[0][0] if headings else 0
    return "\n".join(lines[start:]).strip("\n")


def import_one(index: int, las_root: Path, repo_root: Path, dry_run: bool = False) -> dict:
    """Import one parsed document into the repo layout."""
    reviewed_drops: dict[str, str] = index.get("drop_images") or {}
    document = load_document(las_root, index["las_key"], extra_drops=reviewed_drops)
    if document is None:
        return {
            "las_key": index["las_key"],
            "status": "missing-parse",
            "path": str(las_root / index["las_key"] / "result.md"),
        }
    sections = document.sections
    forced_dimension = index.get("document_dimension")
    if forced_dimension:
        # Single-subject papers (for example an essay-only booklet) carry no
        # section markers, so the whole body belongs to one dimension.
        setattr(sections, forced_dimension, whole_document_body(document.text))

    report: dict = {
        "las_key": index["las_key"],
        "status": "ok",
        "label": index["label"],
        "markers": sections.markers,
        "images_total": len(document.decisions),
        "images_dropped": sorted(n for n, d in document.decisions.items() if not d.keep),
        "written": [],
    }

    appendices = index.get("appendices") or {}
    dimensions = {
        "comprehensive": ("comprehensive-by-year", index.get("comprehensive")),
        "case": ("case-by-year", index.get("case")),
        "essay": ("essay-by-year", index.get("essay")),
    }
    asset_dir = repo_root / "past-papers" / "assets" / index["label"]
    to_store: list[tuple[Path, str]] = []

    def prepare_body(source: ParsedDocument, dimension: str) -> str:
        """Rewrite figure links and normalise headings for one section."""
        raw = getattr(source.sections, dimension)
        kept: dict[str, str] = {}
        noted: dict[str, str] = {}
        for name in extract_image_names(raw):
            decision = source.decisions.get(name)
            if decision is not None and not decision.keep:
                if decision.reason.startswith("reviewed-removal"):
                    noted[name] = "*（原图含机构广告或水印，已移除）*"
                continue
            kept[name] = f"../assets/{index['label']}/{Path(name).stem}{IMAGE_OUTPUT_SUFFIX}"
            to_store.append((source.images_dir / name, name))
        return normalize_section_headings(rewrite_image_links(raw, kept, noted), dimension)

    for dimension, (folder, meta) in dimensions.items():
        if not meta or not getattr(sections, dimension).strip():
            continue
        body = prepare_body(document, dimension)
        appendix = appendices.get(dimension)
        if appendix:
            appendix_doc = load_document(las_root, appendix["las_key"], extra_drops=reviewed_drops)
            if appendix_doc is not None and getattr(appendix_doc.sections, dimension).strip():
                appendix_body = prepare_body(appendix_doc, dimension)
                heading = appendix.get("title", "参考答案与解析")
                body = f"{body}\n\n---\n\n## {heading}\n\n{appendix_body}"
                report.setdefault("appendices", {})[dimension] = appendix["las_key"]
        header = render_header(meta, dimension, meta["title"], meta["subtitle"])
        target = repo_root / "past-papers" / folder / f"{index['label']}.md"
        if index.get("filename_suffix"):
            target = target.with_name(f"{index['label']}{index['filename_suffix']}.md")
        report["written"].append(str(target.relative_to(repo_root)))
        report.setdefault("stats", {})[dimension] = summarize(body, dimension)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(header + body + "\n", encoding="utf-8")

    if not dry_run and to_store:
        asset_dir.mkdir(parents=True, exist_ok=True)
        for source_image, name in to_store:
            if source_image.exists():
                store_image(source_image, asset_dir / name)

    return report


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["documents"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--las-root", type=Path, default=DEFAULT_LAS_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--only", action="append", default=None, help="only import this las_key (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args(argv)

    documents = load_manifest(args.manifest)
    if args.only:
        wanted = set(args.only)
        documents = [d for d in documents if d["las_key"] in wanted]

    reports = [import_one(index, args.las_root, args.repo_root, args.dry_run) for index in documents]
    for item in reports:
        if item["status"] != "ok":
            print(f"[{item['status']}] {item['las_key']}")
            continue
        print(
            f"[ok] {item['las_key']} -> {', '.join(item['written']) or '(no section matched)'} "
            f"| images kept={item['images_total'] - len(item['images_dropped'])}/{item['images_total']} "
            f"| sections={item['markers']}"
        )
    if args.report:
        args.report.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
