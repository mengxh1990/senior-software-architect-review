#!/usr/bin/env python3
"""Render the comprehensive-quiz figure crops recorded in the manifest.

The objective quiz runtime hands the learner a figure as a separate asset
(``figure_assets``) rather than a raw Markdown path. Those assets must be
reproducible: every crop is described in
``scripts/comprehensive_figure_manifest.json`` by its source PDF, page number,
render DPI and pixel crop box.

This maintenance tool turns each manifest entry into
``past-papers/assets/comprehensive/<year>/<file>``:

* ``pdftoppm -png -r <dpi> -f <page> -l <page> -x <x> -y <y> -W <w> -H <h>``
  renders exactly the recorded crop;
* the faint pink print watermark is suppressed on near-grayscale scans (same
  rule the case/essay importer uses), so the exam figure stays readable;
* the result is stored as a lossless WebP to match the other repo assets.

Usage::

    python3 scripts/build_comprehensive_figures.py            # render all crops
    python3 scripts/build_comprehensive_figures.py --check    # verify assets only
    python3 scripts/build_comprehensive_figures.py --only 2018下-p02-pcb-index

The runtime (``sanitize_bank`` / ``tutor.py``) never calls this script; it only
reads the resulting assets, so a missing local PDF or ``pdftoppm`` blocks the
maintenance build but never a learner's quiz turn.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "scripts" / "comprehensive_figure_manifest.json"
ASSET_SUFFIX = ".webp"


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到插图来源表：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"插图来源表损坏：{path}: {error}") from error


def iter_assets(manifest: dict, only: str | None = None):
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise SystemExit("插图来源表需要非空的 assets 数组")
    for asset in assets:
        if only and asset.get("id") != only:
            continue
        yield asset


def asset_path(asset: dict) -> Path:
    relative = str(asset.get("file") or "").strip()
    if not relative:
        raise SystemExit(f"插图 {asset.get('id')} 缺少 file")
    path = REPO_ROOT / relative
    if path.suffix.lower() != ASSET_SUFFIX:
        path = path.with_suffix(ASSET_SUFFIX)
    return path


def source_pdf(manifest: dict, asset: dict) -> Path:
    source = asset.get("source") or {}
    relative = str(source.get("pdf") or "").strip()
    if not relative:
        raise SystemExit(f"插图 {asset.get('id')} 缺少 source.pdf")
    pdf = Path(relative)
    if not pdf.is_absolute():
        root = Path(str(manifest.get("source_root") or "").strip())
        pdf = root / relative
    return pdf


def render_asset(manifest: dict, asset: dict, *, dry_run: bool = False) -> Path:
    source = asset.get("source") or {}
    pdf = source_pdf(manifest, asset)
    if not pdf.is_file():
        raise SystemExit(f"插图 {asset.get('id')} 的源 PDF 不存在：{pdf}")
    page = int(source.get("page") or 0)
    dpi = int(source.get("dpi") or 300)
    crop = source.get("crop") or {}
    try:
        box = {key: int(crop[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"插图 {asset.get('id')} 的 crop 需要 x/y/width/height 整数") from error
    if page < 1 or min(box.values()) <= 0:
        raise SystemExit(f"插图 {asset.get('id')} 的 page/crop 取值非法")
    tool = shutil.which("pdftoppm")
    if not tool:
        raise SystemExit("找不到 pdftoppm（poppler-utils），无法复现裁图")

    target = asset_path(asset)
    if dry_run:
        print(f"会渲染 {asset['id']} -> {target.relative_to(REPO_ROOT).as_posix()}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "crop"
        command = [
            tool,
            "-png",
            "-r",
            str(dpi),
            "-f",
            str(page),
            "-l",
            str(page),
            "-x",
            str(box["x"]),
            "-y",
            str(box["y"]),
            "-W",
            str(box["width"]),
            "-H",
            str(box["height"]),
            str(pdf),
            str(prefix),
        ]
        subprocess.run(command, check=True, capture_output=True)
        rendered = sorted(Path(tmp).glob("crop*.png"))
        if not rendered:
            raise SystemExit(f"pdftoppm 未产出裁图：{asset['id']}")
        store_figure(rendered[0], target)
    print(f"已生成 {target.relative_to(REPO_ROOT).as_posix()}")
    return target


def store_figure(source: Path, target: Path) -> None:
    """Suppress a faint print watermark, then save a lossless WebP."""

    from PIL import Image

    from import_las_papers import suppress_print_watermark

    with Image.open(source) as image:
        cleaned = suppress_print_watermark(image)
        cleaned.save(target, "WEBP", lossless=True, method=6)


def check_asset(asset: dict) -> list[str]:
    """Report problems that would leave the item pointing at a missing crop."""

    problems: list[str] = []
    target = asset_path(asset)
    if not target.is_file():
        problems.append(f"{asset.get('id')}: 资产缺失 {target.relative_to(REPO_ROOT).as_posix()}")
    item_ids = asset.get("item_ids") or []
    if not isinstance(item_ids, list) or not item_ids:
        problems.append(f"{asset.get('id')}: 需要 item_ids 以标明引用该图的题号")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只校验资产是否存在，不渲染")
    parser.add_argument("--dry-run", action="store_true", help="打印将要渲染的裁图，不落盘")
    parser.add_argument("--only", help="只处理指定 id")
    args = parser.parse_args(argv[1:])

    manifest = load_manifest()
    problems: list[str] = []
    for asset in iter_assets(manifest, args.only):
        if args.check:
            problems.extend(check_asset(asset))
        else:
            render_asset(manifest, asset, dry_run=args.dry_run)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    if args.check:
        print("综合知识插图资产齐全。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
