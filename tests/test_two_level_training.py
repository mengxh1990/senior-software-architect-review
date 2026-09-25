"""The teaching runtime depends on modules, never on optional note tags."""
from __future__ import annotations
import contextlib
import copy
import hashlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tutor')]
import tutor as t
import knowledge_taxonomy as taxonomy
import module_assessment
import mock_paper
import build_frequency_snapshot

K = 'K21.MESSAGING_CACHE'


class TwoLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.curriculum = t.load_curriculum()
        cls.pool = t.load_quiz_question_pool(cls.curriculum)

    def cli(self, data, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = t.main(['--data-dir', str(data), *args])
        self.assertEqual(0, code, err.getvalue())
        text = out.getvalue()
        return json.loads(text) if text.lstrip().startswith('{') else text

    def state(self):
        return t.new_state(self.curriculum, '2026-09-01T09:00:00+08:00')

    def event(self, n, **updates):
        event = dict(attempt_id=f'two-{n}', event_type='practice', item_id=f'fixture:{n}',
                     topic_id=K, subject='comprehensive', skill='recognition', mode='practice',
                     at=f'2026-09-{23+n%2}T10:00:00+08:00', score=1, max_score=1,
                     confidence='sure', source_type='self_authored', wrong_reasons=[],
                     question_fingerprint=f'content-{n}')
        event.update(updates)
        return event

    def test_two_levels_only_in_curriculum_and_runtime(self):
        self.assertEqual(13, len(self.curriculum['domains']))
        modules = [m for m in self.curriculum['topics'] if m['id'].startswith('K')]
        self.assertEqual(31, len(modules))
        self.assertTrue(all('domain_id' in m and 'knowledge_ids' not in m for m in modules))
        self.assertTrue(all('topic_id' in item and 'knowledge_id' not in item for item in self.pool))
        self.assertNotIn('knowledge', self.state())

    def test_arbitrary_or_absent_tags_cannot_change_module_scores(self):
        expected, with_tags = self.state(), self.state()
        for n in range(6):
            t.apply_record_event(expected, self.event(n), self.curriculum)
            t.apply_record_event(with_tags, self.event(n, knowledge_id='UNKNOWN-OR-RENAMED', note_tags=['arbitrary']), self.curriculum)
        self.assertEqual(expected, with_tags)
        row = expected['topics'][K]['mastery']['recognition']
        self.assertEqual('pass_ready', row['status'])
        self.assertEqual('module_sample', row['evidence_scope'])
        self.assertEqual('sample_ready', row['learning_status'])

    def test_missing_note_files_do_not_change_selection_progress_frequency_or_grading(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.cli(data, 'init', '--exam-date', '2026-11-01', '--daily-minutes', '60')
            expected = self.cli(data, 'progress', '--today', '2026-09-26', '--json')
            frequency = build_frequency_snapshot.build_snapshot('2026-09-26')
            original_read = Path.read_text
            def deny_notes(path, *args, **kwargs):
                if path.name in {'note-tags.json', 'question-note-tags.json'}:
                    raise FileNotFoundError('optional notes deliberately absent')
                return original_read(path, *args, **kwargs)
            with patch.object(Path, 'read_text', deny_notes):
                self.assertEqual(expected, self.cli(data, 'progress', '--today', '2026-09-26', '--json'))
                self.assertEqual(frequency, build_frequency_snapshot.build_snapshot('2026-09-26'))
                quiz = self.cli(data, 'quiz-prepare', '--topic', K, '--limit', '5', '--today', '2026-09-26')
                manifest = json.loads(t.quiz_session_path(data, quiz['quiz_id']).read_text())
                self.assertTrue(all(q['topic_id'] == K for q in manifest['questions']))
                answers = ','.join(''.join(q['correct']) for q in manifest['questions'])
                result = self.cli(data, 'quiz-grade', '--quiz-id', quiz['quiz_id'], '--answers', answers,
                                  '--confidences', ','.join(['sure']*len(manifest['questions'])), '--at', '2026-09-26T12:00:00+08:00')
                self.assertEqual(5, result['score'])
                self.assertTrue(all('knowledge_id' not in row for row in result['results']))

    def test_module_quiz_crosses_note_tags_without_using_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); self.cli(data, 'init', '--exam-date', '2026-11-01')
            quiz = self.cli(data, 'quiz-prepare', '--topic', K, '--limit', '5', '--today', '2026-09-26')
            manifest = json.loads(t.quiz_session_path(data, quiz['quiz_id']).read_text())
            notes = json.loads((ROOT/'tutor/question-note-tags.json').read_text())['items']
            self.assertGreater(len({tuple(notes.get(q['item_id'], [])) for q in manifest['questions']}), 1)
            self.assertTrue(all('knowledge_id' not in q for q in manifest['questions']))

    def test_module_readiness_requires_independent_content_and_dates(self):
        for same_date, same_content in [(True, False), (False, True)]:
            state = self.state()
            for n in range(8):
                event = self.event(n)
                if same_date: event['at'] = '2026-09-23T10:00:00+08:00'
                if same_content: event['question_fingerprint'] = 'duplicate'
                t.apply_record_event(state, event, self.curriculum)
            row = state['topics'][K]['mastery']['recognition']
            self.assertNotEqual('pass_ready', row['status'])
            self.assertEqual('evidence_insufficient', row['learning_status'])

    def test_english_requires_two_reading_sources(self):
        state = self.state()
        for n in range(1, 6):
            t.apply_record_event(state, self.event(n, item_id=f'exam-bank/23-english-reading.md#{n}',
                question_fingerprint=None, topic_id=module_assessment.ENGLISH), self.curriculum)
        row = state['topics'][module_assessment.ENGLISH]['mastery']['recognition']
        self.assertEqual(1, row['contexts'])
        self.assertNotEqual('pass_ready', row['status'])
        for n in range(6, 11):
            t.apply_record_event(state, self.event(n, item_id=f'exam-bank/23-english-reading.md#{n}',
                question_fingerprint=None, topic_id=module_assessment.ENGLISH), self.curriculum)
        self.assertEqual('pass_ready', row['status'])
        self.assertEqual(2, row['contexts'])

    def test_legacy_multiple_point_rubrics_merge_to_one_module_event(self):
        event = self.event(0, subject='case', skill='application', topic_id='C02.CASE_DATABASE',
                score=20, max_score=25, complete=True, assessment_scope='case', duration_seconds=1500,
                assessed_knowledge=[{'knowledge_id':'DB.KEYS_FD','score':8,'max_score':10,'evidence':'a'},
                                    {'knowledge_id':'DB.NORMAL_FORMS','score':12,'max_score':15,'evidence':'b'}])
        old = copy.deepcopy(event)
        state = t.rebuild_evidence({'created_at':'2026-09-01T09:00:00+08:00'}, self.state(), [event])
        row = state['topics']['K10.DATABASE_MODELING']['mastery']['application']
        self.assertEqual(1, row['attempt_count'])
        self.assertEqual(20, row['score_sum'])
        self.assertEqual(1, state['subjects']['case']['evidence_count'])
        self.assertEqual(1500, state['training_days']['2026-09-23']['case'])
        self.assertEqual(old, event)
        self.assertNotIn('knowledge', state)

    def test_progress_route_and_read_only_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); self.cli(data,'init','--exam-date','2026-11-01','--daily-minutes','60')
            def hashes():
                return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
            before=hashes();result=self.cli(data,'progress','--today','2026-09-26','--json')
            self.assertEqual(before,hashes())
            self.assertEqual(31,len(result['module_progress']))
            self.assertNotIn('knowledge_progress',result)
            self.assertNotIn('training_progress',result)
            self.assertNotIn('--knowledge',result['next_action']['command'])
            self.assertNotIn('--unit',result['next_action']['command'])
            self.cli(data,*shlex.split(result['next_action']['command']),'--today','2026-09-26')

    def test_old_manifest_tags_and_route_flags_are_removed_without_changing_answers(self):
        item = t.quiz_question_for_topic(next(q for q in self.pool if q['candidate_topics']==[K] and q['quality_status']=='ready'), t.topic_map(self.curriculum)[K])
        old = {'questions':[{**item,'knowledge_id':'CACHE.PENETRATION'}],
               'next_quiz':{'next_action':{'command':f'quiz-prepare --topic {K} --knowledge CACHE.PENETRATION','knowledge_id':'CACHE.PENETRATION'}}}
        normalized=t.normalized_quiz_manifest(old,t.topic_map(self.curriculum))
        self.assertNotIn('knowledge_id',normalized['questions'][0])
        self.assertEqual(item['correct'],normalized['questions'][0]['correct'])
        self.assertNotIn('--knowledge',normalized['next_quiz']['next_action']['command'])
        self.assertIn('knowledge_id',old['questions'][0])


if __name__ == '__main__':
    unittest.main()
