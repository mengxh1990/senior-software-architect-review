#!/usr/bin/env python3
"""Validate direct domain/K classification and gate-ready module capacity."""
from __future__ import annotations
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import knowledge_taxonomy as taxonomy
import module_assessment
import paper_practice
import tutor

ROOT = Path(__file__).resolve().parents[1]


def audit() -> dict:
    curriculum = tutor.load_curriculum()
    errors = taxonomy.validate_catalog(curriculum)
    pool = tutor.load_quiz_question_pool(curriculum)
    counts = defaultdict(Counter)
    fingerprints = defaultdict(set)
    bank_map = {}
    for item in pool:
        entry = taxonomy.objective_metadata(item['id'])
        if not entry or item.get('classification_status') != 'reviewed':
            errors.append(f"题目缺少已核验的模块分类：{item['id']}")
            continue
        tid = entry['topic_id']
        if item['candidate_topics'] != [tid]:
            errors.append(f"题目主模块不唯一：{item['id']}")
        fingerprints[entry['content_fingerprint']].add(tid)
        if item['id'].startswith('exam-bank/'):
            bank_map[item['id']] = tid
        if item.get('quality_status') == 'ready' and item.get('teaching_status') == 'ready':
            counts[tid]['bank' if item['id'].startswith('exam-bank/') else 'past'] += 1
    if any(len(ids) > 1 for ids in fingerprints.values()):
        errors.append('相同内容题目主模块不一致')
    if bank_map != taxonomy.read(ROOT/'scripts/exam_bank_topics.json')['items']:
        errors.append('exam_bank_topics.json 与直接模块映射不一致')
    subjective = paper_practice.build_case_items() + paper_practice.build_essay_items()
    if len(subjective) != len({item['id'] for item in subjective}):
        errors.append('主观题 ID 重复')
    route_counts = Counter()
    known = {t['id'] for t in curriculum['topics'] if t['id'].startswith('K')}
    for item in subjective:
        if item['practice_mode'] == 'answer_key':
            continue
        entry = taxonomy.subjective_metadata(item['id'])
        if not entry or not entry.get('covered_topic_ids') or not set(entry['covered_topic_ids']) <= known:
            errors.append(f"主观题缺少有效模块范围：{item['id']}")
            continue
        if 'classification_content_changed' in item.get('quality_issues', []):
            errors.append(f"主观题题面变化后未复核：{item['id']}")
        if entry.get('complete') != item.get('complete'):
            errors.append(f"主观题完整性标记需复核：{item['id']}")
        if paper_practice.eligible(item):
            route_counts[item['topic_id']] += 1
    capacity = module_assessment.inventory(pool, curriculum)
    capacity.update(module_assessment.route_inventory([item for item in subjective if paper_practice.eligible(item)], curriculum))
    rows = [{'topic_id': t['id'], 'name': t['name'], 'domain_id': t.get('domain_id'),
             'bank_ready': counts[t['id']]['bank'], 'past_ready': counts[t['id']]['past'],
             'subjective_ready': route_counts[t['id']], 'resource': capacity.get(t['id'])}
            for t in curriculum['topics']]
    return {'healthy': not errors, 'errors': errors, 'taxonomy_version': taxonomy.TAXONOMY_VERSION,
            'domains': len(curriculum['domains']), 'modules': len(known), 'objective_items': len(pool),
            'objective_ready': sum(sum(c.values()) for c in counts.values()),
            'subjective_ready': sum(route_counts.values()), 'topics': rows}


def write_bank_map() -> None:
    entries = taxonomy.read(taxonomy.QUESTION_PATH)['items']
    payload = {'schema_version': 1, 'description': '由 question_topics.json 直接模块映射生成。',
               'items': {ident: row['topic_id'] for ident, row in entries.items() if ident.startswith('exam-bank/')}}
    (ROOT/'scripts/exam_bank_topics.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--write-bank-map', action='store_true')
    args = parser.parse_args()
    if args.write_bank_map:
        write_bank_map()
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
          f"知识域 {result['domains']}；K模块 {result['modules']}；可用客观题 {result['objective_ready']}；可用主观题 {result['subjective_ready']}\n" + '\n'.join(result['errors']))
    return 0 if result['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
