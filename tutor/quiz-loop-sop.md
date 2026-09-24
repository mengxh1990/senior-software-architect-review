# 答题循环 SOP · Quiz Loop Runtime

> 出题 / 作答 / 判分 / 记档 / 反馈 / 换科 的运行时管道。
> 与 [`tutor/PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md)（记档规则）和
> [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md)（教师人格）
> 配合使用。本文只讲**怎么把一轮客观题跑完并落盘**，不覆盖案例/论文的完整训练循环
> （那部分在 coach 人格里）。

## 何时用

- 用户说：复习 / 学习 / 保分 / 过线 / 私教 / 继续答题 / 换科 / 出题 / 出下一题 / 再来一组
- 用户已答完一屏、等你判分和下一屏
- 用户明确要求"选择器点选式"作答

**不用**这套流程的场景：
- 纯“看看进度”或 `/status`：只调用 `python3 scripts/tutor.py progress --json`，不得创建 quiz session
- 只讲教材、不考核（passive lecture）
- 案例题、论文题的完整训练（用 coach 人格里的对应流程）
- `.study/` 损坏或缺失 → 先按 coach 人格里的建档步骤走

## 前置检查

每个新任务首次进入循环前必须过；同一任务后续答题轮不得重复执行：
- CWD 在本仓库根，`python3 scripts/tutor.py --help` 正常
- 已读 [`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md) 与
  [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md)
- `.study/` 存在（若无，先按 coach 人格建档）
- 已跑过 `python3 scripts/tutor.py doctor`，全部检查 PASS

## 回合预算与结束条件

- “今天学什么／安排训练”先调用一次只读 `progress --json`，再按其 `next_action` 调用对应训练入口；综合出题回合只调用一次 `quiz-prepare`，判分回合只调用一次 `quiz-grade`。只有命令失败且直接阻塞本轮时，才允许增加一次修复或诊断调用。
- **运行时白名单**：统一路由阶段只允许 `progress`，综合出题回合只允许 `quiz-prepare`，判分回合只允许 `quiz-grade`，变式回合只允许 `quiz-variant-grade`。不得用 `cat` / `rg` / `sed` 读取源码、题库、`quiz-sessions/*.json`、`state.json` 或知识库来"确认"答案，也不得手工改 manifest、伪造选项或要求考生补答"不会"的题。命令非零退出且直接阻塞本轮时，最多做一次只读诊断；仍失败就如实报告阻塞，改开维护任务。
- **上下文预算**：非题面输出保持简短，不把完整题库、候选池或状态全集带进教学线程；题目本身不受此限制。为了"确认"重复运行确定性命令也算违规。每个日历日用新任务从 `.study/` 恢复；同一任务一旦发生源码/题库排查，后续教学转到新任务，避免维护上下文跨日累积。
- `quiz-prepare` 每轮只调用一次，返回的即为已过质量门禁的题：直接展示，不得再自行复核、筛选、丢弃或为该题重跑命令。作答或判分时才发现残缺（题干被解析污染、缺图缺表、答案不唯一）的题，用 `--invalidate` 排除，本回合结束后另开维护任务。
- 题目质量在进入本循环前已由门禁判定：`quiz-prepare` 只会给出 `ready_for_quiz` 的题。若某题在作答后才发现残缺（例如依赖的表其实没给出），用 `--invalidate` 把它排除出本组，不要临场补内容；在本回合结束后，按 `item_id + 原因` 去重，新开独立维护任务尝试以权威原卷做最小修复。只有题干、必要材料和唯一答案均可可靠恢复时才能重新放行，否则维持拦截。
- 同一轮同时要求"看薄弱点 + 安排训练"时，使用一次 `progress --json` 获取三科状态、薄弱点和统一路由，再按 `next_action` 出题；两段之间不插入探索性调用。`weakpoints` 只用于明确要求某科详细排名的只读请求。
- 第一次调用必须批量收集推荐、到期错题、候选题、题目元数据和去重信息，不得按题逐次搜索。
- 非阻塞的诊断状态异常、元数据瑕疵或维护建议不得在训练回合内追查源码；记录后另开仓库维护任务处理。
- `quiz-grade` 成功返回即表示判分、原子记档和状态更新完成。收到整组答案后只调用一次 `quiz-grade`，判分前后都不得追加 `status` 或 `diagnose`；给出必要反馈后立即结束本轮。
- **路由锁定**：优先服从 `quiz-grade` / `quiz-variant-grade` 返回的 `next_action`。`await_variants` 只收变式答案；`quiz_prepare` 直接进入下一组；`case_prepare` 直接调用一次 `case-prepare`。除非用户明确改题型或命令失败，否则同一回合不得重新比较"继续综合还是切案例"。

