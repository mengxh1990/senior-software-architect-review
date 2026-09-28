"""Regression coverage for the comprehensive-quiz figure channel."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
MANIFEST_PATH = SCRIPTS / "comprehensive_figure_manifest.json"
BUILD_SCRIPT = SCRIPTS / "build_comprehensive_figures.py"
SANITIZER_PATH = SCRIPTS / "sanitize_bank.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if name == "sanitize_bank":
        sys.modules["sanitize_bank"] = module
    spec.loader.exec_module(module)
    return module


builder = _load_module("build_comprehensive_figures", BUILD_SCRIPT)
sanitize_bank = _load_module("sanitize_bank", SANITIZER_PATH)


class ComprehensiveFigureManifestTests(unittest.TestCase):
    def test_manifest_entries_are_traceable_and_complete(self) -> None:
        """每条裁图记录必须能追溯到源 PDF、页码和裁剪框。"""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(1, manifest.get("schema_version"))
        self.assertTrue(manifest.get("source_root"), "必须记录原卷所在目录")
        assets = manifest.get("assets")
        self.assertIsInstance(assets, list)
        self.assertTrue(assets, "插图来源表至少需要一条记录")
        for asset in assets:
            with self.subTest(asset=asset.get("id")):
                self.assertTrue(asset.get("id"))
                self.assertTrue(
                    asset.get("file", "").startswith("past-papers/assets/comprehensive/")
                )
                self.assertTrue(asset.get("item_ids"), "必须标明引用该裁图的题号")
                source = asset.get("source") or {}
                self.assertTrue(source.get("pdf"), "必须记录源 PDF")
                self.assertGreaterEqual(int(source.get("page") or 0), 1)
                self.assertGreater(int(source.get("dpi") or 0), 0)
                crop = source.get("crop") or {}
                for key in ("x", "y", "width", "height"):
                    self.assertGreater(int(crop.get(key) or 0), 0, f"裁剪框缺少 {key}")

    def test_cli_check_passes_and_assets_exist(self) -> None:
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("齐全", result.stdout)

    def test_manifest_items_reference_their_crop_in_markdown(self) -> None:
        """裁图必须在对应题目的 markdown 里被引用，且题面只留【图 N】。"""
        manifest = builder.load_manifest()
        for asset in builder.iter_assets(manifest):
            target = builder.asset_path(asset)
            self.assertTrue(target.is_file(), f"缺少裁图资产：{target}")
            relative = target.relative_to(REPO_ROOT / "past-papers").as_posix()
            link = "../" + relative
            for item_id in asset["item_ids"]:
                with self.subTest(asset=asset["id"], item=item_id):
                    source = REPO_ROOT / item_id.split("#", 1)[0]
                    number = item_id.rsplit("#", 1)[1]
                    item = next(
                        candidate
                        for candidate in sanitize_bank.parse_paper(source)
                        if candidate["id"].rsplit("#", 1)[1] == number
                    )
                    self.assertIn(
                        link,
                        item.get("figures") or [],
                        f"{item_id} 的 markdown 未引用裁图 {link}",
                    )
                    self.assertIn("【图 1】", item["stem"])
                    self.assertNotIn("](", item["stem"])

    @unittest.skipUnless(shutil.which("pdftoppm"), "需要 pdftoppm 才能复现裁图")
    def test_recorded_crop_box_reproduces_the_shipped_asset(self) -> None:
        """按来源表记录的页码/裁剪框渲染，尺寸必须与入库裁图一致。"""
        from PIL import Image

        manifest = builder.load_manifest()
        checked = 0
        for asset in builder.iter_assets(manifest):
            pdf = builder.source_pdf(manifest, asset)
            if not pdf.is_file():
                continue
            checked += 1
            source = asset["source"]
            crop = source["crop"]
            shipped = builder.asset_path(asset)
            with tempfile.TemporaryDirectory() as tmp:
                prefix = Path(tmp) / "crop"
                subprocess.run(
                    [
                        shutil.which("pdftoppm"),
                        "-png",
                        "-r",
                        str(source["dpi"]),
                        "-f",
                        str(source["page"]),
                        "-l",
                        str(source["page"]),
                        "-x",
                        str(crop["x"]),
                        "-y",
                        str(crop["y"]),
                        "-W",
                        str(crop["width"]),
                        "-H",
                        str(crop["height"]),
                        str(pdf),
                        str(prefix),
                    ],
                    check=True,
                    capture_output=True,
                )
                rendered = sorted(Path(tmp).glob("crop*.png"))[0]
                with Image.open(rendered) as image:
                    self.assertEqual((crop["width"], crop["height"]), image.size)
            with Image.open(shipped) as image:
                self.assertEqual(
                    (crop["width"], crop["height"]),
                    image.size,
                    f"{asset['id']} 入库裁图与记录的裁剪框尺寸不一致",
                )
        if checked == 0:
            self.skipTest("本机没有可复核的原卷 PDF")


if __name__ == "__main__":
    unittest.main()
