# 两层训练分类与模块证据

训练分类只有两层：**13 个知识域 → 31 个 K 模块**。题目直接归属 K 模块；C 案例赛道、P 论文主题是平行练习入口。没有第三层知识点成绩账户或训练单元。

## 数据职责

- `curriculum.json`：知识域、K 模块及其唯一 `domain_id`，以及 C/P 练习入口。
- `scripts/question_topics.json`：客观题直接 `topic_id`、题型、难度、题面指纹。原始章节标签不决定归属。
- `scripts/subjective_topics.json`：主观题批次身份、完整性、小问和可评分的 K 模块范围。
- `scripts/exam_bank_topics.json`：兼容接口的生成文件，不独立维护。
- `note-tags.json`、`question-note-tags.json`：可选笔记标签目录和题目笔记索引。运行时不读取；增删、改名或缺失不改变选题、判分、复测、进度和考频。
- `.study/attempts.jsonl`：不可改写的原始作答；`.study/state.json.topics`：由日志重建的模块和练习入口成绩。

4+1 视图题统一归 K06，SOA 基础题归 K13，GoF 模式题归 K29，微服务题归 K12。旧 K 英文后缀保留为历史 ID，含义以当前名称为准。

## 出题与评估

`progress` 返回 `--topic` 和实际可用题数。常规训练、补练均在对应 K 模块内选择通过门禁的不同题面，优先未曝光题并兼顾题型、难度。不会按笔记标签筛选，也不会因某个细概念缺题卡住模块训练。原题错因、解析和题目身份继续保留；模块复测达标不能被解释为原题每个细节均已掌握。

普通模块识别：至少6道不同题的确定成功证据，跨2天，最近12道不同内容的加权正确率至少80%。英语模块至少10道、2份可追溯阅读材料并跨日。重复同题不增加独立证据，猜测和不确定降低权重；已达标后答错仍会回退。近期加权表现不足仍显示需巩固，不能被最后一次答对掩盖。跨日和阅读材料证据独立保存，不因最近12题窗口滚动而丢失；当前表现仍由近期窗口和回退规则评估。门槛不随题库库存降低。

`learning_status` 区分未测、证据不足、需巩固、模块抽样达标；`measurement_status` 表示样本是否充分；`resource_status` 单独报告题库容量。模块抽样达标不代表模块全部内容掌握，也不替代独立75题整卷测量。

案例/论文沿用完整作答、时间、字数和独立性门槛。案例库存至少2道独立完整题才足够测量；不足时报告资源缺口。现有题已成功且无待纠正表现时，自动训练跳过无法补足独立证据的路线，显式指定仍可复习；错题补练保留。rubric 按小问评分，可通过 `topic_id` 指向实际考查的 K 模块；同模块多小问合并一次。整题总分不复制给所有关联模块，用时和事件数只计一次。笔记标签不用于评分。

## 独立模考

`mock-forms.json` 固定三套互不重题的75题蓝图，每套覆盖31K；卷01保留原v4题目和题序。卷ID锁定题目和内容指纹，改题后须核验并发布新ID。出卷前排除已作答、已出示的小测题、重复卷和隔离题；没有可用新卷时报告测量资源不足，自动路由继续模块训练。网页可显式进入同卷复习，保留成绩但不增加独立模考证据。正在进行的答卷、判分和错因始终绑定原卷ID。

## 维护与迁移

```bash
python3 scripts/audit_taxonomy.py --write-bank-map
python3 scripts/build_frequency_snapshot.py --write
python3 scripts/gen_topic_map.py
python3 scripts/tutor.py repair --dry-run
python3 scripts/tutor.py repair --normalize-question-links
python3 scripts/tutor.py doctor
```

题面改动必须复核分类和指纹；残缺或不能唯一判分的题继续隔离。题库容量和模块目录见 `topic-map.md`。

两层体系以原始事件重建，不将旧细点聚合成绩直接相加。旧 `assessed_knowledge` 仅由冻结的 `legacy_knowledge_modules.json` 在迁移时转换为模块评分；它不是当前分类，也不依赖笔记标签。备份状态、会话和原日志，保留原始答卷字节、实际成绩和个人路线。新训练不接受细点评分参数。