## Step 1 · 一次准备整组题

开始新训练任务时先获取统一路由：

```bash
python3 scripts/tutor.py progress --json
```

只有 `next_action.mode=quiz_prepare` 时进入本客观题流程。沿用返回的
`next_action.command`，包括其 `--topic` 考点路由参数；同组客观题按该稳定考点组卷：

```bash
python3 scripts/tutor.py quiz-prepare --subject comprehensive --topic <next_action.topic_id> --limit 5
```

该命令在一个进程内完成逐题诊断、推荐、真题优先抽取、当天去重、细考点与
facet 解析，并把私有答案保存到 `.study/quiz-sessions/`。标准输出只包含可直接
展示的题干、选项和来源类型，不包含答案或解析。

同一份输出还带排课结论，直接展示、不要另外分析：

| 字段 | 用法 |
|---|---|
| `objective` | 一句话说明本组训练目标与依据，开场直接引用 |
| `evidence_summary` | 3–5 条证据（近期正确率、是否到期、本组覆盖哪些考点） |
| `days_left` / `daily_minutes` | 距离考试天数与每日可投入分钟数，用于控制任务体量 |
| `substitution`（可选） | 原细考点无可用同概念题时，本组只是同稳定考点替代练习；照实说明，不宣称完成原细考点复测 |

需要更细的薄弱点排名时才用一次只读 `weakpoints`；不要为了排课再手工统计。

## Step 2 · 展示题目

直接使用 `quiz-prepare` 返回的 `questions`；不要再次搜索题库或调用
`sanitize_bank.py`。以下脱敏规则由 `quiz-prepare` 内部执行。

若返回 `contexts`，按 `questions[].context_id` 在首次出现该 ID 前完整展示对应的
`contexts[].text`（例如英语阅读短文或已验证的关联题干）；同一 context 只展示一次。
没有上下文的题不得自行补写，质量门禁会在出题前过滤。

### 底层脱敏契约

exam-bank 的题目块结构：

```
### N. 题干（可能含 **加粗关键词**）

A. 选项文本

B. 选项文本

C. 选项文本

✅ **D. 正确选项文本**

**答案**：D
**解析**：……
---
```

`✅` / 整行 `**...**` / `**答案**：X` / `**解析**：` 都是内联的。**绝对不要直接把这段贴给用户**。

`quiz-prepare` 内部用 `scripts/sanitize_bank.py` 剥离（下面这条命令只是契约示例，不在答题循环内调用）：

```bash
python3 scripts/sanitize_bank.py exam-bank/07-software-engineering.md 1 4 6
```

输出是 JSON 数组，每项含 `stem` / `options[]` / `correct` / `explanation`。
`correct` 与 `explanation` **只**用于判分和作答后反馈，**不**在作答前回显。

### Step 2b · 真题抽题契约（由 `quiz-prepare` 内部执行）

`past-papers/comprehensive-by-year/` 里是 20 个考期的综合知识真题（1055 个可用题块），
**同一套脱敏契约**。`quiz-prepare` 已按"真题优先、自编题兜底"抽题并完成脱敏，
本节只说明它内部使用的契约；下面的命令仅用于题库维护、抽查与人工修复，
**不在答题循环内调用**。

