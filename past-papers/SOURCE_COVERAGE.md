# 真题来源与结构化覆盖

> 本表描述仓库内的整理状态。仓库只收录**转录后的 Markdown**：贡献者购买的扫描件不进入版本库，扫描原件仅作为转录来源留在贡献者本机。

## 来源类型

- `real`：题干和答案来源可追溯到正式试卷或可信公开整理；
- `recalled_real`：考后回忆版，可能缺题干、选项或准确答案；
- `simulation`：自编或教辅模拟题，不能称为真题；
- `unstructured`：已知存在外部原始资料，但尚未按本仓库格式整理。

## 转录来源说明

2009–2022 的多数考期由**贡献者购买的扫描件**转录而来，流程见 [`scripts/import_las_papers.py`](../scripts/import_las_papers.py) 与 [`scripts/las_import_manifest.json`](../scripts/las_import_manifest.json)：

1. `byted-las-document-parse` 将扫描 PDF 解析为 Markdown（normal 模式）；
2. 机械清洗页眉、页码、广告条与页脚水印；
3. 按"综合知识 / 案例分析 / 论文"切分，剔除机构 logo 与重复贴片图；
4. 内容图转存为无损 WebP，并对灰度图中的粉色印刷水印做颜色抑制；
5. 题干、选项、答案、解析**逐字保留**，导入过程不改写、不补写内容。

因此这些文件的 **题干与答案可追溯到正式试卷**，`解析` 部分属于出版方整理，不是本仓库编写。

## 综合知识训练覆盖（2018 年起，11 个考期）

| 考期 | 仓库状态 | 来源类型 | 文件 |
|---|---|---|---|
| 2018 下 | 已结构化 | real | [2018下.md](./comprehensive-by-year/2018下.md) |
| 2019 下 | 已结构化 | real | [2019下.md](./comprehensive-by-year/2019下.md) |
| 2020 | 24 个回忆题块，原 PDF 自身不完整，不能视为 75 题完整卷 | recalled_real | [2020.md](./comprehensive-by-year/2020.md) |
| 2021 | 已结构化 | real | [2021.md](./comprehensive-by-year/2021.md) |
| 2022 | 已结构化 | real | [2022.md](./comprehensive-by-year/2022.md) |
| 2023 下 | 已结构化（回忆版考点清单 + rkpass 重建稿按内容匹配互证，多空题组已拆为逐空小题） | recalled_real | [2023下.md](./comprehensive-by-year/2023下.md) |
| 2024 上 | 已结构化 | recalled_real | [2024上.md](./comprehensive-by-year/2024上.md) |
| 2024 下 | 已结构化 | recalled_real | [2024下.md](./comprehensive-by-year/2024下.md) |
| 2025 上 | 已结构化 | recalled_real | [2025上.md](./comprehensive-by-year/2025上.md) |
| 2025 下 | 已结构化 | recalled_real | [2025下.md](./comprehensive-by-year/2025下.md) |
| 2026 上 | 来源 PDF + 考情概要 | recalled_real | [2026上.md](./comprehensive-by-year/2026上.md) |

**知识点标签**：综合训练只保留 2018 下–2026 上的转录。2009–2017 综合题已删除，旧题组映射及批量导入入口同步移除；私人历史记录不受影响。

**案例与论文标签**：标签表现有案例 91 条、论文 60 条（含不同版本/答案区，不等于独立试卷题数）。2009–2017 仅保留 30 道案例和 28 道论文。案例编号 01–15，论文编号 01–13；逐题依据见 [历史题复核](HISTORICAL_CURATION.md)，标签与正文一致性由 `scripts/tag_case_essay.py --check` 校验。

## 案例分析覆盖（20 / 20 考期）

