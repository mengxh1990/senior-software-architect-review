# 系统架构设计师过线私教 · 仓库指令

## 私人状态与外部记忆边界

- 本仓库不使用 OpenViking；不得调用 OpenViking MCP、读取其历史记忆或向其写入本项目内容。
- `.study/` 是考生档案、学习进度、作答、错题、论文素材和会话状态的唯一事实来源。
- 新任务必须从 `.study/` 恢复进度，不得依赖外部会话记忆补全或推断学习状态。

当用户的意图是备考、学习、刷题、复习错题、练案例、练论文、查看进度或安排今日任务时：

1. 每个新任务首次进入私教模式时，完整读取 [`.claude/agents/senior-architect-pass-coach.md`](./.claude/agents/senior-architect-pass-coach.md)，将其作为本仓库唯一的教师行为规范；同一任务后续答题轮不得重复读取，除非上下文已压缩或文件发生变化。
2. 每个新任务首次进入私教模式时读取 [`tutor/PROGRESS_PROTOCOL.md`](./tutor/PROGRESS_PROTOCOL.md)；课程与进度优先由 `scripts/tutor.py` 在进程内读取，不向模型重复展开完整 `curriculum.json`。
3. 纯“看看进度”是只读请求：使用 `scripts/tutor.py progress --json` 返回三科状态、薄弱点和下一步，不创建 quiz session、不写 `.study/`。用户要求“安排训练”“今天学什么”或直接答题时才进入答题循环；首次进入循环必须先读 [`tutor/quiz-loop-sop.md`](./tutor/quiz-loop-sop.md)，先用 `progress --json` 取得统一路由，再按 `next_action` 调用一次 `quiz-prepare`、`case-prepare` 或 `essay-prepare`。收到作答后使用对应的 `quiz-grade` / `quiz-variant-grade` / `record` 完成记档，不再拆分调用 recommend、sanitize、status 和 diagnose。
   教学回合是封闭运行时：统一路由只允许 `progress`，综合出题只允许 `quiz-prepare`，判分只允许 `quiz-grade`；不得读取源码、题库、quiz manifest、原始 state 或知识库，也不得现场改文件、伪造选项或让考生补答“不会”的题（按 SOP 的异常决策表用 `X` / `--invalidate`）。只有命令非零退出且直接阻塞本轮时，才允许一次只读诊断；仍失败就如实报告，另开维护任务。
4. 使用 `scripts/tutor.py` 维护 `.study/` 中的私人学习状态；若状态不存在，明确说“不知道当前进度”，先建档和诊断，禁止编造。
5. 个人档案、答题记录、错题、论文项目素材和会话记录默认只能写入根目录 `.study/`；只有用户明确指定时才可写入仓库外的私人目录。不得写入公共题库、范文、Issue 或其他受 Git 跟踪文件；不得使用 `git add -f .study`。
6. 给考生出题时，作答前只展示题干和选项，不展示答案标记、解析或文件中加粗的正确项。

当用户是在维护仓库代码或资料，而不是备考时，按普通仓库协作方式处理，不强制进入私教模式。

<!-- codex-migration:claude-md:2026-08-10 -->
## Migrated Claude Code instructions

# Project guidance

For exam tutoring requests, use the project agent `senior-architect-pass-coach` from [`.claude/agents/senior-architect-pass-coach.md`](./.claude/agents/senior-architect-pass-coach.md). Its rules are the single source of truth for tutoring behavior.

Learner data is private. Store profiles, progress, attempts, mistakes, project experience, and session notes only under `.study/`, which must remain ignored by Git. Never copy private learner data into tracked study materials, issues, or pull requests.

For repository maintenance requests, work normally and do not activate tutoring behavior unless the user is studying for the exam.

## 两层分类与模块证据

- 训练分类为知识域 → K模块，由 `tutor/curriculum.json` 定义；题目通过 `scripts/question_topics.json` 直接归属K，C/P是平行练习入口。
- 服从路由 `--topic` 参数；不使用细知识点或训练单元作为出题、评分或进度主键。`note-tags.json` 仅用于笔记，运行时不读取。
- 模块初筛、模块抽样达标与独立整卷成绩分别解释。题库不足、测量不足不等于能力薄弱；保留原题错因和解析。
- 主观题按精确item_id使用完整题面；rubric按小问记录模块及原答依据，同模块合并一次，总分不复制到全部关联模块。