真题是正式试卷，证据强度高于自编题，只要推荐器给出的考点能抽到真题，就优先出真题。

```bash
# 仅题库维护/抽查时使用
# 按 tutor 考点抽（推荐：直接对接 Step 1 的 recommend 结果）
python3 scripts/sanitize_bank.py --topic K10.DATABASE_MODELING --limit 5

# 指定考期 + §标签
python3 scripts/sanitize_bank.py --year 2013下 --tag §5 --limit 3

# 只看某份卷子
python3 scripts/sanitize_bank.py past-papers/comprehensive-by-year/2018下.md --tag §6 --limit 5

# 看各考期覆盖情况
python3 scripts/sanitize_bank.py --list
```

返回项比 exam-bank 多三个字段：

| 字段 | 含义 |
|---|---|
| `tag` / `tag_label` | 该题块的 §考点标签（2009–2017 为题组级，2018 起为逐题） |
| `range` | 原卷题号区间，如 `[7, 8]`；运行时只放行可作为**单个独立作答单元**呈现的题 |
| `candidate_topics` | 由 `curriculum.json` 的 `raw_tags` 推出的 tutor 考点编号，用于 `record --topic` |

抽题注意事项：

- `id` 形如 `past-papers/comprehensive-by-year/2013下.md#7-8`，**record 时原样作为 `--item-id`**；
- 一个题块含多个独立小问（如 `#7-8`）且尚未有逐小题选项、答案与记档模型时，会被质量门禁标记为 `multi_question_group` 并跳过；**不得**把多道题的答案压成一次作答或手工拆题。未来有结构化子题模型后再恢复。
- `--source-type` 按考期来源选择：2009–2022 用 `real`，回忆版考期（2023 下、2024 上/下、2025 上/下、2026 上）用 `recalled_real`；
- 真题保留试卷原始的答案分布，**不要**套用"自编题正确答案需分散到不同选项"的规则；
- 2019 下、2020、2023 下的整理版本只覆盖部分题目（26 / 12 / 1 个可用题块），抽不到时退回 `exam-bank/` 或自编题；
- 题干里的插图引用形如 `![p1_000.png](../assets/2013下/p1_000.webp)`，**呈现时不要贴图片路径**，用文字描述图意或直接说明"原题含图"。

### Step 2c · 案例与论文真题（按题型 / 主题抽题）

案例与论文的主观题也用真题。自动排案例时优先使用聚合入口 `case-prepare`：

```bash
# 自动复用当前推荐器、个人路线、去重和题面完整性门禁
python3 scripts/tutor.py case-prepare

# 用户明确指定案例考点时
python3 scripts/tutor.py case-prepare --topic K25.RELIABILITY_ENGINEERING
```

`case-prepare` 返回并锁定 `topic_id + case_type + item_id`，同时提供现存插图的
`figure_assets` 绝对路径、作答后 reveal 参数和 record 上下文。默认跳过缺图或图文件
不存在的题；只有用户明确接受缺图题时才加 `--allow-missing-figures`。收到成功结果后
不得再调用 `--list`、手工比较题型或搜索图片路径。

论文以及案例作答后的答案揭示仍使用 [`scripts/paper_practice.py`](../scripts/paper_practice.py)：

```bash
# 案例：按题型盲练（自动剥离参考答案）
python3 scripts/paper_practice.py --subject case --type 01 --limit 2
# 案例：作答后取参考答案
python3 scripts/paper_practice.py --subject case --year 2013下 --numeral 一 --reveal
# 论文：按主题取题干与小问
python3 scripts/paper_practice.py --subject essay --topic 06 --limit 4
# 看各题型可用量
python3 scripts/paper_practice.py --list
```