| 考期 | 仓库状态 | 来源类型 | 文件 |
|---|---|---|---|
| 2009 下 – 2017 下 | 30 道经典选题；题干版与答案版配对，非完整年份卷 | real | [2009下-原卷.md](./case-by-year/2009下-原卷.md) … [2017下-原卷.md](./case-by-year/2017下-原卷.md) |
| 2018 下 | 已结构化（购买扫描件转录，含参考答案） | real | [2018下.md](./case-by-year/2018下.md) |
| 2019 下 | 已结构化（购买扫描件转录，含问题解析） | real | [2019下.md](./case-by-year/2019下.md) |
| 2020 | 已结构化（购买扫描件转录，含答案） | real | [2020.md](./case-by-year/2020.md) |
| 2021 | 已结构化（购买扫描件转录，含答案） | real | [2021.md](./case-by-year/2021.md) |
| 2022 | 已结构化（原卷题干 + 文末答案解析） | real | [2022.md](./case-by-year/2022.md) |
| 2023 下 | 已结构化（**部分还原**：必答题题干、插图与参考答案齐全，选答题仅存考点线索） | recalled_real | [2023下.md](./case-by-year/2023下.md) |
| 2024 上 | 已结构化 | recalled_real | [2024上.md](./case-by-year/2024上.md) |
| 2024 下 | 已结构化 | recalled_real | [2024下.md](./case-by-year/2024下.md) |
| 2025 上 | 已结构化（回忆还原版） | recalled_real | [2025上.md](./case-by-year/2025上.md) |
| 2025 下 | 已结构化 | recalled_real | [2025下.md](./case-by-year/2025下.md) |
| 2026 上 | 已结构化 | recalled_real | [2026上.md](./case-by-year/2026上.md) |

## 论文覆盖

两套资料分工不同，配合使用：

- [`essay-questions-by-year.md`](./essay-questions-by-year.md)：2009–2026 题目清单 + 主题映射 + 选题决策树（速查）；
- [`essay-by-year/`](./essay-by-year/)：按考期存放**完整题干与小问**（写作训练用）。

| 考期 | 完整题干 | 来源类型 |
|---|---|---|
| 2009 下 – 2017 下 | 28 道经典选题，原题号保留；非完整年份卷 | real |
| 2018 下 | ✅（购买扫描件转录，另含参考要点） | real |
| 2019 下 | ✅（购买扫描件转录，含解析） | real |
| 2020 | ✅（购买扫描件转录，含参考要点） | real |
| 2021 – 2022 | ✅（购买扫描件转录） | real |
| 2023 下 | 部分还原（四个论题名称已多源确认，题干正文与小问仅部分还原） | recalled_real |
| 2024 上 | 见 [`incoming-raw/2024年上半年/论文.md`](./incoming-raw/2024年上半年/论文.md) | recalled_real |
| 2024 下 | ✅（回忆还原版转录） | recalled_real |
| 2025 上 | ✅（回忆还原版转录） | recalled_real |
| 2025 下 / 2026 上 | 见 [`incoming-raw/`](./incoming-raw/) 对应年份 | recalled_real |

## 整理准入规则

1. 题干、选项或答案缺失时明确写 `？`，不得猜补；
2. 组合题必须保留共享题干和小题边界；
3. 每道题标注年份、题号、稳定考点及来源类型；
4. 回忆版与正式试卷分开统计；
5. 同一道题的不同扫描件只保留一个结构化记录；
6. 只有完整同科试卷才能登记为模考证据。

## 版权边界

仓库不提交商业课件、扫描版教材或购买来的 PDF 原件。扫描件仅用于**转录**，转录结果只保留题干、选项、答案、解析等考试内容本身。

**插图口径**：`past-papers/assets/` 只保留与题目内容直接相关的图形（架构图、数据流图、E-R 图、流程图、计算表等）。凡是机构广告、二维码推广图、页眉页脚 logo、重复贴片图，以及仍然可见机构水印的图片，一律不入库：

- 形状与重复贴片规则自动剔除广告条与页脚 logo；
- 对灰度内容图做粉色印刷水印抑制；
- 逐张 OCR 复核 + 像素复核，凡命中机构推广文案或残留可见水印的，记入 [`scripts/las_import_manifest.json`](../scripts/las_import_manifest.json) 的 `drop_images` 并**从仓库删除**，正文原位置保留 `（原图含机构广告或水印，已移除）` 标注；
- 该口径有回归测试保护（`tests/test_import_las_papers.py`），确保被剔除的图片不会再出现在 `assets/`。

当前 136 张插图全部通过"无广告文案、无水印残留"复核。若权利人对收录有异议，请提交 Issue 申请移除。

## 历史题筛选边界

2009–2017 综合题已退出训练；旧案例/论文的保留理由与标签复核见 [HISTORICAL_CURATION.md](HISTORICAL_CURATION.md)。来源 PDF 留存于本机，仓库不复制已删除题目的全文作为另一套训练入口。
