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
- 只讲教材、不考核（passive lecture）
- 案例题、论文题的完整训练（用 coach 人格里的对应流程）
- `.study/` 损坏或缺失 → 先按 coach 人格里的建档步骤走

## 前置检查

进入循环前必须过：
- CWD 在本仓库根，`python3 scripts/tutor.py --help` 正常
- 已读 [`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md) 与
  [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md)
- `.study/` 存在（若无，先按 coach 人格建档）
- 已跑过 `python3 scripts/tutor.py doctor`，三项 PASS

## Step 1 · 引擎推荐下一考点

```bash
python3 scripts/tutor.py diagnose --subject comprehensive
python3 scripts/tutor.py recommend --subject comprehensive --limit 5
# 若要切换科目：
python3 scripts/tutor.py recommend --subject case --limit 6
python3 scripts/tutor.py recommend --subject essay --limit 6
```

有完整模考时，必须先合并逐题作答与 `postmortems.jsonl`：未纠偏错题、
到期跨日复测、猜对/不确定题依次优先。通用考点排序只用于填补剩余名额。
同一细考点当天完成确定作答后进入冷却，不继续密集重复。

从输出取**优先级最高、且有 exam-bank 题**的考点。
"有没有 exam-bank 题"用
[`tutor/topic-map.md`](./topic-map.md)（脚本自动生成）查。

若 recommend 头部考点在 topic-map 的"无 exam-bank 题"清单里（`C01–C04`、
`P01–P06` 等），需要自编题，并在 record 时打 `--source-type self_authored`。

## Step 2 · 题目脱敏

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

用 `scripts/sanitize_bank.py` 剥离：

```bash
python3 scripts/sanitize_bank.py exam-bank/07-software-engineering.md 1 4 6
```

输出是 JSON 数组，每项含 `stem` / `options[]` / `correct` / `explanation`。
`correct` 与 `explanation` **只**用于判分和作答后反馈，**不**在作答前回显。

### Step 2b · 真题抽题（优先于自编题）

`past-papers/comprehensive-by-year/` 里是 20 个考期的综合知识真题（1055 个可用题块），
**同一套脱敏契约**，用同一个脚本按考点抽题。真题是正式试卷，证据强度高于自编题，
只要 `recommend` 出来的考点能抽到真题，就优先出真题。

```bash
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
| `range` | 题号区间，如 `[7, 8]`；**一个题块可能含多个小问**（共享题干） |
| `candidate_topics` | 由 `curriculum.json` 的 `raw_tags` 推出的 tutor 考点编号，用于 `record --topic` |

抽题注意事项：

- `id` 形如 `past-papers/comprehensive-by-year/2013下.md#7-8`，**record 时原样作为 `--item-id`**；
- 一个题块含多个小问（如 `#7-8`）时按**一题**呈现与记档，不要拆成两条 record；
- `--source-type` 按考期来源选择：2009–2022 用 `real`，回忆版考期（2023 下、2024 上/下、2025 上/下、2026 上）用 `recalled_real`；
- 真题保留试卷原始的答案分布，**不要**套用"自编题正确答案需分散到不同选项"的规则；
- 2019 下、2020、2023 下的整理版本只覆盖部分题目（26 / 12 / 1 个可用题块），抽不到时退回 `exam-bank/` 或自编题；
- 题干里的插图引用形如 `![p1_000.png](../assets/2013下/p1_000.webp)`，**呈现时不要贴图片路径**，用文字描述图意或直接说明"原题含图"。

### Step 2c · 案例与论文真题（按题型 / 主题抽题）

案例与论文的主观题也用真题，入口是 [`scripts/paper_practice.py`](../scripts/paper_practice.py)：

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

## Step 4 · 逐题 record 入档

每答完一屏，立即按题逐条 record：

```bash
python3 scripts/tutor.py record \
  --attempt-id a-YYYYMMDD-dNN \
  --topic <K编号.XXX> \
  --item-id 'exam-bank/<file>.md#<N>' \
  --skill recognition \
  --subject comprehensive \
  --mode diagnostic \
  --score 0|1 --max-score 1 \
  --confidence sure|unsure|guess \
  --source-type simulation|self_authored
```

自编题必须先登记，登记文件包含稳定 `item_id`、`topic_id`、`concept_id`、
`question_family_id`、题干和选项：

```bash
python3 scripts/tutor.py register-question --file .study/new-question.json
```

登记会计算内容指纹；内容完全相同却更换 `item_id` 会被拒绝。变式题使用
`variant_of` 指向来源题，并共享细考点或题型族，便于推荐器做冷却和去重。

### 聚合考点必须传 `--facet`

以下考点漏掉 `--facet` 会报错或数据被并入 `default`（详见
[`tutor/topic-map.md`](./topic-map.md#1-聚合考点record---facet-必填)）：

| Topic | Facets |
|---|---|
| `K05.TEST_CMMI_PATTERNS` | `testing` / `cmmi` / `design_patterns` |
| `K06.DESIGN_DATA_VIEWS` | `high_level_design` / `data_design` / `uml_views` |
| `K12.PATTERNS_SOA_MICROSERVICES` | `design_patterns` / `soa` / `microservices` |
| `K13.VIEWS_SOA_LAYERING` | `four_plus_one` / `soa` / `layering` |

上表由 [`scripts/gen_topic_map.py`](../scripts/gen_topic_map.py) 从
`curriculum.json` 生成。**发现出入以 topic-map.md 为准。**

### 证据分级（详见 PROGRESS_PROTOCOL §4）

| 场景 | record？ | skill | mode | 能升 pass_ready？ |
|---|---|---|---|---|
| 客观题（含真题）、闭卷、答对、`sure` | ✅ | recognition | diagnostic/practice | 是（累积 6 条证据 + 跨日 2 次） |
| 答对但 `guess` | ✅ | recognition | diagnostic | 否，只算 fragile |
| 答错 | ✅ | recognition | diagnostic | 否，进 1/3/7/14 复习队列 |
| 案例独立作答 + 逐项估分 | ✅ | application | practice/mock | 需 2 次 15/25 等价分 |
| 案例只看讲解未作答 | ⚠️ 只写 note | application | practice | 否 |
| 论文口述骨架 | ✅ | application | practice | 否 |
| 论文限时成文 ≥ 2500 字 + 估分 | ✅ | production | mock | 需 2 篇达安全线 |
| 学员纯聊天没作答 | ❌ | — | — | — |

record 完再跑 `python3 scripts/tutor.py status` 确认落盘。

## Step 5 · 反馈与换科

对每道错题给三件套（口头即可，不再走 AskUserQuestion 免得打断节奏）：

1. **错因分类**：
   - `recall_failure`：概念记得但顺序/名字想不起来
   - `concept_confusion`：混淆了两个相邻概念（例：CMM 老版 vs CMMI）
   - `careless_reading`：题干"错误的是/不属于"读反
   - `knowledge_gap`：完全没学过
   - `application_error`：知识点会但套错场景（案例常见）

2. **最小记忆钩子**：一句口诀 / 一张对比表 / 一个反例。
   例：
   > RUP 四阶段：**"初精构移"**——初定边界、精立架构（含风险消除）、构建功能、移交用户
   > CMMI：**初·管·定·量·优**（L2 现在叫 Managed，老 CMM 才叫 Repeatable）

3. **一道变式题**（口头答）

反馈完再走 `recommend` 换下一考点；连续答对 2 组后主动提"换科"。

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
- [ ] 错题给了错因 + 记忆钩子 + 变式题
- [ ] 没把 `✅` / `**答案**` / `**解析**` 泄给学员
- [ ] 没有硬贴 exam-bank 原文（一律走 `sanitize_bank.py`）
- [ ] 出真题时没把 `![...](../assets/...)` 图片路径或 `【解析】` 贴给学员

全部打勾才进入下一轮 `recommend`。

## 相关工具

| 工具 | 位置 | 作用 |
|---|---|---|
| CLI | [`scripts/tutor.py`](../scripts/tutor.py) | init / status / recommend / record / doctor |
| 脱敏器 | [`scripts/sanitize_bank.py`](../scripts/sanitize_bank.py) | 剥 exam-bank 与真题答案，输出结构化 JSON；支持 `--topic` / `--tag` / `--year` / `--list` 抽题 |
| 考点表生成 | [`scripts/gen_topic_map.py`](../scripts/gen_topic_map.py) | 由 `curriculum.json` 生成 `topic-map.md` |
| 教师人格 | [`.claude/agents/senior-architect-pass-coach.md`](../.claude/agents/senior-architect-pass-coach.md) | 覆盖诊断 / 案例 / 论文全流程决策 |
| 记档协议 | [`PROGRESS_PROTOCOL.md`](./PROGRESS_PROTOCOL.md) | 证据分级、掌握度定义、私隐边界 |
| 考点表 | [`topic-map.md`](./topic-map.md) | 自动生成，切勿手改 |