| 字段 | 含义与用法 |
|---|---|
| `practice_mode=blind` | 可以盲练：题干已与参考答案分离 |
| `practice_mode=read_only` | 题干与参考答案混排（多见于 2009–2018 答案详解转录版），**只能当研读材料，不要出给学员** |
| `practice_mode=answer_key` | 卷末答案区，工具已排除，不要当题目 |
| `missing_figure=true` | 该题插图在广告/水印清理时被移除，**仍然可以出题**：按 `figure_note` 用文字描述图意，或提示学员对照原始 PDF；只想出插图完整的题时加 `--skip-missing-figures` |
| `stem` 里的 `【图 N】` | 对应 `figures` 里的插图，呈现时**不要贴文件路径** |
| `source_type` | 直接作为 `record --source-type`（正式卷 `real` / 回忆版 `recalled_real`） |
| `answer_source` | 原卷题没有内嵌答案，作答后到这个路径对应的研读版文件取参考答案 |

用法要点：

- 案例题作答后跑 `--reveal` 取参考答案，按评分点逐项估分并标注"AI 估分"；
- `--item-id` 用输出的 `id`，`--skill application`（案例）/ `production`（论文成文）；
- 案例可盲练 79 道（2009 下–2017 下取自 `<考期>-原卷.md` 的无答案题干，2018 下起取自带答案的整理版）、论文 67 道；案例其余 62 道为题干与答案混排的卷子，只作研读与作答后对答案。不够时回退 [`case-types/`](../past-papers/case-types/) 的自编模拟题与 [`paper-topics/`](../past-papers/paper-topics/) 的仿真题。

## Step 3 · 批量出题

默认一次展示 5 题，学员统一作答后再统一判分和记档；冲刺时可按学员要求改为
10 题。每组尽量覆盖不同细考点，不为了凑题重复当天已纠偏的题型。

如果运行环境支持点选工具：

- 每次调用 ≤ 4 题（工具上限）
- `label` 只放选项字母：`"A"` / `"B"` / `"C"` / `"D"`（`label` 有 12 字符限制）
- `question` 放完整题干（含"下列错误的是"这类否定词）
- `options[].description` 放该选项的**完整文本**
- `header` 放很短的题目标签，如 `"Q1 瀑布模型"`

11 题拆成 4+4+3，最后一屏可以补一道"整体把握度"自评问题
（`稳 / 不确定 / 蒙`）。

**反面示例（不要这样写）**：
- `label: "A. 严格按顺序..."` ❌（超长 + 违反 12 字符限制）
- `label: "瀑布顺序描述"` ❌（label 应是选项标识，不是题干）
- 把 `correct` 或 `✅` 塞进 `description` ❌（泄题）

## Step 4 · 一次判分和记档

收到整组答案后只调用一次：

```bash
python3 scripts/tutor.py quiz-grade \
  --quiz-id <quiz-id> \
  --answers 'C,A,D,BD,B' \
  --confidences 'sure,sure,sure,sure,sure'
```

答案输入只有三种合法状态，不需要额外问答：

| 输入 | `response_state` | 记档 | 含义 |
|---|---|---|---|
| `A`–`D` / 组合答案 | `answered` | 正常判分 | 独立作答 |
| `X` | `conceded` | 0 分 + `knowledge_gap` | 考生明确说"不会" |
| 缺题号 | — | 不写入 | 输入不完整，`quiz-grade` 会整组拒绝 |

`X` 不能与选项混写（`AX` 直接报错）。考生说"不会"时直接传 `X`，**不要**让他随便蒙一个字母，也不要为了补齐原子判分去改文件。

考生在作答时明确说明了错因，才在同一次判分中传入，例如：

```bash
python3 scripts/tutor.py quiz-grade --quiz-id <id> --answers 'C,B,A,D,B' \
  --wrong-reason '2=recall_failure;5=misread'
```

没有明确说明的普通答错保持 `unclassified`，不得为了填满字段自动写成 `concept_confusion`。

题目本身有问题时用同一次调用处理，不要中断本组：

