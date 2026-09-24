# 私教进度协议 v2

本协议规定“学到哪里”如何被记录和判断。它是教学系统的事实来源，禁止用资料阅读量、聊天轮数或一次答对冒充掌握度。

## 1. 不可变目标

- 合格线：综合、案例、论文**分别 ≥ 45/75**，不能求平均。
- 安全目标：日常测量以 **52/75** 为默认目标，可在 50–55 范围调整。
- 优化目标：最大化三科同时过线概率，而不是最大化总分。
- 分配原则：先补瓶颈科目，再选该科收益最高的考点。

## 2. 私有文件

`.study/` 是每位考生的私人状态目录，必须被 Git 忽略：

| 文件 | 用途 |
|---|---|
| `profile.json` | 考期、每日时间、匿名技术背景 |
| `state.json` | 可重建的当前状态与三科证据摘要 |
| `attempts.jsonl` | 只追加的原始作答事件 |
| `postmortems.jsonl` | 只追加的考后错因补充，与可信交卷记录关联 |
| `question-registry.json` | 私有自编题登记、变式来源与内容指纹 |
| `dashboard.md` | 便于人阅读的进度面板 |
| `paper-project.md` | 论文项目素材，可能含敏感信息 |

不得把 `.study/` 内容复制到公共题库、范文、提交记录、Issue 或 PR。脚本默认离线运行。

## 3. 稳定考点 ID

`past-papers/` 中的原始 `§N.M` 标签用于来源追溯，但个别历史标签与最新版大纲存在漂移，因此不能直接作为学习进度主键。

[`curriculum.json`](./curriculum.json) 定义稳定教学 ID：

- `Kxx.*`：跨科知识考点；
- `Cxx.*`：案例赛道；
- `Pxx.*`：论文主题。

原标签保存在 `raw_tags`，题目可有一个主考点和多个次考点，但只有稳定 ID 更新进度。

## 4. 三维掌握度

同一知识点分别记录：

| 维度 | 对应能力 | 主要证据 |
|---|---|---|
| `recognition` | 识别、回忆、选择 | 综合选择题、口头闪卡 |
| `application` | 分析、计算、选型、踩关键词 | 案例题、小型架构判断 |
| `production` | 独立组织项目实践并论证 | 论文提纲、段落、完整限时作文 |

考点状态：

```text
unseen → learning → fragile → pass_ready
```

`due` 是按能力维度计算的排课标记，不是掌握状态；识别题到期不能污染同一考点的案例或论文能力。`strategic_skip` 是学习计划决策，也不能伪装成掌握。

最低稳定证据：

- `recognition`：至少 6 个不同内容的确定作答证据，并在不同日期独立答对至少两次；`unsure`、`guess` 不能满足稳定门槛。同内容不同来源 ID 只计一个独立证据。排课数值按最近 12 个不同内容的作答计算，把握度权重 sure=1、unsure=0.5、guess=0；原始得分另行保留。
- `application`：至少两次独立完整案例（`assessment_scope=case`、`complete=true`、25 分制）达到 15/25，间隔至少 48 小时；术语或小问片段不能证明整个案例赛道已准备好。
- `production`：至少一次按 75 分制估分、记录实际用时、显式标记完整、实际用时不超过 120 分钟且不少于 2500 字的作文达到安全线；只写摘要或提纲不能标记 `pass_ready`。

## 5. 作答事件

每次有效作答向 `attempts.jsonl` 追加一个事件：

```json
{
  "attempt_id": "a-20260810-k19-001",
  "topic_id": "K19.ATAM_TACTICS",
  "item_id": "exam-bank/12-atam-evaluation.md#3",
  "at": "2026-08-10T20:30:00+08:00",
  "subject": "comprehensive",
  "skill": "recognition",
  "mode": "practice",
  "score": 1,
  "max_score": 1,
  "duration_seconds": 42,
  "confidence": "unsure",
  "response_state": "answered",
  "selected_answer": "B",
  "correct_answer": "B",
  "wrong_reasons": [],
  "wrong_reason_source": null,
  "source_type": "self_authored",
  "feedback_seen": false
}
```

