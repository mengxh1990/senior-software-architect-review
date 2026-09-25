"""End-to-end regression tests for direct module classification and evidence."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tutor")]
import tutor as t
import knowledge_taxonomy as kt
import paper_practice
import mock_paper
import audit_taxonomy
import build_frequency_snapshot


class KnowledgeTaxonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.curriculum = t.load_curriculum()
        cls.topics = t.topic_map(cls.curriculum)
        cls.pool = t.load_quiz_question_pool(cls.curriculum)
        cls.items = {item["id"]: item for item in cls.pool}

    def state(self):
        return t.new_state(self.curriculum, "2026-09-01T09:00:00+08:00")

    def event(self, item_id, n, **updates):
        metadata = kt.objective_metadata(item_id)
        event = {
            "attempt_id": f"k-{n}",
            "event_type": "practice",
            "item_id": item_id,
            "topic_id": (metadata or {}).get("topic_id", "K01.OS_MEMORY_KERNEL"),
            "subject": "comprehensive",
            "skill": "recognition",
            "mode": "practice",
            "at": f"2026-09-{23 + n % 2}T10:00:00+08:00",
            "confidence": "sure",
            "score": 1,
            "max_score": 1,
            "wrong_reasons": [],
            "source_type": "self_authored",
        }
        event.update(updates)
        return event

    def test_every_objective_item_has_one_content_checked_primary(self):
        for item in self.pool:
            with self.subTest(item=item["id"]):
                self.assertEqual("reviewed", item["classification_status"])
                self.assertEqual(
                    [kt.objective_metadata(item["id"])["topic_id"]],
                    item["candidate_topics"],
                )
        source = copy.deepcopy(self.pool[0])
        source["stem"] += " 已修改条件"
        self.assertEqual(
            "needs_review", kt.annotate_question(source)["classification_status"]
        )

    def test_same_concepts_have_one_owner_across_legacy_modules(self):
        ids = [
            "exam-bank/14-absd-views.md#3",
            "past-papers/comprehensive-by-year/2025上.md#19-20",
        ]
        for item in ids:
            self.assertEqual(
                ["K06.DESIGN_DATA_VIEWS"], self.items[item]["candidate_topics"]
            )
        for number in (11, 12):
            row = self.items[f"past-papers/comprehensive-by-year/2023下.md#{number}"]
            self.assertEqual(["K03.SOFTWARE_DESIGN_UML"], row["candidate_topics"])
        self.assertEqual(
            ["K13.VIEWS_SOA_LAYERING"],
            self.items["exam-bank/26-soa-evolution.md#1"]["candidate_topics"],
        )

    def test_pattern_samples_are_scoped_to_pattern_module(
        self,
    ):
        state = self.state()
        for n in range(1, 7):
            t.apply_record_event(
                state,
                self.event(f"exam-bank/13-design-patterns.md#{n}", n),
                self.curriculum,
            )
        self.assertNotIn("K12.PATTERNS_SOA_MICROSERVICES", state["topics"])
        record = state["topics"]["K29.DESIGN_PATTERNS"]["mastery"]["recognition"]
        self.assertEqual("pass_ready", record["status"])
        self.assertEqual("module_sample", record["evidence_scope"])
        self.assertNotIn("knowledge", state)

    def test_two_questions_are_insufficient_for_module_sampling(self):
        state = self.state()
        for n in (3, 4):
            t.apply_record_event(
                state, self.event(f"exam-bank/14-absd-views.md#{n}", n), self.curriculum
            )
        module = state["topics"]["K06.DESIGN_DATA_VIEWS"]["mastery"]["recognition"]
        self.assertNotEqual("pass_ready", module["status"])
        self.assertNotIn("K11.COMPONENTS_4PLUS1", state["topics"])
        self.assertNotIn("K13.VIEWS_SOA_LAYERING", state["topics"])

    def test_legacy_module_evidence_needs_no_fine_point(self):
        state = self.state()
        for n in range(6):
            t.apply_record_event(state, self.event(f"legacy:{n}", n), self.curriculum)
        self.assertNotIn("knowledge", state)
        module = state["topics"]["K01.OS_MEMORY_KERNEL"]["mastery"]["recognition"]
        self.assertEqual(6, module["attempt_count"])
        self.assertEqual("pass_ready", module["status"])

    def test_variants_never_drift_to_other_modules(self):
        for ident in [
            "exam-bank/18-cache.md#1",
            "exam-bank/17-distributed-transactions.md#1",
            "exam-bank/15-microservice-cloud-native.md#1",
        ]:
            raw = self.items[ident]
            question = t.quiz_question_for_topic(
                raw, self.topics[raw["candidate_topics"][0]]
            )
            variant = t.pick_variant_question(question, self.pool, self.topics, {ident})
            if variant:
                self.assertEqual(question["topic_id"], variant["topic_id"])
                self.assertNotIn("EJB", variant["stem"])
            # If no verified same-point variant remains, do not invent a different target.
            self.assertIsNone(
                t.pick_variant_question(question, [], self.topics, {ident})
            )

    def test_multiple_batches_are_distinct_and_reveal_is_exact(self):
        items = paper_practice.build_case_items()
        self.assertEqual(len(items), len({item["id"] for item in items}))
        one = "past-papers/case-by-year/2025下.md#批次1-试题二"
        two = "past-papers/case-by-year/2025下.md#批次2-试题二"
        chosen = paper_practice.select(
            items,
            tag=None,
            year=None,
            numeral=None,
            blind_only=False,
            skip_missing_figures=False,
            item_id=two,
        )
        self.assertEqual([two], [item["id"] for item in chosen])
        self.assertNotEqual(
            next(item["tag"] for item in items if item["id"] == one), chosen[0]["tag"]
        )
        with self.assertRaisesRegex(ValueError, "批次"):
            paper_practice.select(
                items,
                tag=None,
                year="2025下",
                numeral="二",
                blind_only=False,
                skip_missing_figures=False,
            )

    def test_incomplete_cases_are_not_full_evidence_and_microservice_is_available(self):
        items = paper_practice.build_case_items()
        first = next(
            i for i in items if i["id"] == "past-papers/case-by-year/2026上.md#试题一"
        )
        self.assertEqual("案例 16", first["tag"])
        self.assertFalse(paper_practice.eligible(first))
        self.assertFalse(first["complete"])
        self.assertTrue(
            any(
                paper_practice.eligible(i)
                for i in items
                if i.get("topic_id") == "C04.CASE_MICROSERVICE"
            )
        )

    def test_case_points_cross_modules_without_extra_events_or_time(self):
        state = self.state()
        event = self.event(
            "past-papers/case-by-year/2026上.md#试题三",
            1,
            topic_id="C02.CASE_DATABASE",
            subject="case",
            skill="application",
            score=20,
            max_score=25,
            assessment_scope="case",
            complete=True,
            duration_seconds=1500,
            assessed_topics=[
                {
                    "topic_id": "K10.DATABASE_MODELING",
                    "score": 15,
                    "max_score": 19,
                    "evidence": "小问1和2说明锁策略",
                },
                {
                    "topic_id": "K21.MESSAGING_CACHE",
                    "score": 5,
                    "max_score": 6,
                    "evidence": "小问3说明锁归属与释放",
                },
            ],
        )
        t.apply_record_event(state, event, self.curriculum)
        self.assertIn("K10.DATABASE_MODELING", state["topics"])
        self.assertIn("K21.MESSAGING_CACHE", state["topics"])
        self.assertEqual(1, len(state["applied_attempt_ids"]))
        self.assertEqual(1, state["subjects"]["case"]["evidence_count"])
        self.assertEqual(1500, state["training_days"]["2026-09-24"]["case"])

    def test_fragment_rubrics_do_not_establish_full_abilities(self):
        for subject, skill, route, kid in [
            ("case", "application", "C02.CASE_DATABASE", "K10.DATABASE_MODELING"),
            ("essay", "production", "P01.ESSAY_ARCHITECTURE", "K04.ARCH_STYLES_ABSD"),
        ]:
            state = self.state()
            for n in range(3):
                event = self.event(
                    f"fragment:{n}",
                    n,
                    topic_id=route,
                    subject=subject,
                    skill=skill,
                    assessment_scope="fragment",
                    complete=False,
                    assessed_topics=[
                        {
                            "topic_id": kid,
                            "score": 1,
                            "max_score": 1,
                            "evidence": "short fragment",
                        }
                    ],
                )
                t.apply_record_event(state, event, self.curriculum)
            record = state["topics"][kid]["mastery"][skill]
            self.assertNotEqual("pass_ready", record["status"])
            self.assertEqual(0, record["mastery"])
            self.assertEqual(3, state["subjects"][subject]["evidence_count"])
            self.assertEqual(
                900, sum(day.get(subject, 0) for day in state["training_days"].values())
            )

    def test_frequency_excludes_small_extracts_and_activates_current_snapshot(self):
        snapshot = build_frequency_snapshot.build_snapshot("2026-09-25")
        year = next(p for p in snapshot["papers"] if p["year"] == "2020")
        self.assertFalse(year["frequency_eligible"])
        self.assertLess(year["paper_coverage"], 0.8)
        self.assertEqual("audited_snapshot", t.runtime_frequency()["active"])

    def test_mock_has_all_modules_unique_items_and_no_answer_leaks(self):
        private = mock_paper.private_items()
        public = mock_paper.public_payload()
        expected = {
            topic["id"]
            for topic in self.curriculum["topics"]
            if topic["id"].startswith("K")
        }
        self.assertEqual(75, len(private))
        self.assertEqual(expected, {item["topic_id"] for item in private})
        self.assertEqual(75, len({item["question_fingerprint"] for item in private}))
        for item in public["items"]:
            self.assertNotIn("answer", item)
            self.assertNotIn("knowledge_id", item)
        self.assertEqual(len(expected), public["coverage"]["modules"])

    def test_optional_rubric_fields_do_not_break_legacy_replays(self):
        old = self.event("exam-bank/14-absd-views.md#3", 1)
        new = {**old, "assessed_topics": [], "assessed_knowledge": []}
        self.assertFalse(t.events_conflict(old, new, compare_at=True))

    def test_catalog_audit_and_foreign_keys(self):
        result = audit_taxonomy.audit()
        self.assertTrue(result["healthy"], result["errors"])


if __name__ == "__main__":
    unittest.main()
