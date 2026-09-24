# 高级软件架构师过线私教

这个目录把原来的静态复习资料变成一个“知道你学到哪里、下一步只练最值钱内容”的私人老师。

它的目标不是让你拿高分，而是让**综合知识、案例分析、论文三科分别稳定达到 45 分**。日常以 50–55 分作为安全余量。

## 30 秒开始

### Codex

1. Clone 本仓库并在 Codex 中打开仓库目录。
2. 直接说：`开始私教，我只求考过。`
3. Codex 会读取根目录 `AGENTS.md`，建立仅保存在本地 `.study/` 的私人档案。

### Claude Code

打开仓库后说：`使用 senior-architect-pass-coach，开始私教。`

### 其他 AI

把 [通用启动提示词](./prompts/generic-chat.md) 与本仓库一并提供给 AI。普通聊天产品无法直接写本地文件时，让它在每次结束时输出状态更新，下次对话重新上传自己的私人状态。

## 本地考试终端

Agent 始终是学习入口；网页只负责提供接近正式机考的作答体验，不承载聊天、模型调用或 API Key。由 Agent 或考生在仓库根目录启动：

```bash
python3 scripts/serve.py
```

然后打开 <http://localhost:8420>。考试页提供 75 题答题卡、150 分钟倒计时、标记与拿不准状态、刷新恢复、两次确认交卷和考后错因反馈。原始答卷与错因只写入本机 `.study/`，当前 Agent 可直接读取并据此安排下一轮训练。
如果页面提示历史题目关联需迁移，先由 Agent 运行 `python3 scripts/tutor.py repair --normalize-question-links`；迁移前页面不会开放试卷或接受交卷。学习航图的“下一组可执行练习”按实际质量与去重门禁预览，不创建答题会话。

## 它会记住什么

- 距离考试还有多久、每天能学多少分钟；
- 综合、案例、论文三科各自的真实测量证据；
- 每个稳定教学考点在“识别 / 应用 / 产出”三种能力上的掌握度；
- 哪些题是猜对的、错因是什么、何时需要重做；
- 案例主赛道和论文匿名项目素材。

私人数据默认写入：

```text
.study/
├── profile.json        # 考期、时间预算、匿名背景
├── state.json          # 三科状态与考点掌握度
├── attempts.jsonl      # 只追加的作答证据
├── postmortems.jsonl   # 只追加的考后错因补充（按需创建）
├── question-registry.json # 私有自编题与内容指纹
├── quiz-sessions/      # 客观题题面、答案键与幂等判分状态
├── dashboard.md        # 人类可读进度面板
└── paper-project.md    # 论文匿名项目素材（按需创建）
```

整个 `.study/` 已被 Git 忽略。不要使用 `git add -f .study`，不要把真实公司、客户、系统或个人信息写入公共仓库、Issue 或 PR。

## 教师 & 运行时文档