```bash
# 第 4 题缺关键表格：排除该题，其余正常判分
python3 scripts/tutor.py quiz-grade --quiz-id <id> --answers 'C,B,A,X,B' \
  --invalidate '4=missing_required_table'
# 第 3 题答案键反直觉：照常判分，同时留给维护任务核对
python3 scripts/tutor.py quiz-grade --quiz-id <id> --answers 'C,B,A,X,B' \
  --audit '3=答案键疑似有误'
```

`--invalidate` 的题不生成 attempt、不计入掌握度，结果里写明"题目无效，本题不计分"；`--audit` 只标记 `needs_audit` 并把说明写入 `.study/quiz-audit-queue.jsonl`，供独立维护任务处理。

`quiz-grade` 会先校验整组答案，再用一个锁和一次日志替换提交全部事件，最后更新
状态、面板和测验清单。任何一题校验失败时整组不写入；重复提交同一测验保持幂等。

### 判分返回值就是讲解的全部素材

返回结果即当前回合的唯一事实来源，**不要再查题库、知识库或 manifest**。每题包含：

| 字段 | 含义 |
|---|---|
| `response_state` / `selected` / `correct` / `is_correct` | 作答状态与判分；`conceded` 的 `selected` 为 `null` |
| `wrong_reasons` / `wrong_reason_status` | 只保存考生明确说明的错因；未说明的普通答错为 `unclassified`，`conceded` 固定为 `knowledge_gap` |
| `explanation` | 已清洗的解析，是微课的事实素材；答对且确定时可能为 `null` |
| `memory_hook` | 登记过的记忆钩子；为 `null` 时用一句话概括即可，不得检索 |
| `variant_question` | 已验证的同细考点变式题（含 `stem` / `options` / `answer`）；没有精确匹配时返回 `null`，不得用同一大考点下的无关题兜底 |
| `next_review_at` | 该考点的下次复习日 |

顶层另有 `score` / `max_score` / `conceded_count` / `invalidated_count`，用于一句话汇报本组结果。

单题 `record` 保留给主观题、旧流程兼容和人工修复，不用于正常客观题循环。

自编题必须先登记，登记文件包含稳定 `item_id`、`topic_id`、`concept_id`、
`question_family_id`、题干和选项：

```bash
python3 scripts/tutor.py register-question --file .study/new-question.json
```

登记会计算内容指纹；内容完全相同却更换 `item_id` 会被拒绝。变式题使用
`variant_of` 指向来源题，并共享细考点或题型族，便于推荐器做冷却和去重。

### 聚合考点必须传 `--facet`

