"""Regression tests for the end-to-end teaching policy, using synthetic learners."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tutor as t
import paper_practice

DAY = date(2026, 9, 24)
K = "K01.OS_MEMORY_KERNEL"
C = "C02.CASE_DATABASE"


class TrainingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.curriculum = t.load_curriculum()
        self.topics = t.topic_map(self.curriculum)
        self.cli("init", "--exam-date", "2026-11-01", "--daily-minutes", "60")

    def cli(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = t.main(["--data-dir", str(self.data), *args])
        self.assertEqual(code, result, err.getvalue())
        text = out.getvalue()
        return json.loads(text) if text.lstrip().startswith("{") else text

    def state(self, measured=True):
        state = t.new_state(self.curriculum, "2026-09-01T09:00:00+08:00")
        if measured:
            for subject in t.SUBJECTS:
                state["subjects"][subject]["lower_bound_score"] = 65
                state["subjects"][subject]["last_practiced_at"] = "2026-09-24T09:00:00+08:00"
        return state

    def event(self, n, *, topic=K, skill="recognition", day=23, **overrides):
        event = {"attempt_id": f"a-{n}", "event_type": "practice", "item_id": f"fixture:{n}",
                 "topic_id": topic, "subject": t.choose_subject_for_skill(self.topics[topic], skill),
                 "skill": skill, "at": f"2026-09-{day:02d}T09:00:00+08:00", "mode": "practice",
                 "score": 1, "max_score": 1, "confidence": "sure", "source_type": "self_authored",
                 "wrong_reasons": []}
        event.update(overrides)
        return event

    def apply(self, state, event):
        return t.apply_record_event(state, event, self.curriculum)

    def raw(self, n, topic=K, **overrides):
        item = {"id": f"fixture:{n}", "stem": f"Question {n}", "options": [{"label": x, "text": x} for x in "ABCD"],
                "correct": ["A"], "explanation": "Reason for A", "source_type": "self_authored",
                "candidate_topics": [topic], "quality_status": "ready", "teaching_status": "ready"}
        item.update(overrides)
        return item

    def test_public_mapping_is_single_and_shared_with_mock(self):
        pool = {item["id"]: item for item in t.load_quiz_question_pool(self.curriculum)}
        self.assertEqual(["K08.SOFTWARE_PROCESS_MODELS"], pool["exam-bank/07-software-engineering.md#1"]["candidate_topics"])
        self.assertEqual(["K05.TEST_CMMI_PATTERNS"], pool["exam-bank/07-software-engineering.md#6"]["candidate_topics"])
        sys.path.insert(0, str(ROOT / "tutor"))
        import mock_paper
        for item in mock_paper.private_items():
            self.assertEqual([item["topic_id"]], pool[item["item_id"]]["candidate_topics"])
            self.assertEqual(item["question_fingerprint"], t.public_question_fingerprints()[item["item_id"]])

    def test_unsure_and_guesses_do_not_establish_mastery_or_erase_need(self):
        for confidence in ("unsure", "guess"):
            state = self.state()
            for n in range(6):
                self.apply(state, self.event(n, day=23+n//3, confidence=confidence))
            record = state["topics"][K]["mastery"]["recognition"]
            self.assertNotEqual("pass_ready", record["status"])
            self.assertLessEqual(t.topic_mastery(state, K, "recognition"), 0.5)

    def test_duplicate_contents_are_one_mastery_evidence(self):
        state = self.state()
        for n in range(6):
            self.apply(state, self.event(n, day=23+n//3, question_fingerprint="same-content"))
        self.assertNotEqual("pass_ready", state["topics"][K]["mastery"]["recognition"]["status"])

    def test_cold_start_and_two_danger_subjects_keep_real_need(self):
        state = self.state(False)
        state["strategy"]["subject_policies"]["essay"] = {"mode": "manual_trigger"}
        self.apply(state, self.event(1, day=24))
        _, allocation = t.effective_subject_allocations(state, DAY)
        self.assertEqual("case", t.select_target_subject(state, allocation, DAY))
        state["subjects"]["comprehensive"]["lower_bound_score"] = 44
        state["subjects"]["case"]["lower_bound_score"] = 20
        _, allocation = t.effective_subject_allocations(state, DAY)
        self.assertEqual("case", t.select_target_subject(state, allocation, DAY))

    def test_case_due_cannot_preempt_danger_subject(self):
        state = self.state()
        state["subjects"]["comprehensive"]["lower_bound_score"] = 20
        self.apply(state, self.event(1, topic=C, skill="application", score=0, max_score=25))
        self.assertEqual("comprehensive", t.next_training_action(state, DAY)["subject"])

    def test_maintenance_is_an_executable_route(self):
        state = self.state()
        state["strategy"]["subject_policies"]["essay"] = {"mode": "manual_trigger"}
        state["subjects"]["case"]["last_practiced_at"] = "2026-09-21T09:00:00+08:00"
        self.assertEqual("case_prepare", t.next_training_action(state, DAY)["mode"])

    def test_all_paused_waits_for_user(self):
        state = self.state()
        state["strategy"]["subject_policies"] = {subject: {"mode": "manual_trigger"} for subject in t.SUBJECTS}
        self.assertEqual("await_explicit_request", t.next_training_action(state, DAY)["mode"])
        for subject in t.SUBJECTS:
            self.cli("configure", "--subject-policy", f"{subject}=manual_trigger")
        result = self.cli("progress", "--runtime-json")
        self.assertEqual("await_explicit_request", result["next_action"]["mode"])

    def test_crunch_keeps_survival_cards_and_high_frequency_unlearned_topics(self):
        # §7 不变量 7：考前 3 天只排除「低频且无保命卡」的未学考点，
        # 高频/快分点与带保命卡的考点保留，且保命卡优先。
        state_path = self.data / "state.json"
        disk_state = json.loads(state_path.read_text(encoding="utf-8"))
        for subject in t.SUBJECTS:
            disk_state["subjects"][subject]["lower_bound_score"] = 65
            disk_state["subjects"][subject]["last_practiced_at"] = "2026-09-24T09:00:00+08:00"
        state_path.write_text(json.dumps(disk_state, ensure_ascii=False), encoding="utf-8")
        calm = self.cli("recommend", "--json", "--subject", "comprehensive", "--today", "2026-10-20", "--limit", "40")
        crunch = self.cli("recommend", "--json", "--subject", "comprehensive", "--today", "2026-10-30", "--limit", "40")
        self.assertFalse(calm["crunch_mode"])
        self.assertTrue(crunch["crunch_mode"])
        self.assertEqual(2, crunch["days_to_exam"])

        def ids(payload):
            return [item["topic_id"] for item in payload["recommendations"]]

        def score(payload, topic_id):
            return next(item["priority_score"] for item in payload["recommendations"] if item["topic_id"] == topic_id)

        # 低频且无保命卡的未学考点在考前被排除，平日仍在计划内。
        for topic_id in ("K29.DESIGN_PATTERNS", "K30.LANGUAGES_COMPILERS", "K31.BIG_DATA_ARCHITECTURE"):
            self.assertIn(topic_id, ids(calm))
            self.assertNotIn(topic_id, ids(crunch))
        # 高频/快分点的未学考点（无保命卡）考前保留。
        self.assertIn("K16.REQUIREMENTS_MANAGEMENT", ids(crunch))
        # 保命卡接入排序键：考前获得精确 1.5 倍加分、无卡考点分数不变。
        # 排序断言不依赖快照重建后的绝对位次，只钉 crunch 下带卡考点排在
        # 无卡考点之前（calm 下的相对位次随频率快照变化，不作前提）。
        card, no_card = "K22.ENGLISH_READING", "K16.REQUIREMENTS_MANAGEMENT"
        self.assertIn(card, ids(calm))
        self.assertIn(card, ids(crunch))
        self.assertLess(ids(crunch).index(card), ids(crunch).index(no_card))
        self.assertGreater(score(calm, card), 0)
        self.assertAlmostEqual(score(crunch, card) / score(calm, card), 1.5, places=2)
        self.assertAlmostEqual(score(crunch, no_card), score(calm, no_card), places=3)

    def test_subject_tie_break_uses_daily_trained_time_in_every_branch(self):
        # §7：危险/未测科的平局同样按当日已训练用时打破，硬门禁语义不变。
        state = self.state()
        state["subjects"]["comprehensive"]["lower_bound_score"] = 40
        state["subjects"]["case"]["lower_bound_score"] = 40
        state["subjects"]["essay"]["lower_bound_score"] = 65
        _, allocations = t.effective_subject_allocations(state, DAY)
        self.assertEqual(allocations["comprehensive"], allocations["case"])
        self.assertEqual("comprehensive", t.select_target_subject(state, allocations, DAY))
        state.setdefault("training_days", {})[DAY.isoformat()] = {"comprehensive": 1800}
        self.assertEqual("case", t.select_target_subject(state, allocations, DAY))
        # 三科均安全（>=45）时走 primary 分支，平局同样按当日已训练用时打破，
        # 且回退语义保持「返回 primary」。
        safe = self.state()
        _, safe_allocations = t.effective_subject_allocations(safe, DAY)
        self.assertEqual(1, len(set(safe_allocations.values())))
        self.assertEqual("comprehensive", t.select_target_subject(safe, safe_allocations, DAY))
        safe.setdefault("training_days", {})[DAY.isoformat()] = {"comprehensive": 1800}
        self.assertEqual("case", t.select_target_subject(safe, safe_allocations, DAY))

    def test_placeholder_explanation_never_passes_teaching_gate(self):
        # F6 根因回归防护：占位解析「（解析待补充）」不是可教学反馈，
        # 不得仅凭非空检查就通过教学门禁进入盲练池。
        item = self.raw(0, explanation="（解析待补充）")
        self.assertEqual("missing_explanation", t.teaching_status_for(item))
        item = self.raw(1, explanation="待补充")
        self.assertEqual("missing_explanation", t.teaching_status_for(item))
        item = self.raw(2, explanation="Reason for A")
        self.assertEqual("ready", t.teaching_status_for(item))
        item = self.raw(3, quality_status="invalid")
        self.assertEqual("not_applicable", t.teaching_status_for(item))
        # 教学门禁拦截的条目永远不会被 quiz 选题取用。
        blocked = self.raw(4, explanation="（解析待补充）", teaching_status="missing_explanation")
        self.assertIsNone(t.quiz_question_for_topic(blocked, self.topics[K]))

    def test_ready_pool_items_never_carry_placeholder_explanations(self):
        # 对题池全集断言：凡 teaching_status=ready 的条目，解析不得匹配占位模式。
        pool = t.load_quiz_question_pool(self.curriculum)
        ready = [item for item in pool if item["teaching_status"] == "ready"]
        self.assertTrue(ready)
        for item in ready:
            self.assertFalse(t.sanitize_bank.is_placeholder_explanation(item.get("explanation")))

    def test_missing_duration_estimates_full_essay_at_45_minutes(self):
        # §7：非片段完整论文缺时长按 45 分钟估值；片段仍按 5 分钟。
        state = self.state()
        t.record_training_time(state, {"subject": "essay", "assessment_scope": "essay",
                                       "at": "2026-09-24T09:00:00+08:00"})
        t.record_training_time(state, {"subject": "essay", "assessment_scope": "fragment",
                                       "at": "2026-09-24T10:00:00+08:00"})
        self.assertEqual({"essay": 2700 + 300}, state["training_days"]["2026-09-24"])
        self.assertEqual(2700 + 300, state["estimated_training_seconds"]["2026-09-24"])

    def test_corrupt_review_date_degrades_to_due_in_read_only_routing(self):
        # 路由入口遇到损坏的复习日期时降级为到期，不让只读路由整体崩溃。
        state = self.state()
        for subject in t.SUBJECTS:
            state["subjects"][subject]["last_measured_at"] = "2026-09-24T09:00:00+08:00"
        core = [topic_id for group in self.curriculum["strategy"]["comprehensive_cold_start_groups"]
                for topic_id in group]
        for topic_id in core:
            state["topics"].setdefault(topic_id, {"mastery": {}})["mastery"]["recognition"] = {"attempt_count": 1}
        state["topics"][K]["mastery"]["recognition"]["next_review_at"] = "not-a-date"
        state["topics"][K]["mastery"]["recognition"]["status"] = "needs_practice"
        action = t.next_training_action(state, DAY, profile={"daily_minutes": 60})
        self.assertEqual("quiz_prepare", action["mode"])
        self.assertEqual("due_review", action["task_kind"])

    def test_early_success_keeps_review_stage(self):
        state = self.state()
        for n, day in enumerate((1, 2, 5, 10, 12)):
            self.apply(state, self.event(n, day=day, score=0 if n == 0 else 1))
        self.assertEqual("2026-09-26", state["topics"][K]["mastery"]["recognition"]["next_review_at"])

    def test_case_fragments_do_not_prove_whole_case(self):
        state = self.state()
        for n, day in enumerate((20, 22)):
            self.apply(state, self.event(n, topic=C, skill="application", day=day))
        self.assertNotEqual("pass_ready", state["topics"][C]["mastery"]["application"]["status"])

    def test_assessed_case_point_closes_only_covered_review(self):
        state = self.state()
        self.apply(state, self.event(0, topic="K10.DATABASE_MODELING", skill="application", score=0, max_score=10, day=20))
        self.apply(state, self.event(1, topic=C, skill="application", score=20, max_score=25, day=24,
                                   assessment_scope="case", complete=True,
                                   assessed_topics=[{"topic_id": "K10.DATABASE_MODELING", "score": 8, "max_score": 10, "evidence": "fixture point"}]))
        record = state["topics"]["K10.DATABASE_MODELING"]["mastery"]["application"]
        self.assertGreater(record["next_review_at"], DAY.isoformat())
        self.assertEqual(2, len(state["applied_attempt_ids"]))
        self.assertEqual(2, state["subjects"]["case"]["evidence_count"])

    def test_short_urgent_group_is_not_replaced_by_lower_priority(self):
        pool = [self.raw(n) for n in range(4)] + [self.raw(n+4, "K02.NETWORK_PROTOCOLS") for n in range(5)]
        with patch.object(t, "load_quiz_question_pool", return_value=pool):
            items, _ = t.select_quiz_group(self.data, DAY, 5, [{"topic_id": K}, {"topic_id": "K02.NETWORK_PROTOCOLS"}], self.curriculum, self.topics)
        self.assertEqual(4, len(items))
        self.assertEqual({K}, {item["topic_id"] for item in items})

    def test_same_content_cannot_be_selected_twice(self):
        pool = [self.raw(n, stem="identical") for n in range(6)]
        with patch.object(t, "load_quiz_question_pool", return_value=pool):
            items, _ = t.select_quiz_group(self.data, DAY, 5, [{"topic_id": K}], self.curriculum, self.topics)
        self.assertEqual(1, len(items))

    def test_grade_survives_exhausted_continuation(self):
        self.cli("configure", "--subject-policy", "case=manual_trigger", "--subject-policy", "essay=manual_trigger")
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(0)]):
            quiz = self.cli("quiz-prepare", "--topic", K, "--limit", "1")
            result = self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "A", "--prepare-next", "--runtime-json")
            self.assertEqual(1, result["score"])
            self.assertEqual("failed", result["preparation_status"])
            self.assertEqual(1, len(t.load_attempts(self.data / "attempts.jsonl")))
            replay = self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "A", "--prepare-next", "--runtime-json")
            self.assertTrue(replay["idempotent"])

    def test_progress_survives_empty_bank(self):
        with patch.object(t, "load_quiz_question_pool", return_value=[]):
            result = self.cli("progress", "--json")
        self.assertEqual("resources_unavailable", result["next_action"]["mode"])
        self.assertEqual(set(t.SUBJECTS), set(result["subjects"]))

    def test_invalidated_question_is_quarantined_until_reviewed_release(self):
        pool = [self.raw(0)]
        # 客观题会话的 created_at 取自真实时钟，而 quiz_questions_served_on 按它做
        # 「当日不重复出题」判定；--today 只影响筛选日。若不固定时钟，当真实日期恰好
        # 等于下面的查询日时，放行后的题目仍会被当日去重挡住。因此固定时钟，并让
        # 出题日（DAY）与查询日（DAY+1）错开。
        served_on = DAY.isoformat()
        queried_on = (DAY + timedelta(days=1)).isoformat()
        with patch.object(t, "now_iso", return_value=f"{served_on}T09:00:00+08:00"), patch.object(
            t, "load_quiz_question_pool", return_value=pool
        ):
            quiz = self.cli("quiz-prepare", "--topic", K, "--limit", "1", "--today", served_on)
            self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "X", "--invalidate", "1=missing_table")
            self.assertEqual({"fixture:0"}, t.quarantined_item_ids(self.data, pool))
            self.cli("quiz-prepare", "--topic", K, "--limit", "1", "--today", queried_on, code=2)
            self.cli("release-question", "--item-id", "fixture:0", "--evidence", "Verified source table")
            self.assertEqual(set(), t.quarantined_item_ids(self.data, pool))
            self.cli("quiz-prepare", "--topic", K, "--limit", "1", "--today", queried_on)
        changed = [self.raw(0, explanation="new unreviewed version")]
        self.assertEqual({"fixture:0"}, t.quarantined_item_ids(self.data, changed))

    def test_variants_explain_errors_and_accept_invalidation(self):
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(n) for n in range(3)]):
            quiz = self.cli("quiz-prepare", "--topic", K, "--limit", "1")
            grade = self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "B")
            # 变式解析不随 quiz-grade 输出（作答前不泄露答案与解析），改从会话清单核验。
            manifest = json.loads(
                (self.data / "quiz-sessions" / f"{quiz['quiz_id']}.json").read_text(encoding="utf-8")
            )
            self.assertEqual("Reason for A", manifest["result"]["results"][0]["variant_question"]["explanation"])
            result = self.cli("quiz-variant-grade", "--quiz-id", quiz["quiz_id"], "--answers", "X", "--invalidate", "1=missing_table")
            self.assertEqual(0, result["max_score"])
            self.assertEqual(1, result["invalidated_count"])
            self.assertEqual(1, len(t.load_attempts(self.data / "attempts.jsonl")))

    def test_unsure_correct_has_bounded_follow_up(self):
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(n) for n in range(10)]):
            quiz = self.cli("quiz-prepare", "--topic", K, "--limit", "5")
            result = self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "A,A,A,A,A", "--confidences", "unsure")
            count = sum(bool(row.get("variant_question")) for row in result["results"])
            self.assertEqual(2, count)

    def test_repeat_or_overtime_mock_does_not_raise_evidence_level(self):
        state = self.state(False)
        for n in range(3):
            t.apply_mock_event(state, {"attempt_id": f"m{n}", "item_id": "same", "at": f"2026-09-{20+n}T09:00:00+08:00",
                "subject": "case", "score": 65, "max_score": 75, "duration_seconds": 4000,
                "source_type": "simulation", "complete": True})
        self.assertEqual("low", state["subjects"]["case"]["evidence_level"])
        t.apply_mock_event(state, {"attempt_id": "long", "item_id": "new", "at": "2026-09-24T09:00:00+08:00",
                "subject": "case", "score": 75, "max_score": 75, "duration_seconds": 20000,
                "source_type": "simulation", "complete": True})
        self.assertEqual("low", state["subjects"]["case"]["evidence_level"])
        self.assertEqual(["overtime"], state["subjects"]["case"]["mock_scores"][-1]["ineligible_reasons"])

    def test_full_essay_measures_subject_but_overtime_does_not_qualify(self):
        state = self.state(False)
        event = self.event(1, topic="P01.ESSAY_ARCHITECTURE", skill="production", score=60, max_score=75,
                           mode="full_timed", complete=True, word_count=2600, duration_seconds=20000)
        self.apply(state, event)
        self.assertEqual("cold_start", state["subjects"]["essay"]["evidence_level"])
        self.assertNotEqual("pass_ready", state["topics"][event["topic_id"]]["mastery"]["production"]["status"])
        self.apply(state, {**event, "attempt_id": "essay2", "item_id": "essay2", "duration_seconds": 7200, "at": "2026-09-24T09:00:00+08:00"})
        self.assertEqual("low", state["subjects"]["essay"]["evidence_level"])
        self.assertEqual(2, len(state["applied_attempt_ids"]))

    def test_budget_stops_automatic_training_without_creating_quiz(self):
        state = self.state()
        self.apply(state, self.event(1, day=24, duration_seconds=3600))
        action = t.next_training_action(state, DAY, profile={"daily_minutes": 60})
        self.assertEqual("session_complete", action["mode"])
        self.assertEqual(0, action["budget"]["remaining_seconds"])

    def test_mixed_check_keeps_topic_and_content_diversity(self):
        other = "K02.NETWORK_PROTOCOLS"
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(0), self.raw(1, other)]):
            items, _ = t.select_quiz_group(self.data, DAY, 5, [{"topic_id": K}, {"topic_id": other}], self.curriculum, self.topics, mixed=True)
        self.assertEqual({K, other}, {item["topic_id"] for item in items})

    def test_case_reveal_follows_answer_source(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, paper_practice.main(["--subject", "case", "--year", "2009下", "--numeral", "一", "--reveal"]))
        items = json.loads(out.getvalue())
        self.assertTrue(items[0]["answer"])
        self.assertEqual("reference_source_excerpt", items[0]["answer_format"])

    def test_subjective_score_needs_auditable_response_and_rubric(self):
        self.cli("record", "--topic", C, "--skill", "application", "--score", "20", "--max-score", "25",
                 "--assessment-scope", "case", "--complete", "--attempt-id", "case", "--item-id", "case", code=2)
        assessment = self.data / "score.json"
        assessment.write_text(json.dumps({"response_text": "独立案例回答", "rubric": {"version": "v1", "points": [
            {"score": 20, "max_score": 25, "evidence": "覆盖评分点"}]}}))
        self.cli("record", "--topic", C, "--skill", "application", "--score", "20", "--max-score", "25",
                 "--assessment-scope", "case", "--complete", "--attempt-id", "case", "--item-id", "case",
                 "--assessment-file", str(assessment))
        event = t.load_attempts(self.data / "attempts.jsonl")[0]
        self.assertEqual("独立案例回答", event["response_text"])
        self.assertEqual("v1", event["rubric"]["version"])

    def test_successful_fragment_does_not_regress_a_full_case(self):
        state = self.state()
        for n, day in enumerate((20, 22)):
            self.apply(state, self.event(n, topic=C, skill="application", day=day,
                score=25, max_score=25, complete=True, assessment_scope="case"))
        self.apply(state, self.event(3, topic=C, skill="application", day=24))
        self.assertEqual("pass_ready", state["topics"][C]["mastery"]["application"]["status"])

    def test_sufficient_block_time_routes_to_overdue_measurement(self):
        state = self.state()
        state["strategy"]["subject_policies"] = {"comprehensive": {"mode": "manual_trigger"}, "essay": {"mode": "manual_trigger"}}
        action = t.next_training_action(state, DAY, profile={"daily_minutes": 100})
        self.assertEqual("mock_manual_flow", action["mode"])
        self.assertEqual("case", action["subject"])

    def test_budget_stop_and_all_paused_work_in_plain_text_progress(self):
        for subject in t.SUBJECTS:
            self.cli("configure", "--subject-policy", f"{subject}=manual_trigger")
        self.assertIn("收尾/等待", self.cli("progress"))

    def test_removed_variant_is_excluded_without_losing_other_results(self):
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(0), self.raw(1)]):
            quiz = self.cli("quiz-prepare", "--topic", K, "--limit", "1")
            self.cli("quiz-grade", "--quiz-id", quiz["quiz_id"], "--answers", "B")
        with patch.object(t, "load_quiz_question_pool", return_value=[self.raw(0)]):
            result = self.cli("quiz-variant-grade", "--quiz-id", quiz["quiz_id"], "--answers", "A")
            self.assertEqual(1, result["invalidated_count"])
            self.assertEqual(0, result["recorded_attempts"])
            replay = self.cli("quiz-variant-grade", "--quiz-id", quiz["quiz_id"], "--answers", "A")
            self.assertTrue(replay["idempotent"])

    def test_shadow_rebuild_is_readonly_and_preserves_policies(self):
        self.cli("configure", "--subject-policy", "essay=manual_trigger")
        before = {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()}
        result = self.cli("repair", "--dry-run")
        self.assertTrue(result["strategy_preserved"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()})


    def test_full_timed_requires_essay_scope_and_recomputed_word_count(self):
        # full_timed 仅用于完整论文：显式片段 scope 直接拒绝（否则会绕过
        # --assessment-file 强制与原答字数重算）；正常路径必须有原答文件，
        # 字数以去空白重算为准。
        def essay_args(attempt_id, item_id):
            return ("record", "--topic", "P01.ESSAY_ARCHITECTURE", "--skill", "production",
                    "--score", "52", "--max-score", "75", "--mode", "full_timed", "--complete",
                    "--duration-seconds", "7200", "--attempt-id", attempt_id, "--item-id", item_id)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = t.main(["--data-dir", str(self.data), *essay_args("essay-fragment", "essay-1"),
                             "--assessment-scope", "fragment", "--word-count", "2600"])
        self.assertEqual(2, result)
        self.assertIn("full_timed 仅用于完整论文", err.getvalue())
        self.cli(*essay_args("essay-no-file", "essay-1"), "--word-count", "2600", code=2)
        assessment = self.data / "essay-score.json"
        assessment.write_text(json.dumps({"response_text": "架构论证。" * 650 + "   ",
                                          "rubric": {"version": "v1", "points": [
                                              {"score": 52, "max_score": 75, "evidence": "覆盖评分点"}]}},
                                         ensure_ascii=False))
        self.cli(*essay_args("essay-ok", "essay-2"), "--word-count", "1",
                 "--assessment-file", str(assessment))
        event = t.load_attempts(self.data / "attempts.jsonl")[0]
        self.assertEqual(3250, event["word_count"])

    def test_remeasurement_is_required_only_after_seven_full_days(self):
        # 规范为「超过 7 天」：恰好第 7 天不需要复测，第 8 天才需要。
        state_path = self.data / "state.json"
        disk_state = json.loads(state_path.read_text(encoding="utf-8"))
        for subject in t.SUBJECTS:
            disk_state["subjects"][subject]["lower_bound_score"] = 65
        for age, expected in ((7, False), (8, True)):
            for subject in t.SUBJECTS:
                disk_state["subjects"][subject]["last_measured_at"] = \
                    f"2026-09-{24 - age:02d}T09:00:00+08:00"
            state_path.write_text(json.dumps(disk_state, ensure_ascii=False), encoding="utf-8")
            result = self.cli("progress", "--today", DAY.isoformat(), "--json")
            self.assertEqual(expected, all(row["needs_remeasurement"]
                                           for row in result["subjects"].values()))

    def test_event_without_timestamp_is_rejected_with_clear_error(self):
        # 缺 at 字段不再静默取当前时间，也不再以 KeyError 逃逸。
        event = self.event(0, day=24)
        event.pop("at")
        with self.assertRaises(t.TutorError) as ctx:
            self.apply(self.state(), event)
        self.assertIn("at", str(ctx.exception))

    def test_mock_event_without_timestamp_is_rejected_with_clear_error(self):
        # 与 record 事件对齐：缺 at 的 mock 事件也以 TutorError 报告，
        # 不再在回放路径以 KeyError('at') 逃逸。
        event = {"attempt_id": "m1", "item_id": "p1", "subject": "case", "score": 65,
                 "max_score": 75, "duration_seconds": 4000, "source_type": "simulation",
                 "complete": True}
        with self.assertRaises(t.TutorError) as ctx:
            t.apply_mock_event(self.state(), event)
        self.assertIn("at", str(ctx.exception))

    def test_remeasurement_boundary_is_shared_by_status_payload(self):
        # status/serve.py 直出的 status_payload 与 progress 共用同一边界常数：
        # 恰好第 7 天不需要复测，第 8 天才需要，两条路径不得再次分叉。
        profile, _ = t.load_profile_and_state(self.data)
        now = datetime.now().astimezone()
        for age, expected in ((7, False), (8, True)):
            state = self.state()
            for subject in t.SUBJECTS:
                state["subjects"][subject]["last_measured_at"] = (
                    now - timedelta(days=age)).isoformat(timespec="seconds")
            payload = t.status_payload(profile, state)
            self.assertEqual(
                expected,
                all(row["needs_remeasurement"] for row in payload["subjects"].values()),
            )

    def test_recent_and_mock_evidence_order_by_parsed_time_across_timezones(self):
        # ISO 字符串在混合时区下字典序不可靠，证据窗口与模考序列按解析时间排序。
        state = self.state(False)
        self.apply(state, self.event(0, day=20, at="2026-09-20T02:00:00+08:00"))
        self.apply(state, self.event(1, day=20, at="2026-09-19T20:00:00+00:00"))
        recent = state["topics"][K]["mastery"]["recognition"]["recent_evidence"]
        self.assertEqual("fixture:1", recent[-1]["item_id"])
        t.apply_mock_event(state, {"attempt_id": "m1", "item_id": "p1", "at": "2026-09-20T02:00:00+08:00",
                                   "subject": "case", "score": 65, "max_score": 75, "duration_seconds": 4000,
                                   "source_type": "simulation", "complete": True})
        t.apply_mock_event(state, {"attempt_id": "m2", "item_id": "p2", "at": "2026-09-19T20:00:00+00:00",
                                   "subject": "case", "score": 66, "max_score": 75, "duration_seconds": 4000,
                                   "source_type": "simulation", "complete": True})
        self.assertEqual("m2", state["subjects"]["case"]["mock_scores"][-1]["mock_id"])

    def test_derived_attempt_id_collision_does_not_double_count(self):
        # 派生事件与真实日志事件撞名时跳过应用，也不把真实事件的已应用标记
        # 删掉，避免下次加载重复计分。
        state = self.state()
        real = self.event(0, topic="K10.DATABASE_MODELING", skill="application", score=8, max_score=10,
                          day=20, attempt_id="case1:K10.DATABASE_MODELING")
        self.apply(state, real)
        self.apply(state, self.event(1, topic=C, skill="application", score=20, max_score=25, day=24,
                                     assessment_scope="case", complete=True, attempt_id="case1",
                                     assessed_topics=[{"topic_id": "K10.DATABASE_MODELING", "score": 8,
                                                       "max_score": 10, "evidence": "fixture point"}]))
        self.assertIn("case1:K10.DATABASE_MODELING", state["applied_attempt_ids"])
        self.assertEqual(1, state["topics"]["K10.DATABASE_MODELING"]["mastery"]["application"]["attempt_count"])
        self.assertEqual(2, state["subjects"]["case"]["evidence_count"])
        # full_timed 自动生成的 :measurement 模考撞名时同样不误删真实模考标记。
        t.apply_mock_event(state, {"attempt_id": "essay1:measurement", "item_id": "paper-x",
                                   "at": "2026-09-24T09:00:00+08:00", "subject": "essay",
                                   "score": 60, "max_score": 75, "duration_seconds": 7000,
                                   "source_type": "simulation", "complete": True})
        self.apply(state, self.event(2, topic="P01.ESSAY_ARCHITECTURE", skill="production", score=60,
                                     max_score=75, mode="full_timed", complete=True, word_count=2600,
                                     duration_seconds=7000, attempt_id="essay1"))
        self.assertIn("essay1:measurement", state["applied_attempt_ids"])
        self.assertEqual(1, len(state["subjects"]["essay"]["mock_scores"]))


if __name__ == "__main__":
    unittest.main()
