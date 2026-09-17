# Changelog

## [Unreleased]

### Added

- 本地综合知识模拟考试终端：75 题高频卷、机考答题卡、限时交卷、确定性判分，以及仅写入 `.study/` 的逐题证据与考后错因反馈。
- 历年真题转录入库：`past-papers/comprehensive-by-year/` 补齐 2009 下–2017 下（综合知识 9 个考期），新增 `past-papers/case-by-year/` 2009 下–2022、2025 上（案例分析 15 个考期，含参考答案），新增 `past-papers/essay-by-year/`（论文完整题干与小问，16 个考期），原题插图以无损 WebP 存入 `past-papers/assets/`。
- 真题转录工具链：`scripts/import_las_papers.py` + `scripts/las_import_manifest.json`，把 `byted-las-document-parse` 的解析结果机械清洗、按科目切分、剔除广告与水印图后写入仓库；配套 `tests/test_import_las_papers.py`。
- `past-papers/SOURCE_COVERAGE.md` 重写为三科覆盖表，并记录转录来源、准入规则与版权边界；`past-papers/case-by-year/README.md`、`past-papers/essay-by-year/README.md` 提供按年索引。
- 2009 下–2017 下综合知识补题组级 `§N` 知识点标签：新增 `scripts/tag_comprehensive_questions.py`（`--preview` 复核、`--check` 校验覆盖、`--apply` 落盘）与标签表 `scripts/comprehensive_topic_tags.json`，共 439 个题组，覆盖第 1–75 题无缺口。
- 真题插图合规清理：逐张 OCR 与像素复核 174 张插图，删除 38 张机构广告图与残留水印图（清单见 `scripts/las_import_manifest.json` 的 `drop_images`，正文原位置保留"已移除"标注），`past-papers/assets/` 收敛到 136 张纯内容图，并新增回归测试防止回填。
- 真题接入出题流程：`scripts/sanitize_bank.py` 支持解析 `past-papers/comprehensive-by-year/` 的两种真题版式，新增 `--topic`（按 tutor 考点）/ `--tag`（按 §标签）/ `--year` / `--limit` / `--list`，输出与 exam-bank 同一套脱敏契约并附 `tag`、`range`、`candidate_topics`；`tutor/quiz-loop-sop.md` 新增 Step 2b 真题抽题流程，教练人格同步更新客观题来源优先级。
- 案例与论文补主题标签：案例分析 101 道题按 `case-types/` 题型编号标注、论文 67 道题按 `paper-topics/` 主题编号标注，每道题标题下带 `> **题型**／**主题**：…` 一行；新增 `scripts/tag_case_essay.py`（`--preview` / `--check` / `--apply`）与标签表 `scripts/case_essay_tags.json`，兼容各考期的标题写法并把裸行 `试题二 论…` 规范为标题。
- 案例与论文真题接入训练：新增 `scripts/paper_practice.py`，支持按案例题型 / 论文主题抽题，案例自动剥离参考答案（`--reveal` 才输出）、把插图链接换成 `【图 N】` 标记；插图被移除的题**默认照常出题**并给出 `figure_note`（教练用文字描述图意），需要插图完整时才加 `--skip-missing-figures`；切不开答案的卷（2009–2018 答案详解转录版、2020、2024 下、2025 下）标为 `read_only` 只作研读，卷末答案区标为 `answer_key` 不作为题目，避免把参考答案当题干发出。`quiz-loop-sop.md` 新增 Step 2c，教练人格同步更新主观题来源。
