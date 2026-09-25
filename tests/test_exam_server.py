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

    def test_submit_persists_once_and_same_paper_retest_is_not_a_new_mock(self) -> None:
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

        attempts = exam_server.tutor.load_attempts(
            exam_server.tutor.state_paths(self.data_dir)["attempts"]
        )
        self.assertEqual(sum(event.get("event_type") == "mock" for event in attempts), 1)
        self.assertEqual(len(attempts), 151)
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

    def test_concurrent_first_submissions_create_one_full_mock(self) -> None:
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
        self.assertEqual(sum(event.get("event_type") == "mock" for event in attempts), 1)
        self.assertEqual(len(attempts), 151)


if __name__ == "__main__":
    unittest.main()