- [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md) — 教师人格与决策规则（诊断 / 案例 / 论文全流程）
- [`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md) — 记档规则、证据分级、隐私边界
- [`quiz-loop-sop.md`](./quiz-loop-sop.md) — 客观题一轮"出题→作答→判分→记档"的运行时管道
- [`topic-map.md`](./topic-map.md) — 考点↔资源映射表（脚本自动生成，请勿手改）
- [`../scripts/tutor.py`](../scripts/tutor.py) — 私人进度 CLI（含 `quiz-prepare` / `quiz-grade` 一体化客观题循环，以及只读的 `weakpoints` 薄弱点排名）
- [`../scripts/sanitize_bank.py`](../scripts/sanitize_bank.py) — exam-bank 答案脱敏器与题目质量门禁（题库维护、抽查与人工修复用，不在答题循环内调用）
- [`frequency-snapshot.json`](./frequency-snapshot.json) — 双层考频审计快照；覆盖率达标前只报告、不替换运行时权重
- [`../scripts/build_frequency_snapshot.py`](../scripts/build_frequency_snapshot.py) — 从质量门禁后的真题生成并校验考频快照
- [`../scripts/gen_topic_map.py`](../scripts/gen_topic_map.py) — 由 `curriculum.json` 重新生成 `topic-map.md`

## 常用说法

| 你说 | 私教会做 |
|---|---|
| `开始私教` | 建档或恢复进度，安排轻量诊断 |
| `今天学什么？` | 先读取只读进度汇总，再按统一路由直接开始最高收益任务 |
| `来 10 道题` | 从到期错题和高频薄弱点出题 |
| `复习错题` | 按遗忘节奏做变式与跨日复测 |
| `练案例` | 选择 ATAM 或已确定的案例主赛道；作答后按得分点估分、解析，并给出逐问参考标准答案 |
| `练论文` | 围绕一个匿名项目练选题、摘要、提纲或整篇 |
| `模拟考试` | 独立计时并记录真实分数证据 |
| `看看进度` | 只读显示三科风险、证据等级、薄弱 Top 5 和下一步，不创建答题会话 |
| `今天收工` | 写入本次证据并安排下次复习 |

斜杠形式 `/start`、`/today`、`/quiz`、`/review`、`/case`、`/essay`、`/mock`、`/status`、`/done` 只是别名；自然语言是主入口。

## 可选的本地状态工具

智能体会在后台调用标准库脚本，也可以手动使用：

```bash
# 建档
python3 scripts/tutor.py --data-dir .study init \
  --exam-date 2026-11-01 --daily-minutes 45

# 查看三科状态
python3 scripts/tutor.py --data-dir .study status

# 一次只读获取三科状态、薄弱 Top 5、到期项和下一步
python3 scripts/tutor.py --data-dir .study progress --limit 5 --json

# 推荐下一项
python3 scripts/tutor.py --data-dir .study recommend

# 一次完成诊断、选题与脱敏
python3 scripts/tutor.py --data-dir .study quiz-prepare \
  --subject comprehensive --limit 5

# 一次完成案例路由、盲练真题选取、去重与插图完整性检查（只读）
python3 scripts/tutor.py --data-dir .study case-prepare

# 显式指定案例应用考点时仍通过 curriculum 解析题型，不手写题型映射
python3 scripts/tutor.py --data-dir .study case-prepare \
  --topic K25.RELIABILITY_ENGINEERING

# 返回的 contexts 是题目共享上下文（例如英语阅读短文）；呈现时在关联题目前展示一次，
# 不需要、也不应从题库另行查找原文。

# 一次完成判分、批量记档与状态更新
python3 scripts/tutor.py --data-dir .study quiz-grade \
  --quiz-id <quiz-id> --answers 'C,A,D,BD,B'

# 只有考生明确说明时才记录错因；未提供的答错保持 unclassified
python3 scripts/tutor.py --data-dir .study quiz-grade \
  --quiz-id <quiz-id> --answers 'C,B,A,D,B' \
  --wrong-reason '2=recall_failure;5=misread'

# 考生明确说“不会”时用 X（0 分、记为 knowledge_gap，不需要补答）
python3 scripts/tutor.py --data-dir .study quiz-grade \
  --quiz-id <quiz-id> --answers 'C,B,A,X,B'

# 坏题排除出本组（不记证据）；答案键存疑则标记给维护任务
python3 scripts/tutor.py --data-dir .study quiz-grade \
  --quiz-id <quiz-id> --answers 'C,B,A,X,B' \
  --invalidate '4=missing_required_table' --audit '3=答案键疑似有误'

# 常见别名会规范化为稳定原因，例如 incomplete_stem -> unclear_stem，
# 避免教学回合因记忆枚举名称而失败重试。

# 查看最近完整模考暴露的具体薄弱点
python3 scripts/tutor.py --data-dir .study diagnose --subject comprehensive

# 只读查看到期、近期正确率与未覆盖考点（答题循环内的薄弱点入口）
python3 scripts/tutor.py --data-dir .study weakpoints \
  --subject comprehensive --days 21 --limit 10

# 自编题先登记题干、选项和内容指纹
python3 scripts/tutor.py --data-dir .study register-question \
  --file .study/new-question.json

# 诊断后固定个人路线（案例 1–3 条、论文 1–3 个主题）
python3 scripts/tutor.py --data-dir .study configure \
  --case-track C01.CASE_ATAM --case-track C02.CASE_DATABASE \
  --essay-theme P01.ESSAY_ARCHITECTURE \
  --skip-topic 'K07.REALTIME_EMBEDDED=考前低收益，暂时只保留保命卡'

# 学科启停策略：论文只在考生明确要求时训练
python3 scripts/tutor.py --data-dir .study configure \
  --subject-policy essay=manual_trigger --subject-policy-reason '考生要求主动触发'

# 记录一次作答（示例）
python3 scripts/tutor.py --data-dir .study record \
  --topic K19.ATAM_TACTICS --skill recognition \
  --score 4 --max-score 5 --attempt-id demo-atam-001 \
  --item-id exam-bank/12-atam-evaluation.md#3 \
  --source exam-bank/12-atam-evaluation.md

# 完整限时论文证据（缺少完整性、用时或 2500 字均不会计为过线）
python3 scripts/tutor.py --data-dir .study record \
  --topic P01.ESSAY_ARCHITECTURE --skill production \
  --score 52 --max-score 75 --attempt-id essay-2026-08-10-001 \
  --item-id essay-prompt-architecture-001 --mode full_timed \
  --duration-seconds 7200 --word-count 2700 --complete

# 记录一科限时模考
python3 scripts/tutor.py --data-dir .study mock \
  --subject comprehensive --mock-id 2026-08-10-comp-001 \
  --paper-id 2025上 --score 48 --max-score 75 \
  --duration-minutes 140 --complete

# 检查内容、状态与隐私设置
python3 scripts/tutor.py --data-dir .study doctor

# 代码升级后按原始事件重算复习日期和派生统计；attempts.jsonl 不变
python3 scripts/tutor.py --data-dir .study repair --recompute-derived

# 检查双层考频快照是否与当前题库一致
python3 scripts/build_frequency_snapshot.py --check
```

脚本不联网、不上传数据、没有第三方依赖。状态格式、掌握判据和排课公式见 [进度协议](./PROGRESS_PROTOCOL.md)。

## 第一版边界

- 历年卷用于统计考频，但其中部分是回忆版或缺失题，不直接全部纳入自动判分。
- 自动训练优先使用通过质量与讲解门禁的历年真题，自编题作为补充。
- 案例与论文分数只能称为“AI 估分”，并必须展示评分依据。案例完成作答后还必须提供逐问“标准答案（参考）”；主观题答案以核心采分点为准，不宣称存在唯一官方文字答案。
- 没有完整限时证据时，只显示“待诊断/低置信度”，不制造精确通过率。
- 使用必填的稳定 `item_id` 追踪独立题目；每题只映射到稳定大考点，保留题目来源和内容指纹。

## 维护者入口

- 智能体规范：[`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md)
- 高频课程表：[`curriculum.json`](./curriculum.json)
- 进度协议：[`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md)
- 公共模板：[`templates/`](./templates/)

运行验收：

```bash
python3 -m unittest discover -s tests -v
```