要求：

- `attempt_id` 全局唯一；重复写入必须幂等，内容冲突必须拒绝。
- `response_state` 只有 `answered` 与 `conceded` 两种取值，缺失时按 `answered` 处理。`conceded` 表示考生明确表示"不会"，是有效的负向知识证据：得 0 分、错因固定为 `knowledge_gap`、`confidence` 为 `null`，并进入 1/3/7/14/30 复习阶梯；它**不**等于猜测，也不允许用伪造的错误选项代替。完全没有回应（既没答也没说不会）不写事件。
- 普通答错只能在考生明确说明时记录 `wrong_reasons`，并标记 `wrong_reason_source=learner`；未说明时保持空数组并作为 `unclassified` 统计，不得默认推断为 `concept_confusion`。`knowledge_gap`（明确不会）和 `guessed_correct`（明确猜测）属于可由输入直接确认的事实。
- 题目本身无效（缺关键图表、答案不在选项中、题干被解析污染）时不写事件：判分时用 `--invalidate` 排除该题，只对有效题记档；题库疑点用 `--audit` 写入 `.study/quiz-audit-queue.jsonl`，由独立维护任务处理。
- `item_id` 必填并稳定标识一道独立题目；不得用新的 `attempt_id` 兜底。复做同一 `item_id` 可以验证遗忘，但不能冒充多个独立掌握证据。
- 自编题先登记题干、选项、稳定大考点 `topic_id` 和内容指纹；同内容换 ID 不得作为新证据。后续练习用 `variant_of` 关联来源题。
- 变式题的作答同样是有效证据：由 `quiz-variant-grade` 写成 `recognition` attempt，`attempt_id` 为 `<quiz-id>-v-<序号>`，`variant_of` 指回产生它的原题；未声明把握度时默认记为 `sure`，明确标注 `unsure` 或 `guess` 时按标注记录。明确表示不会仍按 `conceded` 处理，`confidence` 为 `null`。
- `source_type` 只能明确标记为 `official_outline`、`real`、`recalled_real`、`self_authored` 或 `simulation`，不得把模拟题称为真题。
- 用户输入无效、尚未回答或只阅读讲解时，不写掌握证据。
- 案例和论文记录原答、rubric 版本、逐项得分与作答依据，并明确为 AI 估分，不能冒充官方成绩。完整案例或论文通过 `record --assessment-file` 读取私人 JSON；逐点评分合计必须等于总得分和满分。完整论文字数从原答去空白后的字符数计算，不能仅由调用者声明。案例 `assessed_topics` 只能关联本赛道覆盖的 K 考点，并保存各点得分、满分、作答依据；同一份作答不增加额外独立事件或重复用时。
- 机考页面必须先保存原始答卷，再把考生选择的错因追加到 `postmortems.jsonl`；错因补充不得反向改写原始得分。

推荐错因枚举：

```text
knowledge_gap, recall_failure, concept_confusion, misread,
calculation, application, missing_keyword, weak_tradeoff,
weak_project_detail, no_metric, expression, time_management,
careless, guessed_correct
```

`knowledge_gap` 同时用于"明确不会"的 `conceded` 事件：它表示这个概念还没建立，而不是读题失误或计算错误。

## 6. 三科分数证据

进度面板分别显示：

- `latest_mock_score`：最近一次同科限时成绩；
- `predicted_score`：基于近期同条件证据的保守中心值；
- `lower_bound_score`：用于排课的保守下界；
- `evidence_level`：`cold_start / low / medium / high`；
- `status`：`unmeasured / danger / near / safe`。

没有完整限时证据时不得输出精确预测。单次小测只能用于考点诊断，不能直接外推 75 分制总成绩。

