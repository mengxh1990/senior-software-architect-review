"""Unit tests for :mod:`scripts.question_registry`.

These pin the metadata contracts the tutor relies on: override tables must not
carry entries that can never take effect (item already mapped in
``question_topics.json``) or ghost items whose source file no longer exists,
and registration must reject unknown keys instead of silently dropping them.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "scripts" / "question_registry.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_registry():
    spec = importlib.util.spec_from_file_location("question_registry", REGISTRY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["question_registry"] = module
    spec.loader.exec_module(module)
    return module


question_registry = _load_registry()


def _item_topics() -> dict:
    payload = json.loads(
        (REPO_ROOT / "scripts" / "question_topics.json").read_text(encoding="utf-8")
    )
    return payload["items"]


def _source_file(item_id: str) -> Path:
    return REPO_ROOT / item_id.split("#", 1)[0]


def _load_taxonomy():
    spec = importlib.util.spec_from_file_location(
        "knowledge_taxonomy", REPO_ROOT / "scripts" / "knowledge_taxonomy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_taxonomy"] = module
    spec.loader.exec_module(module)
    return module


knowledge_taxonomy = _load_taxonomy()


class TaxonomyHeaderTests(unittest.TestCase):
    def test_topic_files_declare_the_current_taxonomy_version(self) -> None:
        """分类映射文件的头部 taxonomy_version 必须与代码常量一致，防止无声漂移。"""
        for name in ("question_topics.json", "subjective_topics.json"):
            with self.subTest(path=name):
                payload = json.loads(
                    (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    knowledge_taxonomy.TAXONOMY_VERSION,
                    payload.get("taxonomy_version"),
                    f"{name} 头部 taxonomy_version 与 knowledge_taxonomy.TAXONOMY_VERSION 不一致",
                )


class OverrideTableTests(unittest.TestCase):
    def test_item_topic_overrides_are_sole_source_and_reference_real_items(self) -> None:
        """覆盖表只收「题目不在主表、覆盖是唯一来源」的条目，且不得指向幽灵题目。"""
        mapped = _item_topics()
        for item_id in question_registry.ITEM_TOPIC_OVERRIDES:
            with self.subTest(item_id=item_id):
                self.assertNotIn(
                    item_id,
                    mapped,
                    "question_topics.json 已映射该题，覆盖条目永远不会生效",
                )
                self.assertTrue(
                    _source_file(item_id).exists(),
                    "覆盖条目指向不存在的题目文件",
                )

    def test_public_item_corrections_reference_real_items(self) -> None:
        for item_id in question_registry.PUBLIC_ITEM_CORRECTIONS:
            with self.subTest(item_id=item_id):
                self.assertTrue(
                    _source_file(item_id).exists(),
                    "更正条目指向不存在的题目文件",
                )


class ValidateEntryTests(unittest.TestCase):
    @staticmethod
    def _entry(**overrides) -> dict:
        entry = {
            "item_id": "self-authored/unit-001",
            "topic_id": "K01.OS_MEMORY_KERNEL",
            "stem": "单元测试登记题",
            "options": ["选项甲", "选项乙"],
        }
        entry.update(overrides)
        return entry

    def test_accepts_documented_keys(self) -> None:
        normalized = question_registry.validate_entry(
            self._entry(variant_of="self-authored/origin-001", memory_hook="钩子")
        )
        self.assertEqual(
            set(normalized),
            {
                "item_id",
                "topic_id",
                "stem",
                "options",
                "variant_of",
                "memory_hook",
                "question_fingerprint",
            },
        )

    def test_rejects_unknown_keys_instead_of_dropping_them(self) -> None:
        with self.assertRaises(ValueError) as caught:
            question_registry.validate_entry(
                self._entry(explain="笔误键", memory_hok="另一个笔误键")
            )
        message = str(caught.exception)
        self.assertIn("未知键", message)
        self.assertIn("explain", message)
        self.assertIn("memory_hok", message)


class LoadRegistryTests(unittest.TestCase):
    @staticmethod
    def _write_registry(path: Path, entry: dict) -> None:
        path.write_text(
            json.dumps(
                {"schema_version": question_registry.SCHEMA_VERSION, "questions": [entry]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_load_registry_strips_legacy_keys_from_existing_files(self) -> None:
        """旧版登记文件带 concept_id 等历史键时仍可加载，不阻断判分与记档命令。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "question-registry.json"
            self._write_registry(
                path,
                {
                    "item_id": "self-authored/legacy-001",
                    "topic_id": "K01.OS_MEMORY_KERNEL",
                    "stem": "旧格式登记题",
                    "options": ["选项甲", "选项乙"],
                    "concept_id": "K01",
                    "concept_label": "操作系统内存",
                    "question_family_id": "family-001",
                },
            )
            loaded = question_registry.load_registry(path)
        self.assertIn("self-authored/legacy-001", loaded)
        self.assertEqual(
            {"item_id", "topic_id", "stem", "options", "question_fingerprint"},
            set(loaded["self-authored/legacy-001"]),
        )

    def test_register_input_still_rejects_legacy_keys(self) -> None:
        """历史键在新登记输入中仍按未知键拒绝，笔误键不会静默进档。"""
        with self.assertRaises(ValueError) as caught:
            question_registry.validate_entry(
                {
                    "item_id": "self-authored/legacy-002",
                    "topic_id": "K01.OS_MEMORY_KERNEL",
                    "stem": "带历史键的新登记",
                    "options": ["选项甲", "选项乙"],
                    "concept_id": "K01",
                }
            )
        message = str(caught.exception)
        self.assertIn("未知键", message)
        self.assertIn("concept_id", message)


if __name__ == "__main__":
    unittest.main()
