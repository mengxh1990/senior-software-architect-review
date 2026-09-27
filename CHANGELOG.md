# Changelog

## [Unreleased]

### Added

- 统一两层训练分类并修复评估状态与复测资源调度：训练分类收敛为知识域→K模块（细知识点仅作笔记标签），跨日与阅读材料证据同最近 12 题窗口分开计量，新增三套互不重题的 75 题固定模考卷并在出卷前检查曝光资格，案例材料不足且已成功时不再自动重复等待达标。
- 答题循环运行时编排：`progress` / `quiz-prepare` / `quiz-grade` / `quiz-variant-grade` 支持 `--runtime-json` 精简返回值，`quiz-grade` 可用 `--prepare-next` 在同一命令内判分并准备续练，SOP 与教师人格建立运行时白名单与回合预算。
- 新增只读 `tutor.py progress` 聚合入口，一次返回三科状态、有效时间分配、薄弱 Top、到期复习和下一步；`manual_trigger` 科目从自动分配中剔除并单独提示，不再为“看看进度”创建答题会话。
- 新增双层考频审计快照与生成器：正式卷/人工复核稳定层占 80%，近年回忆版趋势层占 20%；当前覆盖率未达门槛时保持 `report_only`，`doctor` 与 CI 检查快照是否同步。
- 当天排课去重补漏：`quiz-prepare` 把当天已生成但未判分的 `quiz-sessions/*.json` 也算作“已经考过”，同一天不再重复出同一道题；诊断、排课与变式训练统一使用稳定大考点和独立题目 ID，`objective` 只播报真正出了题的推荐。`quiz-loop-sop.md` 自检清单补充：向考生说明作答格式只能用占位符（如 `1_ 2_ 3_ 4_ 5_`），不得用真实字母组合举例。

### Fixed

- 代码评审修复（review 5 项）：训练预算入账对整卷墙钟/申报用时按训练时限+5 秒宽限封顶（挂机不再挤爆当日预算，完整原始用时仍留给 overtime 判定）；变式自动排除改记 `excluded`，不再把修复回池的题永久拦在 manifest 之外（人工 `--invalidate` 仍 `invalidated`+永久隔离）；`do_HEAD` 对静态资源委派基类返回 GET 同款响应头，仅 API 路径回 204；弃考会话的开考记录按最长训练时限定期清理，长驻进程内存不再无界增长；跨日证据日期口径抽取为 `module_assessment.first_success_dates` 单一实现，三处调用共用同一时间解析。