每条模考证据必须使用官方 75 分制，并记录全局唯一 `mock_id`、稳定 `paper_id`、实际用时、完成时间、来源类型和 `complete=true`。重复的 `mock_id` 只能幂等重放；内容冲突必须拒绝。未做完整的卷子、单题小测和归一化的 `1/1` 都不能计为模考。CLI 与网页共用测量资格：同卷复测、超过当前训练时限（综合 150 / 案例 90 / 论文 120 分钟）、已练过的测量题不增加独立模考证据；成绩仍保存并标明原因。完整论文事件自动关联科目测量，不必重复登记一份 mock。未知试卷的曝光情况无法自动核验，应由教练确认独立条件。

建议证据等级：

- 0 次完整限时：`cold_start`；
- 1 次：`low`；
- 2 次：`medium`；
- 至少 3 次合格独立测量：`high`。

这些等级不是通过概率。分数下界采用启发式余量，不是统计置信下界；最近有效测量超过 7 天显示 `needs_remeasurement`。`measurement_tasks` 返回待安排的完整测量；有足够当日整块时间时统一路由进入 `mock_manual_flow`。

## 7. 排课优先级

存在完整模考时，先把该场逐题事件与 `postmortems.jsonl` 合并，按稳定大考点形成薄弱点队列：模考失分 → 到期跨日复测 → 猜对或不确定题 → 通用考点排序。同日已展示的题目不得重复；同一大考点的补练只作为该考点证据，不宣称证明原题对应的具体知识已纠偏。

先确定科目瓶颈，再确定考点：

```text
subject_gap = clamp((safe_target - lower_bound_score) / 10, 0.25, 1.5)
need = max(1 - mastery, 0.4 × review_due)

priority =
  subject_gap
  × need
  × evidence_confidence
  × (frequency + quick_win + cross_subject_value)
  ÷ max(estimated_minutes / 15, 1)
```

公式允许实现细节调整，但必须保持以下不变量：

1. 未测量科目优先获得诊断，而不是被忽略。
2. 任一科低于 45 时，不继续给强科刷高分。
3. 到期高频错题排在未学低频难点之前。
4. 高频薄弱项排在低频同等薄弱项之前。
5. 已跨日稳定的内容降权，只做周期维护。
6. 同一科最长 3 天不能完全没有练习。
7. 考前不足 3 天停止低频新课，只看保命卡、错题和答题骨架。

仅在不存在尚未诊断或危险科目的优先任务时，案例应用能力到期且尚未 `pass_ready` 才可先于普通科目分配进入案例赛道；
已 `pass_ready` 的到期项只做周期维护，不单独触发跨科切换。大考点补练优先选择未做且通过质量门禁的题目。

启用科目按风险需求归一化，禁止因论文暂停而把另两科强制均分。危险/未测量科优先；完成该科至少 5 分钟训练后可插入逾期维护，全部科目均安全时优先三天维护，再处理到期案例。平局依据当日已训练用时调整；全部暂停返回 `await_explicit_request`。

每日预算优先使用实际用时；缺失时客观题估计 1 分钟、完整案例 25 分钟、片段 5 分钟，并显式显示估计部分，不能冒充实际计时。预算用尽返回 `session_complete`，不自动创建续练。剩余时间不足完整案例时返回 `targeted_fragment`。用户显式要求继续或指定训练可覆盖自动预算。

## 8. 过线最短路径

### 综合知识

优先顺序来自 431 道结构化历年题：

1. §4 软件工程；
2. §6 系统架构；
3. §1 计算机系统；
4. §7 质量属性；
5. 知识产权、英语、数据库等快分点。

低频且耗时内容可战略放弃，但必须明确记录原因。

### 案例分析

- 架构评估 / ATAM 作为高复用默认候选，不假定必考；
- 再从数据库、消息缓存、微服务中依据诊断选择，总计保留 1–3 条个人赛道；
- 诊断后用 `configure` 持久化路线，推荐器不得继续平均铺开所有题型；
- 用“结论 → 理由 → 技术名词/量化”组织答案；
- 每周至少一次三题或等价强度的限时证据。

