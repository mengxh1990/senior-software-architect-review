from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("exam_server", REPO_ROOT / "scripts" / "serve.py")
assert SPEC and SPEC.loader
exam_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exam_server)


class ExamServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        created_at = exam_server.tutor.now_iso()
        profile = {
            "schema_version": 1,
            "exam_date": "2099-12-31",
            "daily_minutes": 60,
            "timezone": "Asia/Shanghai",
            "background": "",
            "known_strengths": [],
            "known_weaknesses": [],
            "created_at": created_at,
        }
        curriculum = exam_server.tutor.load_curriculum()
        paths = exam_server.tutor.state_paths(self.data_dir)
        exam_server.tutor.atomic_write_json(paths["profile"], profile)
        exam_server.tutor.atomic_write_text(paths["attempts"], "")
        exam_server.tutor.save_state_bundle(
            self.data_dir,
            profile,
            exam_server.tutor.new_state(curriculum, created_at),
            backup=True,
        )

        def handler_factory(*args, **kwargs):
            return exam_server.ExamHandler(*args, data_dir=self.data_dir, **kwargs)

        self.server = exam_server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()

    def request(self, path: str, *, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.origin + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def correct_answers(self):
        return {
            str(item["number"]): item["question"]["answer"]
            for item in exam_server.private_items()
        }

    def install_legacy_mda_evidence(self) -> None:
        command = [
            sys.executable, str(REPO_ROOT / "scripts" / "tutor.py"),
            "--data-dir", str(self.data_dir), "record",
            "--topic", "K15.STRUCTURED_ANALYSIS_DFD", "--skill", "recognition",
            "--score", "0", "--max-score", "1", "--attempt-id", "legacy-mda",
            "--item-id", "legacy-placeholder", "--at", "2026-09-20T09:00:00+08:00",
        ]
        subprocess.run(command, cwd=REPO_ROOT, check=True, capture_output=True)
        paths = exam_server.tutor.state_paths(self.data_dir)
        event = json.loads(paths["attempts"].read_text(encoding="utf-8"))
        event["item_id"] = "past-papers/comprehensive-by-year/2022.md#26"
        exam_server.tutor.atomic_write_text(
            paths["attempts"], json.dumps(event, ensure_ascii=False) + "\n"
        )
        state = json.loads(paths["state"].read_text(encoding="utf-8"))
        state.pop("question_link_version")
        for path in (paths["state"], paths["state"].with_name("state.json.bak")):
            exam_server.tutor.atomic_write_json(path, state)

    def test_legacy_links_block_web_mock_until_explicit_migration(self) -> None:
        self.install_legacy_mda_evidence()
        paths = exam_server.tutor.state_paths(self.data_dir)
        before = {name: path.read_bytes() for name, path in paths.items() if path.is_file()}
        submission = {
            "session_id": f"web-{exam_server.PAPER_ID}-legacyguard1",
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        }
        for path, payload in (
            ("/api/status", None),
            ("/api/mock-paper", None),
            ("/api/mock-submit", submission),
            ("/api/mock-record", submission),
        ):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as denied:
                self.request(path, payload=payload)
            self.assertEqual(409, denied.exception.code)
            body = json.loads(denied.exception.read())
            self.assertIn("repair --normalize-question-links", body["error"])
            self.assertNotIn("correct_answer", json.dumps(body))
            if path == "/api/status":
                self.assertTrue(body["needs_repair"])
                self.assertFalse(body["needs_init"])
        self.assertEqual(before, {name: path.read_bytes() for name, path in paths.items() if path.is_file()})
        exam_server.tutor.normalize_question_links(self.data_dir)
        status, payload = self.request("/api/mock-paper")
        self.assertEqual(200, status)
        self.assertEqual(75, payload["data"]["question_count"])

    def test_web_status_projects_pending_evidence_without_writing(self) -> None:
        paths = exam_server.tutor.state_paths(self.data_dir)
        original = {path: path.read_bytes() for path in (
            paths["state"], paths["state"].with_name("state.json.bak"),
            paths["dashboard"],
        )}
        command = [
            sys.executable, str(REPO_ROOT / "scripts" / "tutor.py"),
            "--data-dir", str(self.data_dir), "record",
            "--topic", "K03.SOFTWARE_DESIGN_UML", "--skill", "recognition",
            "--score", "1", "--max-score", "1", "--attempt-id", "pending-web",
            "--item-id", "synthetic-pending-web", "--at", exam_server.tutor.now_iso(),
        ]
        subprocess.run(command, cwd=REPO_ROOT, check=True, capture_output=True)
        for path, content in original.items():
            exam_server.tutor.atomic_write_bytes(path, content)
        status, payload = self.request("/api/status")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["data"]["subjects"]["comprehensive"]["evidence_count"])
        self.request("/api/learning-plan?subject=comprehensive&limit=5")
        self.assertEqual(original, {path: path.read_bytes() for path in original})
        self.request("/api/mock-record", payload={
            "session_id": f"web-{exam_server.PAPER_ID}-recoverweb1",
            "answers": self.correct_answers(), "duration_seconds": 1800,
        })
        persisted = json.loads(paths["state"].read_text(encoding="utf-8"))
        self.assertEqual(77, len(persisted["applied_attempt_ids"]))


    def test_public_paper_withholds_answers_and_places_english_last(self) -> None:
        status, payload = self.request("/api/mock-paper")
        self.assertEqual(status, 200)
        paper = payload["data"]
        self.assertEqual(len(paper["items"]), 75)
        self.assertEqual((paper["passages"][0]["start"], paper["passages"][0]["end"]), (71, 75))
        serialized = json.dumps(paper, ensure_ascii=False)
        self.assertNotIn('"answer"', serialized)
        self.assertNotIn('"explanation"', serialized)

        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/questions.json")
        self.assertEqual(denied.exception.code, 404)

    def test_mock_submit_records_before_returning_answers(self) -> None:
        answers = self.correct_answers()
        session_id = f"web-{exam_server.PAPER_ID}-submit0001"
        submission = {
            "session_id": session_id,
            "answers": answers,
            "confidences": {},
            "durations": {},
            "duration_seconds": 1800,
        }

        with self.assertRaises(urllib.error.HTTPError) as retired:
            self.request("/api/mock-grade", payload={"answers": answers})
        self.assertEqual(retired.exception.code, 404)

        status, payload = self.request("/api/mock-submit", payload=submission)
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["record"]["already_recorded"])
        self.assertEqual(payload["data"]["score"], 75)
        self.assertEqual(
            payload["data"]["results"][0]["correct_answer"], answers["1"]
        )
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        self.assertEqual(len(attempts), 76)

    def test_prior_practice_keeps_mock_score_without_raising_measurement_confidence(self) -> None:
        item = exam_server.private_items()[0]
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "tutor.py"),
            "--data-dir", str(self.data_dir), "record", "--topic", item["topic_id"],
            "--skill", "recognition", "--score", "1", "--max-score", "1",
            "--attempt-id", "prior-practice", "--item-id", item["item_id"]],
            cwd=REPO_ROOT, check=True, capture_output=True)
        _, payload = self.request("/api/mock-submit", payload={
            "session_id": f"web-{exam_server.PAPER_ID}-exposed01", "answers": self.correct_answers(),
            "duration_seconds": 3600})
        self.assertEqual(75, payload["data"]["score"])
        subject = payload["data"]["record"]["status"]["subjects"]["comprehensive"]
        self.assertEqual("cold_start", subject["evidence_level"])
        self.assertIn("previously_exposed_items", subject["mock_scores"][0]["ineligible_reasons"])

    def test_quarantined_item_blocks_new_mock_paper(self) -> None:
        item = exam_server.private_items()[0]
        path = self.data_dir / "quiz-audit-queue.jsonl"
        path.write_text(json.dumps({"quiz_id": "invalid-fixture", "number": 1,
            "item_id": item["item_id"], "status": "quarantined"}) + "\n")
        _, payload = self.request("/api/mock-paper")
        self.assertEqual(exam_server.PAPER_IDS[1], payload["data"]["paper_id"])
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(f"/api/mock-paper?paper_id={exam_server.PAPER_ID}&mode=review")
        self.assertEqual(409, denied.exception.code)

    def test_mock_submit_withholds_answers_when_recording_fails(self) -> None:
        answers = self.correct_answers()
        state_path = exam_server.tutor.state_paths(self.data_dir)["state"]
        state_path.write_bytes(b'{"schema_version":')
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request(
                "/api/mock-submit",
                payload={
                    "session_id": f"web-{exam_server.PAPER_ID}-submit0002",
                    "answers": answers,
                    "confidences": {},
                    "durations": {},
                    "duration_seconds": 1800,
                },
            )
        self.assertEqual(failed.exception.code, 409)
        body = json.loads(failed.exception.read())
        self.assertFalse(body["ok"])
        self.assertNotIn("correct_answer", json.dumps(body))
        self.assertEqual(
            exam_server.tutor.load_attempts(
                exam_server.tutor.state_paths(self.data_dir)["attempts"]
            ),
            [],
        )

    def test_service_rejects_unignored_repo_private_directory(self) -> None:
        unsafe = REPO_ROOT / f"private-exam-audit-{uuid.uuid4().hex}"
        self.assertFalse(unsafe.exists())
        with self.assertRaises(exam_server.tutor.TutorError):
            exam_server.ensure_private_data_dir(unsafe)
        self.assertFalse(unsafe.exists())

    def test_cross_site_origin_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/status", headers={"Origin": "https://evil.example"})
        self.assertEqual(denied.exception.code, 403)

        navigation = urllib.request.Request(
            self.origin + "/",
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        with urllib.request.urlopen(navigation, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("本地模拟考试", response.read().decode("utf-8"))

    def test_submit_persists_once_and_same_paper_retest_marks_repeated_paper(self) -> None:
        answers = self.correct_answers()
        common = {
            "answers": answers,
            "confidences": {},
            "durations": {},
            "duration_seconds": 9000,
        }
        first_id = f"web-{exam_server.PAPER_ID}-12345678"
        _, first = self.request("/api/mock-record", payload={"session_id": first_id, **common})
        self.assertFalse(first["data"]["retest"])
        self.assertEqual(first["data"]["score"], 75)

        _, replay = self.request("/api/mock-record", payload={"session_id": first_id, **common})
        self.assertFalse(replay["data"]["retest"])
        self.assertTrue(replay["data"]["already_recorded"])

        second_id = f"web-{exam_server.PAPER_ID}-abcdefgh"
        _, second = self.request("/api/mock-record", payload={"session_id": second_id, **common})
        self.assertTrue(second["data"]["retest"])
        scores = second["data"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertEqual(2, len(scores))
        self.assertTrue(scores[0]["measurement_eligible"])
        self.assertFalse(scores[1]["measurement_eligible"])
        self.assertIn("repeated_paper", scores[1]["ineligible_reasons"])
        self.assertEqual(second_id, scores[1]["mock_id"])

        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        self.assertEqual(sum(event.get("event_type") == "mock" for event in attempts), 2)
        self.assertEqual(len(attempts), 152)
        first_question = next(event for event in attempts if event["attempt_id"].endswith("-q-01"))
        self.assertIn(first_question["item_id"], {item["item_id"] for item in exam_server.private_items()})
        self.assertEqual(first_question["selected_answer"], first_question["correct_answer"])

        _, feedback = self.request(
            "/api/mock-feedback",
            payload={"session_id": first_id, "answers": answers, "wrong_reasons": {}},
        )
        self.assertTrue(feedback["ok"])

        _, retest_feedback = self.request(
            "/api/mock-feedback",
            payload={"session_id": second_id, "answers": answers, "wrong_reasons": {}},
        )
        self.assertTrue(retest_feedback["ok"])
        self.assertTrue((self.data_dir / "postmortems.jsonl").is_file())

        tampered_answers = dict(answers)
        original = tampered_answers["1"]
        tampered_answers["1"] = next(key for key in "ABCD" if key != original)
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(
                "/api/mock-feedback",
                payload={
                    "session_id": first_id,
                    "answers": tampered_answers,
                    "wrong_reasons": {"1": ["knowledge_gap"]},
                },
            )
        self.assertEqual(rejected.exception.code, 400)

    def test_mock_record_replay_of_legacy_events_stays_idempotent(self) -> None:
        answers = self.correct_answers()
        session_id = f"web-{exam_server.PAPER_ID}-legacy0001"
        common = {
            "session_id": session_id,
            "answers": answers,
            "confidences": {},
            "durations": {},
            "duration_seconds": 1800,
        }
        status, first = self.request("/api/mock-record", payload=common)
        self.assertEqual(status, 200)
        self.assertFalse(first["data"]["already_recorded"])

        # A session recorded without optional derived item metadata must
        # replay idempotently rather than becoming a conflict.
        attempts_path = exam_server.tutor.state_paths(self.data_dir)["attempts"]
        attempts = exam_server.tutor.load_attempts(attempts_path)
        legacy_keys = {
            "question_fingerprint",
            "variant_of",
        }
        legacy = [
            {key: value for key, value in event.items() if key not in legacy_keys}
            for event in attempts
        ]
        exam_server.tutor.write_attempts(attempts_path, legacy)

        status, replay = self.request("/api/mock-record", payload=common)
        self.assertEqual(status, 200)
        self.assertTrue(replay["data"]["already_recorded"])
        self.assertFalse(replay["data"]["retest"])


    def test_mock_private_items_have_stable_topic_and_fingerprint(self) -> None:
        items = exam_server.private_items()
        self.assertEqual(len(items), 75)
        self.assertTrue(all(item.get("topic_id") for item in items))
        self.assertTrue(all(item.get("item_id") for item in items))
        self.assertTrue(all(len(item.get("question_fingerprint", "")) == 64 for item in items))
        public = exam_server.public_payload()
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("question_fingerprint", serialized)

    def test_new_form_keeps_its_identity_for_grading_replay_and_feedback(self) -> None:
        paper_id = exam_server.PAPER_IDS[1]
        _, payload = self.request(f"/api/mock-paper?paper_id={paper_id}")
        self.assertEqual(paper_id, payload["data"]["paper_id"])
        answers = {str(q["number"]): q["question"]["answer"] for q in exam_server.private_items(paper_id)}
        session_id = f"web-{paper_id}-newform01"
        body = {"session_id": session_id, "paper_id": paper_id, "answers": answers, "duration_seconds": 3600}
        _, submitted = self.request("/api/mock-submit", payload=body)
        self.assertEqual(75, submitted["data"]["score"])
        self.assertEqual(exam_server.private_items(paper_id)[0]["item_id"], submitted["data"]["results"][0]["id"])
        _, replay = self.request("/api/mock-submit", payload=body)
        self.assertTrue(replay["data"]["record"]["already_recorded"])
        self.request("/api/mock-feedback", payload={**body, "wrong_reasons": {}})
        feedback = json.loads((self.data_dir / "postmortems.jsonl").read_text())
        self.assertEqual(paper_id, feedback["paper_id"])
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/mock-submit", payload={**body, "paper_id": exam_server.PAPER_ID})
        self.assertEqual(400, denied.exception.code)

    def test_preflight_skips_exposed_forms_and_keeps_explicit_review(self) -> None:
        t = exam_server.tutor
        profile, state = t.load_profile_and_state(self.data_dir)
        events = []
        for n, paper_id in enumerate(exam_server.PAPER_IDS):
            q = exam_server.private_items(paper_id)[0]
            event = dict(attempt_id=f"prior-{n}", event_type="practice", item_id=q["item_id"], topic_id=q["topic_id"],
                         subject="comprehensive", skill="recognition", mode="practice", at=t.now_iso(),
                         score=1, max_score=1, confidence="sure", source_type="real", wrong_reasons=[])
            events.append(event)
            t.apply_record_event(state, event, t.load_curriculum())
            t.write_attempts(self.data_dir / "attempts.jsonl", events)
            t.save_state_bundle(self.data_dir, profile, state, backup=False)
            if n == 0:
                _, payload = self.request("/api/mock-paper")
                self.assertEqual(exam_server.PAPER_IDS[1], payload["data"]["paper_id"])
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/mock-paper")
        payload = json.loads(denied.exception.read())
        self.assertTrue(payload["resource_unavailable"])
        self.assertNotIn('"answer"', json.dumps(payload))
        _, review = self.request(f"/api/mock-paper?paper_id={exam_server.PAPER_ID}&mode=review")
        self.assertFalse(review["data"]["measurement"]["available"])
        self.assertIn("复习", review["data"]["measurement_note"])
        self.assertEqual(75, len(review["data"]["items"]))

    def test_pending_quiz_exposure_also_blocks_independent_measurement(self) -> None:
        q = exam_server.private_items()[0]
        session = {"schema_version": 1, "created_at": exam_server.tutor.now_iso(),
                   "questions": [{"item_id": q["item_id"], "question_fingerprint": q["question_fingerprint"]}]}
        path = exam_server.tutor.quiz_session_path(self.data_dir, "quiz-preflight")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session))
        _, result = self.request("/api/mock-paper")
        self.assertEqual(exam_server.PAPER_IDS[1], result["data"]["paper_id"])

    def test_concurrent_first_submissions_record_retest_with_repeated_paper(self) -> None:
        common = {
            "answers": self.correct_answers(),
            "confidences": {},
            "durations": {},
            "duration_seconds": 9000,
        }
        payloads = [
            {"session_id": f"web-{exam_server.PAPER_ID}-concurrent{i}", **common}
            for i in (1, 2)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(lambda payload: self.request("/api/mock-record", payload=payload), payloads)
            )

        self.assertEqual(sorted(response[1]["data"]["retest"] for response in responses), [False, True])
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        self.assertEqual(sum(event.get("event_type") == "mock" for event in attempts), 2)
        self.assertEqual(len(attempts), 152)

    def test_server_side_exam_clock_marks_real_overtime_ineligible(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-overtime1"
        status, payload = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        issued = exam_server.ISSUED_EXAMS[session_id]
        self.assertEqual(exam_server.PAPER_ID, issued["paper_id"])
        # 注入一个早已过期的开考时刻，模拟后台标签页节流导致的迟到交卷。
        issued["issued_at"] = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(seconds=10000)
        ).isoformat(timespec="seconds")

        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        })
        self.assertEqual(75, submitted["data"]["score"])
        scores = submitted["data"]["record"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertEqual(1, len(scores))
        self.assertFalse(scores[0]["measurement_eligible"])
        self.assertIn("overtime", scores[0]["ineligible_reasons"])
        self.assertGreater(scores[0]["duration_minutes"], 9000 / 60)
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        mock_event = next(event for event in attempts if event.get("event_type") == "mock")
        self.assertGreaterEqual(mock_event["duration_seconds"], 10000)

    def test_submit_falls_back_to_client_duration_when_issue_record_missing(self) -> None:
        # 未通过 GET 登记开考（如服务中途重启）时，回退客户端申报用时且不封顶：
        # 超过 24 小时的跨天旧会话也能记档，并按 overtime 降资格，与服务端
        # 权威计时路径一致，避免重试一个必然 400 的请求导致答卷丢失。
        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": f"web-{exam_server.PAPER_ID}-fallback1",
            "answers": self.correct_answers(),
            "duration_seconds": 9600,
        })
        scores = submitted["data"]["record"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertIn("overtime", scores[0]["ineligible_reasons"])
        _, stale = self.request("/api/mock-submit", payload={
            "session_id": f"web-{exam_server.PAPER_ID}-fallback2",
            "answers": self.correct_answers(),
            "duration_seconds": 86401,
        })
        stale_scores = stale["data"]["record"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertIn("overtime", stale_scores[0]["ineligible_reasons"])
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/mock-submit", payload={
                "session_id": f"web-{exam_server.PAPER_ID}-fallback3",
                "answers": self.correct_answers(),
                "duration_seconds": 0,
            })
        self.assertEqual(400, denied.exception.code)

    def test_server_side_exam_clock_records_real_duration_and_stays_eligible(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-clockok01"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        issued = exam_server.ISSUED_EXAMS[session_id]
        # 开考后 30 秒交卷；申报一个失真用时（若被采信将按 overtime 降资格）。
        issued["issued_at"] = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(seconds=30)
        ).isoformat(timespec="seconds")

        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 7200,
        })
        self.assertEqual(75, submitted["data"]["score"])
        scores = submitted["data"]["record"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertEqual(1, len(scores))
        self.assertTrue(scores[0]["measurement_eligible"])
        self.assertEqual([], scores[0]["ineligible_reasons"])
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        mock_event = next(event for event in attempts if event.get("event_type") == "mock")
        # 记档用时来自服务端 issued_at 差值，而非客户端申报值。
        self.assertGreaterEqual(mock_event["duration_seconds"], 30)
        self.assertLess(mock_event["duration_seconds"], 600)

    def test_full_duration_auto_submit_at_time_limit_stays_eligible(self) -> None:
        # 恰好用满 150 分钟后由前端自动交卷：服务端权威计时的发卷提前量、
        # 秒级截断与提交传输开销可把记档用时顶到 9001，必须在超时宽限内
        # 保持独立模考资格，而不是把用满时限的考生误判 overtime。
        session_id = f"web-{exam_server.PAPER_ID}-fulltime1"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        issued = exam_server.ISSUED_EXAMS[session_id]
        issued["issued_at"] = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(seconds=9000)
        ).isoformat(timespec="seconds")

        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 9000,
        })
        self.assertEqual(75, submitted["data"]["score"])
        scores = submitted["data"]["record"]["status"]["subjects"]["comprehensive"]["mock_scores"]
        self.assertEqual(1, len(scores))
        self.assertTrue(scores[0]["measurement_eligible"])
        self.assertEqual([], scores[0]["ineligible_reasons"])
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        mock_event = next(event for event in attempts if event.get("event_type") == "mock")
        self.assertGreaterEqual(mock_event["duration_seconds"], 9000)

    def test_idle_wall_clock_time_does_not_consume_daily_budget(self) -> None:
        # 挂机/睡眠墙钟不计训练：入账训练预算以训练时限+宽限封顶，
        # 模考证据里的完整原始用时保持不变（overtime 判定仍用真实用时）。
        session_id = f"web-{exam_server.PAPER_ID}-budget01"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        issued = exam_server.ISSUED_EXAMS[session_id]
        issued["issued_at"] = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(seconds=32400)
        ).isoformat(timespec="seconds")
        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        })
        self.assertEqual(75, submitted["data"]["score"])
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        mock_event = next(event for event in attempts if event.get("event_type") == "mock")
        self.assertGreaterEqual(mock_event["duration_seconds"], 32400)
        state = json.loads(
            exam_server.tutor.state_paths(self.data_dir)["state"].read_text(encoding="utf-8")
        )
        day = exam_server.tutor.parse_datetime(mock_event["at"]).date().isoformat()
        limit = exam_server.tutor.SUBJECT_TIME_LIMITS["comprehensive"]
        self.assertEqual(
            limit + exam_server.tutor.OVERTIME_GRACE_SECONDS,
            state["training_days"][day]["comprehensive"],
        )

    def test_head_on_static_asset_returns_same_headers_without_body(self) -> None:
        with urllib.request.urlopen(self.origin + "/index.html", timeout=3) as response:
            get_length = response.headers["Content-Length"]
            get_type = response.headers["Content-Type"]
            self.assertEqual(200, response.status)
            response.read()
        head = urllib.request.Request(self.origin + "/index.html", method="HEAD")
        with urllib.request.urlopen(head, timeout=3) as response:
            self.assertEqual(200, response.status)
            self.assertEqual(b"", response.read())
            self.assertEqual(get_length, response.headers["Content-Length"])
            self.assertEqual(get_type, response.headers["Content-Type"])

    def test_stale_issued_exam_records_are_evicted_on_new_activity(self) -> None:
        # 弃考会话的开考记录不能在长驻进程里永久滞留。
        stale_id = f"web-{exam_server.PAPER_ID}-staleone"
        status, _ = self.request(f"/api/mock-paper?session_id={stale_id}")
        self.assertEqual(200, status)
        exam_server.ISSUED_EXAMS[stale_id]["issued_at"] = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(hours=3)
        ).isoformat(timespec="seconds")
        fresh_id = f"web-{exam_server.PAPER_ID}-freshone"
        status, _ = self.request(f"/api/mock-paper?session_id={fresh_id}")
        self.assertEqual(200, status)
        self.assertNotIn(stale_id, exam_server.ISSUED_EXAMS)
        self.assertIn(fresh_id, exam_server.ISSUED_EXAMS)

    def test_reissued_session_keeps_first_issued_at(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-reissue1"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        stale = (
            exam_server.tutor.parse_datetime(exam_server.tutor.now_iso()) - timedelta(seconds=600)
        ).isoformat(timespec="seconds")
        exam_server.ISSUED_EXAMS[session_id]["issued_at"] = stale
        # 同会话重复 GET（控制台/curl 重放）不得把权威计时起点重置为当前时刻。
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        self.assertEqual(stale, exam_server.ISSUED_EXAMS[session_id]["issued_at"])

    def test_successful_submit_clears_issued_exam_entry(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-cleanup01"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        self.assertIn(session_id, exam_server.ISSUED_EXAMS)
        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        })
        self.assertEqual(75, submitted["data"]["score"])
        # 交卷落档后清理会话的开考记录，长驻进程不再无界增长。
        self.assertNotIn(session_id, exam_server.ISSUED_EXAMS)

    def test_submit_quality_recheck_skips_pure_replay(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-replayq01"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        })
        self.assertEqual(75, submitted["data"]["score"])
        # 记档成功后题目被隔离：纯幂等重放（事件已全部在档）不应被新的
        # 隔离状态阻塞，仍返回已记档结果而不是 409。
        item = exam_server.private_items()[0]
        (self.data_dir / "quiz-audit-queue.jsonl").write_text(
            json.dumps({"quiz_id": "invalid-fixture", "number": 1,
                        "item_id": item["item_id"], "status": "quarantined"}) + "\n"
        )
        _, replay = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": self.correct_answers(),
            "duration_seconds": 1800,
        })
        self.assertTrue(replay["data"]["record"]["already_recorded"])

    def test_mock_paper_rejects_session_bound_to_another_paper(self) -> None:
        mismatched = f"web-{exam_server.PAPER_IDS[1]}-mismatch01"
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(f"/api/mock-paper?paper_id={exam_server.PAPER_ID}&session_id={mismatched}")
        self.assertEqual(400, denied.exception.code)
        body = json.loads(denied.exception.read())
        self.assertEqual("开考会话与下发的试卷不一致", body["error"])
        self.assertNotIn(mismatched, exam_server.ISSUED_EXAMS)

    def test_postmortem_accepts_legacy_sessions_with_blank_answer_events(self) -> None:
        answers = self.correct_answers()
        for number in ("1", "2"):
            answers[number] = ""
        session_id = f"web-{exam_server.PAPER_ID}-legacyblank"
        self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": answers,
            "duration_seconds": 1800,
        })
        attempts_path = exam_server.tutor.state_paths(self.data_dir)["attempts"]
        attempts = exam_server.tutor.load_attempts(attempts_path)
        # 旧版服务对未答题也写逐题事件（selected_answer 为空）：补齐旧格式事件，
        # 升级后补交错因反馈必须兼容这种会话而不是拒绝记档。
        template = next(
            event for event in attempts
            if event.get("event_type") == "practice" and event["attempt_id"] == f"{session_id}-q-03"
        )
        for number in ("01", "02"):
            legacy_event = dict(template)
            legacy_event["attempt_id"] = f"{session_id}-q-{number}"
            legacy_event["selected_answer"] = ""
            attempts.append(legacy_event)
        exam_server.tutor.write_attempts(attempts_path, attempts)

        _, feedback = self.request("/api/mock-feedback", payload={
            "session_id": session_id,
            "answers": answers,
            "wrong_reasons": {number: ["knowledge_gap"] for number in ("1", "2")},
        })
        self.assertTrue(feedback["ok"])
        self.assertTrue((self.data_dir / "postmortems.jsonl").is_file())

    def test_unanswered_questions_write_no_practice_events(self) -> None:
        answers = self.correct_answers()
        for number in ("1", "2", "3", "8", "75"):
            answers[number] = ""
        session_id = f"web-{exam_server.PAPER_ID}-blank0001"
        _, submitted = self.request("/api/mock-submit", payload={
            "session_id": session_id,
            "answers": answers,
            "duration_seconds": 1800,
        })
        self.assertEqual(70, submitted["data"]["score"])
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        practice = [event for event in attempts if event.get("event_type") == "practice"]
        self.assertEqual(70, len(practice))
        recorded_ids = {event["attempt_id"] for event in attempts}
        self.assertNotIn(f"{session_id}-q-01", recorded_ids)
        self.assertNotIn(f"{session_id}-q-75", recorded_ids)
        self.assertIn(f"{session_id}-q-04", recorded_ids)
        # 未答题按错题参与错因反馈，postmortem 流程按实际答题数校验。
        _, feedback = self.request("/api/mock-feedback", payload={
            "session_id": session_id,
            "answers": answers,
            "wrong_reasons": {number: ["knowledge_gap"] for number in ("1", "2", "3", "8", "75")},
        })
        self.assertTrue(feedback["ok"])
        self.assertTrue((self.data_dir / "postmortems.jsonl").is_file())

    def test_submit_rechecks_paper_quality_after_issuance(self) -> None:
        session_id = f"web-{exam_server.PAPER_ID}-quality01"
        status, _ = self.request(f"/api/mock-paper?session_id={session_id}")
        self.assertEqual(200, status)
        item = exam_server.private_items()[0]
        # 发卷后题目被隔离：交卷必须复核卷质量并拒绝记档。
        (self.data_dir / "quiz-audit-queue.jsonl").write_text(
            json.dumps({"quiz_id": "invalid-fixture", "number": 1,
                        "item_id": item["item_id"], "status": "quarantined"}) + "\n"
        )
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/mock-submit", payload={
                "session_id": session_id,
                "answers": self.correct_answers(),
                "duration_seconds": 1800,
            })
        self.assertEqual(409, denied.exception.code)
        body = json.loads(denied.exception.read())
        self.assertIn("隔离", body["error"])
        self.assertEqual(
            exam_server.tutor.load_attempts(
                exam_server.tutor.state_paths(self.data_dir)["attempts"]
            ),
            [],
        )

    def test_prior_exposure_excludes_only_this_sessions_events(self) -> None:
        answers = self.correct_answers()
        first_id = f"web-{exam_server.PAPER_ID}-abcdefgh"
        second_id = f"web-{exam_server.PAPER_ID}-abcdefgh1"
        for session_id in (first_id, second_id):
            self.request("/api/mock-record", payload={
                "session_id": session_id, "answers": answers, "duration_seconds": 3600})
        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        mock_events = {
            event["attempt_id"]: event for event in attempts if event.get("event_type") == "mock"
        }
        # 会话 id 前缀相近时，上一场的逐题事件仍计入曝光，不被误排除。
        self.assertEqual(75, mock_events[second_id]["prior_exposure_count"])

    def test_curriculum_payload_keeps_static_frequency_on_fallback(self) -> None:
        original = exam_server.tutor.runtime_frequency
        exam_server.tutor.runtime_frequency = lambda: {
            "active": "curriculum_fallback", "snapshot_is_advisory": True,
            "reason": "missing_snapshot", "topics": {},
        }
        try:
            status, payload = self.request("/api/curriculum")
        finally:
            exam_server.tutor.runtime_frequency = original
        self.assertEqual(200, status)
        knowledge = [row for row in payload["data"]["topics"] if row["id"].startswith("K")]
        self.assertTrue(knowledge)
        for row in knowledge:
            self.assertEqual("curriculum_fallback", row["frequency_source"])
            self.assertIsNotNone(row["frequency_count"])

    def test_head_request_shares_same_origin_guard(self) -> None:
        allowed = urllib.request.Request(self.origin + "/api/status", method="HEAD")
        with urllib.request.urlopen(allowed, timeout=3) as response:
            self.assertEqual(204, response.status)
            self.assertEqual(b"", response.read())
        denied = urllib.request.Request(
            self.origin + "/api/status",
            method="HEAD",
            headers={"Origin": "https://evil.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(denied, timeout=3)
        self.assertEqual(403, rejected.exception.code)


if __name__ == "__main__":
    unittest.main()
