"""Two-level domain/module classification, independent of optional note tags.

The historical module name is retained for callers; only legacy event migration
may consult old tag ownership. Active mappings always name a K module directly.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'tutor/curriculum.json'
QUESTION_PATH = ROOT / 'scripts/question_topics.json'
SUBJECTIVE_PATH = ROOT / 'scripts/subjective_topics.json'
NOTE_TAG_PATH = ROOT / 'tutor/note-tags.json'
TAXONOMY_VERSION = 2


@lru_cache(maxsize=16)
def _read(path: str, modified: int) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if payload.get('schema_version') != 1:
        raise ValueError(f'不支持的分类版本：{path}')
    return payload


def read(path: Path) -> dict[str, Any]:
    return _read(str(path), path.stat().st_mtime_ns)


def content_fingerprint(stem: str, options: list[str]) -> str:
    def normalized(value: str) -> str:
        return re.sub(r'\s+', ' ', value.strip()).casefold()
    payload = json.dumps([normalized(stem), *sorted(normalized(option) for option in options)],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def objective_metadata(item_id: str) -> dict[str, Any] | None:
    entry = read(QUESTION_PATH)['items'].get(item_id)
    return {**entry, 'taxonomy_version': TAXONOMY_VERSION} if entry else None


def subjective_metadata(item_id: str) -> dict[str, Any] | None:
    return read(SUBJECTIVE_PATH).get('items', {}).get(item_id)


def annotate_question(item: dict[str, Any]) -> dict[str, Any]:
    entry = objective_metadata(item['id'])
    fingerprint = content_fingerprint(item['stem'], [o['text'] for o in item['options']])
    if entry is None or entry.get('content_fingerprint') != fingerprint:
        return {**item, 'candidate_topics': [], 'classification_status': 'needs_review',
                'classification_reason': 'missing_mapping' if entry is None else 'content_changed'}
    return {**item, **entry, 'candidate_topics': [entry['topic_id']],
            'classification_status': 'reviewed', 'question_fingerprint': fingerprint}


def migrate_legacy_assessments(event: dict[str, Any]) -> dict[str, Any]:
    """One-way replay adapter for old fine-point rubrics; no active tag scoring."""
    legacy = event.get('assessed_knowledge')
    if not legacy:
        return event
    owners = read(ROOT/'scripts/legacy_knowledge_modules.json')['items']
    grouped = {}
    for row in legacy:
        owner = owners.get(row.get('knowledge_id'))
        if not owner:
            raise ValueError('旧评分点缺少可追溯模块，不能自动迁移')
        result = grouped.setdefault(owner, {'topic_id': owner, 'score': 0, 'max_score': 0, 'evidence': ''})
        result['score'] += row['score']
        result['max_score'] += row['max_score']
        result['evidence'] += str(row.get('evidence', '')) + '\n'
    if event.get('assessed_topics'):
        raise ValueError('旧记录含两套模块评分，需核对后迁移')
    return {**event, 'assessed_topics': list(grouped.values()), 'assessed_knowledge': []}


def project_event(event: dict[str, Any]) -> dict[str, Any]:
    """Project verified module corrections, preserving the original ledger."""
    if event.get('event_type') == 'mock':
        return event
    item_id = str(event.get('item_id') or '')
    result = dict(event)
    result.pop('knowledge_id', None)
    result.pop('secondary_knowledge_ids', None)
    if result.get('skill') in (None, 'recognition') and result.get('subject') in (None, 'comprehensive'):
        entry = objective_metadata(item_id)
        if entry:
            previous = result.get('question_fingerprint')
            if previous and previous != entry['content_fingerprint']:
                result['classification_reason'] = 'historical_content_changed'
                return result
            result.update(topic_id=entry['topic_id'], taxonomy_version=TAXONOMY_VERSION)
    entry = subjective_metadata(item_id)
    if entry and result.get('assessment_scope') in {'case', 'essay'}:
        result['topic_id'] = entry['topic_id']
        if not entry.get('complete'):
            result.update(assessment_scope='fragment', complete=False, mode='practice',
                          classification_reason='historical_source_not_measurable',
                          assessed_knowledge=[], assessed_topics=[])
    return result


def validate_catalog(curriculum: dict[str, Any]) -> list[str]:
    errors = []
    domains = {d['id'] for d in curriculum['domains']}
    modules = {t['id']: t for t in curriculum['topics'] if t['id'].startswith('K')}
    if len(domains) != len(curriculum['domains']):
        errors.append('知识域 ID 重复')
    for tid, module in modules.items():
        if module.get('domain_id') not in domains:
            errors.append(f'模块没有唯一有效知识域：{tid}')
    for ident, row in read(QUESTION_PATH)['items'].items():
        if row.get('topic_id') not in modules:
            errors.append(f'题目缺少唯一主模块：{ident}')
    return errors


def source_digest() -> str:
    paths = [CATALOG_PATH, QUESTION_PATH, ROOT/'scripts/quiz_quality_exclusions.json', ROOT/'scripts/sanitize_bank.py']
    paths += sorted((ROOT/'exam-bank').glob('[0-9]*.md'))
    paths += sorted((ROOT/'past-papers/comprehensive-by-year').glob('[0-9]*.md'))
    signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    return _source_digest(signature)


@lru_cache(maxsize=4)
def _source_digest(signature: tuple) -> str:
    digest = hashlib.sha256()
    for name, _, _ in signature:
        path = Path(name)
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