### 论文

- 只维护一个匿名化、内部一致的项目素材；
- 按历史频率 × 项目适配度 × 表达能力选 1–3 个主题，并保存为个人路线；
- 提纲和摘要用于训练，但只有完整限时作文才能证明过线准备度。

## 9. 间隔复习

答错或脆弱题默认安排：当天变式，并按相邻间隔 1 天、3 天、7 天、14 天、30 天推进。当天变式答对只完成纠偏，不能取消次日复测；提前练习不能把已有到期日向后推；只有到期且确定答对才进入下一间隔。阶梯阶段单独保存为 `review_interval_days`，提前作答不修改阶段基准。再次答错、明确不会或猜测后重置到 1 天。

以下情况立即重置为脆弱：

- 原题会做但变式不会；
- 靠猜测答对；
- 只会术语但案例无法应用；
- 论文只有模板，没有自洽项目细节或量化效果。

## 10. 写入与恢复

- 每位考生目录使用独占锁，覆盖完整的读取、幂等判断、事件写入、状态派生和面板更新，禁止并发读改写丢进度。
- `attempts.jsonl` 是事实来源：先原子提交完整事件，再派生 `state.json`。若中途退出，只读命令只在内存中投影未回放事件，不改私人文件；下次有效状态写入时确定性回放并持久化，`doctor` 会提示磁盘尚未同步，绝不能用重试参数伪造缺失事件。
- 所有替换采用“同目录临时文件 → 内容校验 → `fsync` → 原子替换 → 同步父目录”。建档时即创建有效 `state.json.bak`。
- 遇到空文件、截断 JSON、嵌套字段类型错误、重复 ID 或状态/日志集合不一致时，停止正常写入并报告。
- 修复以事件日志重建状态；先复制保留损坏主文件为 `.corrupt.*`，再原子替换主状态。即使主状态缺失，也允许从日志恢复。
- 已核实的历史真题关联错标使用 `repair --normalize-question-links` 一次性纠正：
  保留 `attempts.jsonl` 原始内容，先在 `.study/migration-backups/` 备份，只重算受影响
  考点并更新旧会话的派生题目标签。未完成迁移时不得继续基于旧映射自动排课或机考交卷。

## 11. 题目身份、隔离与训练阶段

- 自编公共题通过 `scripts/exam_bank_topics.json` 保存逐题主考点，普通出题、变式、模考共用；同文件不再自动继承所有考点。来源 ID 保留，内容指纹用于去重与独立证据。
- 紧急考点优先于凑满题数；当日去重、内容去重、14 天已掌握题冷却及质量隔离不放松。题量不足返回短组，空池返回资源不足，纯进度仍可读取。
- `--invalidate` 自动留存审计并隔离，变式同样支持；维护核验后用 `release-question --item-id ... --evidence ...` 放行当前内容版本。改变内容后原版本批准不自动适用于新版本。`--audit` 的未证实疑点与确定无效分开记录。
- 基础诊断覆盖作为 `diagnostic_coverage` 返回。核心考点已覆盖、没有紧急综合复习且当日综合已练至少 10 分钟时，进入 `mixed_check`，通过 `quiz-prepare --mixed` 跨考点抽题；它不作为整卷成绩。
- 一轮最多 2 道即时补练，受剩余预算约束；猜对、不确定答对也可以补练。变式仍只证明对应稳定考点的表现，不能宣称精确纠正原题的知识缺口。
- 考前 3 天自动排课不再开未学考点；没有已有考点可练时返回 `survival_review`，主观科目只复习已学骨架。
- 判分提交后，续练失败通过 `preparation_status=failed` 和 `preparation_error` 返回；本轮成绩必须保留。修复或重试续练不得重复记分。
- `repair --dry-run` 只输出当前规则重算的差异；确认后 `repair --recompute-derived` 备份并更新派生状态，原始日志不变。缺失的历史原答、rubric、完整案例标志不能补造。
