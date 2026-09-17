# Changelog

## [Unreleased]

### Added

- 本地综合知识模拟考试终端：75 题高频卷、机考答题卡、限时交卷、确定性判分，以及仅写入 `.study/` 的逐题证据与考后错因反馈。
- 历年真题转录入库：`past-papers/comprehensive-by-year/` 补齐 2009 下–2017 下（综合知识 9 个考期），新增 `past-papers/case-by-year/` 2009 下–2022、2025 上（案例分析 15 个考期，含参考答案），新增 `past-papers/essay-by-year/`（论文完整题干与小问，16 个考期），原题插图以无损 WebP 存入 `past-papers/assets/`。
- 真题转录工具链：`scripts/import_las_papers.py` + `scripts/las_import_manifest.json`，把 `byted-las-document-parse` 的解析结果机械清洗、按科目切分、剔除广告与水印图后写入仓库；配套 `tests/test_import_las_papers.py`。
- `past-papers/SOURCE_COVERAGE.md` 重写为三科覆盖表，并记录转录来源、准入规则与版权边界；`past-papers/case-by-year/README.md`、`past-papers/essay-by-year/README.md` 提供按年索引。
- 2009 下–2017 下综合知识补题组级 `§N` 知识点标签：新增 `scripts/tag_comprehensive_questions.py`（`--preview` 复核、`--check` 校验覆盖、`--apply` 落盘）与标签表 `scripts/comprehensive_topic_tags.json`，共 439 个题组，覆盖第 1–75 题无缺口。
- 真题插图合规清理：逐张 OCR 与像素复核 174 张插图，删除 38 张机构广告图与残留水印图（清单见 `scripts/las_import_manifest.json` 的 `drop_images`，正文原位置保留"已移除"标注），`past-papers/assets/` 收敛到 136 张纯内容图，并新增回归测试防止回填。
