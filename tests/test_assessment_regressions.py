"""Regression coverage for module measurement and unavailable assessment stock."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tutor')]
import tutor as t
import module_assessment
import mock_paper
import paper_practice

K = 'K21.MESSAGING_CACHE'


class AssessmentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.curriculum = t.load_curriculum()
        self.cli('init', '--exam-date', '2026-11-01', '--daily-minutes', '180')
        self.cli('configure', '--subject-policy', 'case=manual_trigger', '--subject-policy', 'essay=manual_trigger')
        self.profile, self.state = t.load_profile_and_state(self.data)
        self.events = []

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = t.main(['--data-dir', str(self.data), *args])
        self.assertEqual(0, result, err.getvalue())
        text = out.getvalue()
        return json.loads(text) if text.lstrip().startswith('{') else text

    def add(self, n, day, **updates):
        event = dict(attempt_id=f'a-{n}', event_type='practice', item_id=f'fixture:{n}',
                     topic_id=K, subject='comprehensive', skill='recognition', mode='practice',
                     at=f'2026-09-{day:02d}T10:00:00+08:00', score=1, max_score=1,
                     confidence='sure', source_type='self_authored', wrong_reasons=[],
                     question_fingerprint=f'content-{n}')
        event.update(updates)
        t.apply_record_event(self.state, event, self.curriculum)
        self.events.append(event)
        return self.state['topics'][event['topic_id']]['mastery'][event['skill']]

    def save(self):
        t.write_attempts(self.data / 'attempts.jsonl', self.events)
        t.save_state_bundle(self.data, self.profile, self.state, backup=False)

    def test_last_correct_answer_does_not_hide_weak_module(self):
        for n in range(6):
            record = self.add(n, 20+n%2, score=int(n >= 4))
        self.assertEqual('sufficient', record['measurement_status'])
        self.assertEqual('needs_practice', record['learning_status'])
        self.save()
        progress = self.cli('progress', '--today', '2026-09-22', '--limit', '31', '--json')
        self.assertIn(K, [r['topic_id'] for r in progress['weakpoints']])
        self.assertNotIn(K, [r['topic_id'] for r in progress['measurement_gaps']])
        plan = t.build_recommendation_payload(argparse.Namespace(data_dir=self.data, today='2026-09-22', subject='comprehensive', limit=71))
        self.assertTrue(next(r for r in plan['recommendations'] if r['topic_id'] == K)['urgent_review_due'])

    def test_correct_new_answers_cannot_erase_cross_day_measurement(self):
        for n in range(6):
            record = self.add(n, 20 if n < 3 else 21)
        self.assertEqual('pass_ready', record['status'])
        for n in range(6, 15):
            record = self.add(n, 21)
        self.assertEqual('pass_ready', record['status'])
        self.assertEqual(2, record['days'])
        self.assertEqual(15, record['distinct_items'])
        self.assertEqual(12, len(record['recent_evidence']))
        record = self.add(15, 22, score=0)
        self.assertEqual('needs_practice', record['learning_status'])
        self.assertNotEqual('pass_ready', record['status'])

    def test_english_context_evidence_survives_recent_window(self):
        for n in range(20):
            record = self.add(n, 20 if n < 5 else 21, topic_id=module_assessment.ENGLISH,
                              context_id='passage-a' if n < 5 else 'passage-b')
        self.assertEqual('pass_ready', record['status'])
        self.assertEqual(2, record['contexts'])
        self.assertEqual(2, record['days'])

    def test_policy_upgrade_replays_readonly_and_preserves_raw_logs(self):
        for n in range(15):
            self.add(n, 20 if n < 3 else 21)
        self.state['evidence_policy_version'] = 4
        for topic in self.state['topics'].values():
            for record in topic['mastery'].values():
                record.pop('measurement_items', None)
        self.save()
        before = {p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        _, rebuilt = t.load_profile_and_state(self.data, persist_pending=False)
        self.assertEqual('pass_ready', rebuilt['topics'][K]['mastery']['recognition']['status'])
        self.assertEqual(self.state['strategy'], rebuilt['strategy'])
        self.assertEqual(self.state['training_days'], rebuilt['training_days'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()})

    def test_mock_forms_are_disjoint_and_first_form_stays_stable(self):
        seen = set()
        for paper_id in mock_paper.PAPER_IDS:
            private = mock_paper.private_items(paper_id)
            identities = {q['question_fingerprint'] for q in private}
            self.assertEqual(75, len(identities))
            self.assertFalse(seen & identities)
            self.assertEqual(31, len({q['topic_id'] for q in private}))
            seen.update(identities)
            payload = mock_paper.public_payload(paper_id)
            self.assertEqual(paper_id, payload['paper_id'])
            self.assertEqual((71, 75), (payload['passages'][0]['start'], payload['passages'][0]['end']))
            self.assertNotIn('"answer"', json.dumps(payload))

    def test_exhausted_mock_pool_routes_to_practice(self):
        for n, paper_id in enumerate(mock_paper.PAPER_IDS):
            q = mock_paper.private_items(paper_id)[0]
            self.add(n, 20, item_id=q['item_id'], topic_id=q['topic_id'], question_fingerprint=q['question_fingerprint'])
        self.save()
        progress = self.cli('progress', '--today', '2026-09-22', '--json')
        self.assertEqual('quiz_prepare', progress['next_action']['mode'])
        task = next(x for x in progress['measurement_tasks'] if x['subject'] == 'comprehensive')
        self.assertFalse(task['available'])
        self.assertEqual('resources_unavailable', task['status'])

    def test_single_case_stock_does_not_loop_after_success(self):
        route = 'C04.CASE_MICROSERVICE'
        case = next(x for x in paper_practice.build_case_items() if x.get('topic_id') == route and paper_practice.eligible(x))
        self.add(0, 20, item_id=case['id'], topic_id=route, skill='application', subject='case',
                 score=25, max_score=25, complete=True, assessment_scope='case', question_fingerprint=None)
        self.save()
        plan = t.build_recommendation_payload(argparse.Namespace(data_dir=self.data, today='2026-09-22', subject='case', limit=71))
        self.assertNotIn(route, [r['topic_id'] for r in plan['recommendations']])
        resource = next(r for r in plan['resource_gaps'] if r['topic_id'] == route)
        self.assertEqual('insufficient', resource['resource_status'])
        self.assertEqual(1, resource['distinct_items'])
        explicit = self.cli('case-prepare', '--topic', route, '--today', '2026-09-22')
        self.assertFalse(explicit['selection']['fresh_item'])
        self.assertFalse(explicit['selection']['adds_independent_item'])
        self.assertEqual('insufficient', explicit['resource']['resource_status'])

    def test_single_case_failed_attempt_can_still_be_remediated(self):
        route = 'C04.CASE_MICROSERVICE'
        case = next(x for x in paper_practice.build_case_items() if x.get('topic_id') == route and paper_practice.eligible(x))
        self.add(0, 20, item_id=case['id'], topic_id=route, skill='application', subject='case',
                 score=0, max_score=25, complete=True, assessment_scope='case', question_fingerprint=None)
        self.save()
        plan = t.build_recommendation_payload(argparse.Namespace(data_dir=self.data, today='2026-09-22', subject='case', limit=71))
        self.assertIn(route, [r['topic_id'] for r in plan['recommendations']])


if __name__ == '__main__':
    unittest.main()