- 文档对齐：路由命令统一为 `progress --runtime-json`（纯「看看进度」仍用 `--json`，「看薄弱点 + 安排训练」合并场景豁免一次 `--json`）；`PROGRESS_PROTOCOL.md` §7 公式更新为实现常数口径并补考前保命卡落地口径与完整论文 45 分钟估值；`quiz-loop-sop.md` 修正陈旧考期示例与可用题块实测值；`AGENTS.md` 合并本地补充规则（外部资料位置、题目质量与维护分流）；`README.md` 案例题型与考期数修正为实测值。
- 2024 上卷题库维护：逐题补写缺失解析（标注【AI 补写】），第 50–52 题因回忆源单空数据被复制、逐空答案无法核验而暂停盲练待复核；补齐 exam-bank 题块分隔符。
- 出题判分与脱敏：`quiz-grade` 返回值中的 `variant_question` 剥离 answer/explanation，变式解析统一由 `quiz-variant-grade` 返回值获得；答案泄露扫描扩展到共享阅读材料；`paper_practice` 的多批次含混题号在过滤前判定，不再被盲练/缺图过滤静默消歧；题目登记拒绝未知键（加载既有私人登记文件时兼容剥离历史键 `concept_id` / `concept_label` / `question_family_id`）；`question_topics.json` / `subjective_topics.json` 升级 taxonomy_version 2 并清除永不生效与幽灵条目的 `ITEM_TOPIC_OVERRIDES`。
- 证据口径收紧（证据政策版本 5→6）：跨日证据按题目身份去重，每题只计首条确定成功日期、复做旧题不新增跨日证据；英语阅读材料只按确定成功的作答计数；作答事件的 `at` 时间戳必填；pending 回放与 rebuild/repair 同序按解析时间重放；近期与模考证据按解析时间排序；`full_timed` 限定完整论文并从原答去空白重算字数；复测触发统一为超过 7 天。
- 排课 crunch 与路由：考前 3 天只排除「低频且无保命卡」的未学考点，高频（frequency_count ≥ 8）/ 快分点（quick_win ≥ 0.7）与带保命卡考点保留并在考前排序中获得 1.5 倍加分；科目平局统一按当日已训练用时少者优先；薄弱点队列改为 模考失分 → 到期跨日复测 → 猜对或不确定 → 等待复测 → 已验证；缺失时长估值新增非片段完整论文 45 分钟档；只读路由对损坏的复习日期降级为到期而不是整体失败。
- 网页模考测量有效性：交卷用时改以服务端发卷时刻（会话内存记录）为权威、服务重启后回退考生端申报值（不封顶，超限用时由记档侧按 overtime 降资格）；超时判定增加 5 秒宽限，用满时限的自动交卷不再被发卷提前量与传输开销误判 overtime；同会话重复发卷保留首次开考时刻、交卷后清理会话记录，交卷质量复核算入记档临界区且不再阻断已记档会话的幂等重放；未答题不再写 0 分逐题事件，考后错因的可信事件数按实际答题数校验；同卷复测保存整卷成绩并标 `repeated_paper`；交卷前复核整卷质量并校验会话与卷一致；排除本场事件改用带 `-q-` 定界的题目标识；频次快照回退时保留课程表静态考频。
- 清理废弃真题插图（正文已标注移除的 WebP）并把两层分类优化的计划与复核记录归档到 `.planning/`。
- 修复训练链路策略并统一出题判分与掌握证据：逐题主考点与内容指纹防止重复内容抬高独立证据，修正把握度、完整案例与模考资格校验，修复科目路由、案例复习关联与间隔阶梯，支持短组与混合检验，分离判分与续练失败，无效题按内容版本放行。
- 调整变式题默认把握度为确定：未声明把握度时默认记为 `sure`，明确标注 `unsure` / `guess` 时按标注记录。
- 修复 2026 上半年目录项题的选项指数格式。
- 修复历年真题缺表题并完善质量门禁：人工排除表复核收窄，考频快照同步重建。
- 修复题库验证套件中的旧参数与基线登记：幂等重放用例改用仍受支持的题目指纹，已审定真题答案与解析的精确校验值同步修正。
- 补齐分布式事务、大数据、DevOps／Serverless、企业应用集成四章共 72 道自编题的大考点路由；跨主题题按题目本身归属修正，`doctor` 同时检查未接入的题库文件，避免新增章节被漏计。
- 统一训练粒度为课程表中的稳定考点：诊断、抽题、复习和掌握判定不再依赖额外题目分类；旧私人作答保持原样，题目 ID 与内容指纹继续防止重复证据。
- 案例自动路由改为只在 C 级个人赛道中选择，并由路线显式声明覆盖的 K 级支撑考点；配置路线后不再被未选中的架构风格、可靠性等 K 级案例题绕过，显式 `case-prepare --topic` 仍可覆盖自动策略。
- 普通错题不再默认写成 `concept_confusion`；`quiz-grade` / `quiz-variant-grade` 只记录考生通过 `--wrong-reason` 明确提供的错因，“不会”和“猜对”继续作为可确认信号。复习日期按 1/3/7/14/30 天阶梯推进，当天变式不会取消次日复测，并新增 `repair --recompute-derived` 安全重算派生状态。
- 自适应客观题要求同时通过题面质量与讲解门禁，缺少解析的题不会进入训练；`memory_hook` 保持可选，微课以已清洗解析为事实素材。
- 本地模拟考不再提供可脱离交卷的 `/api/mock-grade` 答案查询；页面改用 `/api/mock-submit`，服务端先原子写入原始答卷与模考事件，再回传得分、答案和解析。`repair` 现保留 `subject_policies`，不会在恢复时丢失“仅主动练某科”的个人策略；`serve.py --data-dir` 与写入路径统一复用私人目录的 Git 忽略校验。
- 封闭教学运行时（端到端性能优化 V2 的 P0）：`quiz-grade` 支持 `X` 表示"明确不会"（`response_state=conceded`，0 分、`knowledge_gap`、`confidence=null`，计入复习队列，不再需要伪造选项或让考生补答），支持 `--invalidate 题号=原因` 把坏题排除出本组（不写 attempt、不计掌握度）与 `--audit 题号=说明` 把题库疑点写入 `.study/quiz-audit-queue.jsonl`；判分返回值直接给出完整教学包（`explanation`、`wrong_reasons`、`memory_hook`、同大考点后续题 `variant_question`、`next_review_at`），讲解阶段不再读 manifest、题库或知识库。
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
