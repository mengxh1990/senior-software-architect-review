# Changelog

## [Unreleased]

### Added

- 封闭教学运行时（端到端性能优化 V2 的 P0）：`quiz-grade` 支持 `X` 表示"明确不会"（`response_state=conceded`，0 分、`knowledge_gap`、`confidence=null`，计入复习队列，不再需要伪造选项或让考生补答），支持 `--invalidate 题号=原因` 把坏题排除出本组（不写 attempt、不计掌握度）与 `--audit 题号=说明` 把题库疑点写入 `.study/quiz-audit-queue.jsonl`；判分返回值直接给出完整教学包（`explanation`、`wrong_reasons`、`memory_hook`、同细考点 `variant_question`、`next_review_at`），讲解阶段不再读 manifest、题库或知识库。
- 题目质量门禁：`sanitize_bank` 新增解析清洗（剥离下一题标题、答案残片与资源路径）与 `assess_quality`（题干完整、选项完整、答案合法、盲练安全、图表可用、解析隔离），候选题带 `quality_status` / `quality_issues` / `requires_figure` / `requires_table`；`quiz-prepare` 只选 `ready` 的题，尚不能通过公开输出可靠呈现的本地图片题也会被拦下，人工确认的坏题写入 `scripts/quiz_quality_exclusions.json` 永久排除，`doctor` 新增 `question-bank` 检查报告可出题数与拦截原因。全量真题 1055 道可用题中 996 道通过门禁，59 道被拦下（含 2021 页式地址变换这类缺表题）。
- 题库质量修复与过滤：旧版真题的单行/全角空格选项、跨行紧凑选项和字面 `*` 选项现在可正确解析；`2014下#53`、`2025上#30` 等题恢复完整 A–D 选项。英语阅读填空通过 `contexts` 以共享短文方式呈现，不再给考生裸 `(N)` 编号；数据库与嵌入式题中可恢复的前题依赖已改为独立题干。门禁新增完整 A–D、上下文、跨多独立小题题组和源图/表资源检查：尚无逐小题记档模型的旧版多题组、缺图/缺表、无可信上下文题一律跳过，不再错误压成一题作答。
- 排课与策略结构化：`quiz-prepare` 直接返回 `objective` / `evidence_summary` / `days_left` / `daily_minutes`，"看薄弱点并出题"只需一次命令；`configure --subject-policy 科目=manual_trigger|active` 把"论文先不主动练"这类长期策略写进 `state.json`，推荐器、维护科目选择与薄弱点排名机械尊重该策略（显式 `--subject` 仍可训练）。
- 只读薄弱点排名：`scripts/tutor.py weakpoints --subject <科目> [--days 21] [--limit 10]` 一条命令给出"到期与逾期 / 近期正确率（低→高）/ 未覆盖考点"三段排名，含考点 ID、近期正确率、样本量、蒙对与不确定次数、最近作答日、逾期天数、掌握度与建议动作，并支持 `--json`；该命令只读，不写 `.study/` 与 `attempts.jsonl`。
- 本地综合知识模拟考试终端：75 题高频卷、机考答题卡、限时交卷、确定性判分，以及仅写入 `.study/` 的逐题证据与考后错因反馈。
- 历年真题转录入库：`past-papers/comprehensive-by-year/` 补齐 2009 下–2017 下（综合知识 9 个考期），新增 `past-papers/case-by-year/` 2009 下–2022、2025 上（案例分析 15 个考期，含参考答案），新增 `past-papers/essay-by-year/`（论文完整题干与小问，16 个考期），原题插图以无损 WebP 存入 `past-papers/assets/`。
- 真题转录工具链：`scripts/import_las_papers.py` + `scripts/las_import_manifest.json`，把 `byted-las-document-parse` 的解析结果机械清洗、按科目切分、剔除广告与水印图后写入仓库；配套 `tests/test_import_las_papers.py`。
- `past-papers/SOURCE_COVERAGE.md` 重写为三科覆盖表，并记录转录来源、准入规则与版权边界；`past-papers/case-by-year/README.md`、`past-papers/essay-by-year/README.md` 提供按年索引。
- 2009 下–2017 下综合知识补题组级 `§N` 知识点标签：新增 `scripts/tag_comprehensive_questions.py`（`--preview` 复核、`--check` 校验覆盖、`--apply` 落盘）与标签表 `scripts/comprehensive_topic_tags.json`，共 439 个题组，覆盖第 1–75 题无缺口。
- 真题插图合规清理：逐张 OCR 与像素复核 174 张插图，删除 38 张机构广告图与残留水印图（清单见 `scripts/las_import_manifest.json` 的 `drop_images`，正文原位置保留"已移除"标注），`past-papers/assets/` 收敛到 136 张纯内容图，并新增回归测试防止回填。
- 真题接入出题流程：`scripts/sanitize_bank.py` 支持解析 `past-papers/comprehensive-by-year/` 的两种真题版式，新增 `--topic`（按 tutor 考点）/ `--tag`（按 §标签）/ `--year` / `--limit` / `--list`，输出与 exam-bank 同一套脱敏契约并附 `tag`、`range`、`candidate_topics`；`tutor/quiz-loop-sop.md` 新增 Step 2b 真题抽题流程，教练人格同步更新客观题来源优先级。
- 答题循环提速（一次循环 ≤ 8 次工具调用）：`AGENTS.md` 与 `CLAUDE.md` 的触发条件由"出题→作答→判分→记档"改为"只要本轮会产生落盘（`quiz-grade` / `record` / `mock`）即为答题循环"，用户只说"安排训练""看看进度""今天学什么"时同样先读 `tutor/quiz-loop-sop.md`；教练人格的客观题来源统一走 `tutor.py quiz-prepare`，`sanitize_bank.py` 降级为题库维护、抽查与人工修复入口，不再在循环内调用；SOP 的回合预算补充"元数据瑕疵当场用文字补全、不得重跑换题"与"先一次只读算出薄弱点再出题"两条边界。
- 协议去重与运行时白名单：`AGENTS.md` 明确教学回合为封闭运行时（出题只允许 `quiz-prepare`、判分只允许 `quiz-grade`，禁止读源码/题库/manifest/原始 state/知识库，也禁止现场改文件或让考生补答"不会"的题）；`senior-architect-pass-coach.md` 只保留教学目标、优先级、反馈风格与主观题行为，客观题运行细节和记档字段改为引用 `quiz-loop-sop.md` 与 `PROGRESS_PROTOCOL.md`，`quiz-loop-sop.md` 新增异常决策表与判分返回值契约，`PROGRESS_PROTOCOL.md` 记录 `response_state` 与 `conceded` 的证据语义。
- 案例与论文补主题标签：案例分析 101 道题按 `case-types/` 题型编号标注、论文 67 道题按 `paper-topics/` 主题编号标注，每道题标题下带 `> **题型**／**主题**：…` 一行；新增 `scripts/tag_case_essay.py`（`--preview` / `--check` / `--apply`）与标签表 `scripts/case_essay_tags.json`，兼容各考期的标题写法并把裸行 `试题二 论…` 规范为标题。
- 案例原卷入库并恢复盲练：解析 2009 下–2017 下共 9 份《案例分析》原卷（题干与参考答案天然分离），生成 `past-papers/case-by-year/<考期>-原卷.md` 并复用同考期题型标签；`paper_practice.py` 对原卷题直接盲练、给出原卷插图，并用 `answer_source` 指向研读版文件取参考答案。案例可盲练从 34 道增至 **79 道**。
- 案例与论文真题接入训练：新增 `scripts/paper_practice.py`，支持按案例题型 / 论文主题抽题，案例自动剥离参考答案（`--reveal` 才输出）、把插图链接换成 `【图 N】` 标记；插图被移除的题**默认照常出题**并给出 `figure_note`（教练用文字描述图意），需要插图完整时才加 `--skip-missing-figures`；切不开答案的卷（2009–2018 答案详解转录版、2020、2024 下、2025 下）标为 `read_only` 只作研读，卷末答案区标为 `answer_key` 不作为题目，避免把参考答案当题干发出。`quiz-loop-sop.md` 新增 Step 2c，教练人格同步更新主观题来源。