聚合考点及其合法 facet 只以自动生成的
[`tutor/topic-map.md`](./topic-map.md#1-聚合考点record---facet-必填) 为准；
本文不再复制清单，避免与 `curriculum.json` 漂移。

### 证据分级（详见 PROGRESS_PROTOCOL §4）

| 场景 | record？ | skill | mode | 能升 pass_ready？ |
|---|---|---|---|---|
| 客观题（含真题）、闭卷、答对、`sure` | ✅ | recognition | diagnostic/practice | 是（累积 6 条证据 + 跨日 2 次） |
| 答对但 `guess` | ✅ | recognition | diagnostic | 否，只算 fragile |
| 答错 | ✅ | recognition | diagnostic | 否，进 1/3/7/14/30 复习阶梯 |
| 案例独立作答 + 逐项估分 | ✅ | application | practice/mock | 需 2 次 15/25 等价分 |
| 案例只看讲解未作答 | ⚠️ 只写 note | application | practice | 否 |
| 论文口述骨架 | ✅ | application | practice | 否 |
| 论文限时成文 ≥ 2500 字 + 估分 | ✅ | production | mock | 1 篇达到安全线（以 PROGRESS_PROTOCOL 为准） |
| 学员纯聊天没作答 | ❌ | — | — | — |

正常客观题循环不得在 `quiz-grade` 后追加 `status` 或 `diagnose`；其 JSON 返回值
已经包含本轮得分、逐题结果、错因和下次复习时间。

## Step 5 · 反馈与换科

对每道错题给三件套。错因只有考生明确说明时才写入 `--wrong-reason`；否则展示为“未分类”，不得根据一次错误臆测认知原因：

1. **错因分类**：
   - `recall_failure`：概念记得但顺序/名字想不起来
   - `concept_confusion`：混淆了两个相邻概念（例：CMM 老版 vs CMMI）
   - `misread`：题干"错误的是/不属于"读反
   - `knowledge_gap`：完全没学过
   - `application`：知识点会但套错场景（案例常见）

2. **最小记忆钩子**：一句口诀 / 一张对比表 / 一个反例。
   例：
   > RUP 四阶段：**"初精构移"**——初定边界、精立架构（含风险消除）、构建功能、移交用户
   > CMMI：**初·管·定·量·优**（L2 现在叫 Managed，老 CMM 才叫 Repeatable）

3. **一道变式题**（口头答）

考生答完变式题后，整组用一次 `quiz-variant-grade` 落盘（答案顺序按变式出现顺序，
明确不会写 `X`）：

```bash
python3 scripts/tutor.py quiz-variant-grade --quiz-id <quiz-id> \
  --answers 'B,C' --confidences 'sure,unsure' \
  --wrong-reason '2=recall_failure'
```

变式题的作答会写成正式 `recognition` attempt，`variant_of` 指回产生它的原题：
答错或明确不会的变式进入 1/3/7/14/30 复习阶梯；当天纠偏答对仍保留次日复测，
只有到期且确定答对才推进到下一间隔。答对且确定的题进入题目冷却。
不填 `--confidences` 时按 `unsure` 记录，避免未声明把握度的变式冒充确定掌握。
命令返回值含逐题 `is_correct` / `wrong_reasons` / `next_review_at` 和唯一的
`next_action`，直接用它做反馈和下一步路由。

反馈完直接进入下一轮 `quiz-prepare`（推荐与选题已含在命令内，不再单独调用
`recommend`）；连续答对 2 组后主动提"换科"。

## 异常决策表（照做，不现场推理）

| 场景 | 固定处理 | 继续判分 | 现场读文件 |
|---|---|---|---|
| 考生回答"不会" | 该题传 `X`，0 分 + `knowledge_gap` | 是 | 否 |
| 考生漏写一题且未说明 | 一次性指出缺少题号，请补答 | 否 | 否 |
| 考生要求"其他题先判"且该题明确不会 | 该题用 `X` 完成本组 | 是 | 否 |
| 考生答完变式题 | 整组用 `quiz-variant-grade` 记录一次，再给反馈 | 是 | 否 |
| 题目缺关键图表 | `--invalidate 题号=missing_required_table/figure`，该题不记证据；回合结束后新开去重的修复任务，无法可靠修复则保持拦截 | 是 | 否 |
| 题干不清或残缺 | `--invalidate 题号=unclear_stem`；兼容别名 `incomplete_stem`，工具会规范化 | 是 | 否 |
| 答案键反直觉 | 信任已验证题库，`--audit` 标记 `needs_audit` | 是 | 否 |
| 解析缺失或过短 | 给最小解释，不临时查资料 | 是 | 否 |
| `quiz-grade` 非零退出 | 最多一次只读诊断；仍失败则报告阻塞 | 否 | 仅此一次 |
| 发现历史 pending quiz | 忽略，不影响当前 quiz，维护任务单独清理 | 是 | 否 |
| 同一题同一天再出现 | 照常出题（去重已由 `quiz-prepare` 处理），不手工挑题 | 是 | 否 |

## Boundaries

- ❌ 用户未作答，不写任何 `record`
- ❌ 蒙对不记 pass_ready，只算 fragile / learning
- ❌ 单科 11 题小测不外推 75 分制成绩——限时整卷模考才是硬证据
- ❌ 案例/论文的作答用 `--skill application` 或 `production`，不用 `recognition`
- ❌ 案例只看讲解未作答：不算掌握证据
- ❌ 作答前不展示 cheatsheet、参考答案、加粗正解
- ❌ 不主动展示 `.study/` 的原始 JSON（除非用户明确问）

## Troubleshooting

| 症状 | 原因 | 处理 |
|---|---|---|
| `record` 报 `--facet is required` | 聚合考点漏 facet | 查 [`topic-map.md`](./topic-map.md) 补 facet |
| topic-map 与 curriculum 不一致 | 有人改了 curriculum 没重跑生成脚本 | `python3 scripts/gen_topic_map.py` |
| 学员答案里选项字母对不上 | label 用了字母以外内容 | 回 Step 3 校验 label 只放 A/B/C/D |
| status 仍显示 `unmeasured` | 单题小测不能升 measured | 完整 75 分制整卷模考才升级 |
| 学员嫌打字慢改点选 | 走过一次就立刻切 AskUserQuestion | 后续每屏都点选，不要回退 |
| 学员直接要答案 | PROGRESS_PROTOCOL 禁止直接给答案 | 走 scaffolding：给方法 + 让 TA 填空 |

## Verification（每轮循环收尾自检）

答完一组、准备下一组前，快速过一遍：

- [ ] 每题都 `record` 了，`status` 里 attempt 数增加
- [ ] 聚合考点每条都带了 `--facet`
- [ ] 蒙对/不确定的题 `--confidence` 标了 `unsure` / `guess`
- [ ] 自编题已登记细考点、题型族和内容指纹
- [ ] 未重复原题、同题型或当天已经纠偏的细考点
- [ ] 错题给了知识缺口 + 记忆钩子 + 变式题；未获考生明确说明时没有伪造错因
- [ ] 变式题的作答已用 `quiz-variant-grade` 记录，没有只停留在对话里
- [ ] 没把 `✅` / `**答案**` / `**解析**` 泄给学员
- [ ] 向考生说明作答格式时只用了占位符（如 `1_ 2_ 3_ 4_ 5_`），没有用真实字母组合举例——示例串不得恰好等于答案串
- [ ] 没有硬贴 exam-bank 原文（一律走 `quiz-prepare`）
- [ ] 出真题时没把 `![...](../assets/...)` 图片路径或 `【解析】` 贴给学员
- [ ] "不会"的题用了 `X`，没有伪造选项或让考生补答
- [ ] 讲解只用了 `quiz-grade` 的返回值，没有读 manifest / 题库 / 知识库

全部打勾才进入下一轮 `quiz-prepare`。

## 相关工具

| 工具 | 位置 | 作用 |
|---|---|---|
| CLI | [`scripts/tutor.py`](../scripts/tutor.py) | init / status / progress / weakpoints / recommend / quiz-prepare / quiz-grade / quiz-variant-grade / record / configure / doctor |
| 脱敏器 | [`scripts/sanitize_bank.py`](../scripts/sanitize_bank.py) | 题库维护、抽查与人工修复用；支持 `--topic` / `--tag` / `--year` / `--list`，不在答题循环内调用 |
| 质量排除表 | [`scripts/quiz_quality_exclusions.json`](../scripts/quiz_quality_exclusions.json) | 人工确认的坏题黑名单；`doctor` 报告拦截数量，`quiz-prepare` 机械跳过 |
| 考点表生成 | [`scripts/gen_topic_map.py`](../scripts/gen_topic_map.py) | 由 `curriculum.json` 生成 `topic-map.md` |
| 教师人格 | [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md) | 覆盖诊断 / 案例 / 论文全流程决策 |
| 记档协议 | [`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md) | 证据分级、掌握度定义、私隐边界 |
| 考点表 | [`topic-map.md`](./topic-map.md) | 自动生成，切勿手改 |
