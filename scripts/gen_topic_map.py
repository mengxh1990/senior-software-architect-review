#!/usr/bin/env python3
"""Generate the two-level curriculum and usable module coverage report."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import audit_taxonomy

REPO_ROOT = Path(__file__).resolve().parents[1]
CURRICULUM = REPO_ROOT/'tutor/curriculum.json'
OUTPUT = REPO_ROOT/'tutor/topic-map.md'


def render(curriculum: dict) -> str:
    report = audit_taxonomy.audit()
    counts = {row['topic_id']: row for row in report['topics']}
    lines = ['# 知识域与 K 模块分类', '', '> 自动生成：`python3 scripts/gen_topic_map.py`。数量来自已复核直接模块映射与出题门禁。', '',
             f"训练分类只有两层：{report['domains']} 个知识域 → {report['modules']} 个 K 模块。题目直接归属 K；C/P 是案例和论文练习入口。", '',
             '每个普通模块至少6道不同内容、跨2天、近期加权正确率≥80%才可能抽样达标。英语至少10题、2份可追溯阅读材料、跨2天。抽样达标不代表模块所有内容掌握。', '']
    for domain in curriculum['domains']:
        lines += [f"## {domain['id']} · {domain['name']}", '', '| K 模块 | 名称 | 自编可用题 | 历年可用题 | 不同题面 | 测量容量 |', '|---|---|---:|---:|---:|---|']
        for t in curriculum['topics']:
            if t.get('domain_id') != domain['id']:
                continue
            row = counts[t['id']]; resource = row['resource']
            label = {'available':'足够','insufficient':'不足','missing':'缺题'}[resource['resource_status']]
            lines.append(f"| `{t['id']}` | {t['name']} | {row['bank_ready']} | {row['past_ready']} | {resource['distinct_items']} | {label} |")
        lines.append('')
    lines += ['## 案例与论文练习入口', '', '| ID | 名称 | 完整可用题 | 独立测量容量 |', '|---|---|---:|---|']
    for t in curriculum['topics']:
        if not t['id'].startswith('K'):
            resource = counts[t['id']]['resource']
            label = {'available':'足够','insufficient':'不足','missing':'缺题'}[resource['resource_status']]
            lines.append(f"| `{t['id']}` | {t['name']} | {resource['distinct_items']} | {label}（至少{resource['requirements']['distinct_items']}题） |")
    lines += ['', '## 笔记标签与资源缺口', '',
              '原细知识点已改为可选笔记标签：`note-tags.json` 与 `question-note-tags.json`。没有标签成绩、覆盖率、复习队列或训练单元；标签增删不会改变训练结果。', '',
              '下面的缺口属于题库或材料，不代表考生薄弱：', '']
    for row in report['topics']:
        if row['resource'] and row['resource']['resource_status'] != 'available':
            lines.append(f"- `{row['topic_id']}`：{row['resource']['distinct_items']} 道不同题面，低于独立测量门槛（至少{row['resource']['requirements']['distinct_items']}题），需补充材料；已有题可练习，重复不增加独立题量。")
        elif not row['resource'] and not row['subjective_ready']:
            lines.append(f"- `{row['topic_id']}`：暂无完整盲练材料，自动排课暂不选择。")
    lines += ['', '容量尚未扣除个人当天曝光、冷却与隔离。详细报告：`python3 scripts/audit_taxonomy.py --json`。', '']
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    content = render(json.loads(CURRICULUM.read_text(encoding='utf-8')))
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding='utf-8') != content:
            print('error: tutor/topic-map.md is out of date; run scripts/gen_topic_map.py')
            return 1
        return 0
    OUTPUT.write_text(content, encoding='utf-8')
    print('wrote tutor/topic-map.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
