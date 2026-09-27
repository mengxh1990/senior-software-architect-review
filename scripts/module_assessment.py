"""Module evidence, shared timestamp parsing, and bank capacity.

Note tags are deliberately not imported.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

ENGLISH = 'K22.ENGLISH_READING'


def parse_event_datetime(value: str) -> datetime:
    """ISO-8601 时间戳解析的唯一实现：Z 记 UTC，裸时间按本地时区。

    ``tutor.parse_datetime`` 包装本函数并补 TutorError 语义，保证证据日期
    口径在 skill_status / apply_record_event / evidence_summary 三处一致。
    """
    normalized = value.strip().replace('Z', '+00:00')
    parsed = datetime.fromisoformat(normalized)
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def first_success_dates(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """每题只计其最早一条确定成功证据的日期（§4 跨日口径的唯一实现）。

    身份键取 question_fingerprint 优先、item_id 兜底；复做旧题不新增日期，
    返回 identity → 最早成功日（ISO 日期）。
    """
    dates: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row.get('question_fingerprint') or row.get('item_id')
        if identity and row.get('at'):
            day = parse_event_datetime(row['at']).date().isoformat()
            dates[identity] = min(dates.get(identity, day), day)
    return dates


def requirements(topic_id: str) -> dict[str, int]:
    return {'distinct_items': 10 if topic_id == ENGLISH else 6,
            'days': 2, 'contexts': 2 if topic_id == ENGLISH else 0}


def evidence_summary(record: dict[str, Any], topic_id: str) -> dict[str, Any]:
    recent = record.get('recent_evidence', [])
    # Sampling history is independent of the rolling performance window.
    measured = list(record.get('measurement_items', {}).values()) if 'measurement_items' in record else recent
    # 跨日与材料口径：每个不同题只计其最早一条确定成功证据的日期（复做旧题
    # 不新增日期），阅读材料只按确定成功的作答计数；最早日期与 rebuild/repair
    # 的按时间序重放口径一致。
    qualified = [
        row for row in record.get('qualified_evidence', [])
        if isinstance(row, dict) and (row.get('question_fingerprint') or row.get('item_id')) and row.get('at')
    ]
    first_success_days = first_success_dates(qualified)
    contexts = {row['context_id'] for row in qualified if row.get('context_id')}
    days = set(first_success_days.values())
    req = requirements(topic_id)
    sufficient = len(measured) >= req['distinct_items'] and len(days) >= req['days'] and len(contexts) >= req['contexts']
    accuracy = sum(row['weighted_ratio'] for row in recent) / len(recent) if recent else None
    weak = bool(record.get('regression_active') or record.get('latest_ratio', 1) < .8
                or accuracy is not None and accuracy < .8)
    status = ('unmeasured' if not record.get('attempt_count') else 'needs_practice' if weak
              else 'sample_ready' if record.get('status') == 'pass_ready' else 'evidence_insufficient')
    return {'scope': 'module_sample', 'learning_status': status,
            'measurement_status': 'sufficient' if sufficient else 'partial' if measured else 'unmeasured',
            'distinct_items': len(measured), 'recent_weighted_accuracy': accuracy, 'days': len(days), 'contexts': len(contexts), 'requirements': req}


def inventory(pool: list[dict[str, Any]], curriculum: dict[str, Any]) -> dict[str, Any]:
    identities, contexts = defaultdict(set), defaultdict(set)
    for item in pool:
        if item.get('classification_status') != 'reviewed' or item.get('quality_status') != 'ready' or item.get('teaching_status') != 'ready':
            continue
        topic = item['candidate_topics'][0]
        identities[topic].add(item['question_fingerprint'])
        if item.get('context_id'):
            contexts[topic].add(item['context_id'])
    rows = {}
    for t in curriculum['topics']:
        if not t['id'].startswith('K'):
            continue
        tid = t['id']; req = requirements(tid)
        count = len(identities[tid]); context_count = len(contexts[tid])
        rows[tid] = {'distinct_items': count, 'contexts': context_count, 'requirements': req,
                     'resource_status': 'missing' if not count else 'insufficient' if count < req['distinct_items'] or context_count < req['contexts'] else 'available'}
    return rows


def route_inventory(items: list[dict[str, Any]], curriculum: dict[str, Any]) -> dict[str, Any]:
    """Capacity for independently scored full cases/essays; aliases count once."""
    identities = defaultdict(set)
    for item in items:
        identities[item['topic_id']].add(item.get('canonical_item_id') or item['id'])
    rows = {}
    for topic in curriculum['topics']:
        tid = topic['id']
        if not tid.startswith(('C', 'P')):
            continue
        minimum = 2 if tid.startswith('C') else 1
        count = len(identities[tid])
        rows[tid] = {'distinct_items': count, 'requirements': {'distinct_items': minimum},
                     'practice_available': count > 0,
                     'resource_status': 'missing' if not count else 'insufficient' if count < minimum else 'available'}
    return rows
