# Claude Code guidance

Follow the repository-wide rules in [AGENTS.md](./AGENTS.md).

For exam tutoring behavior, use the canonical coach instructions in
[`.claude/agents/senior-architect-pass-coach.md`](./.claude/agents/senior-architect-pass-coach.md),
plus [`tutor/PROGRESS_PROTOCOL.md`](./tutor/PROGRESS_PROTOCOL.md),
[`tutor/quiz-loop-sop.md`](./tutor/quiz-loop-sop.md) and
[`tutor/curriculum.json`](./tutor/curriculum.json).

“看看进度”是只读请求，使用 `python3 scripts/tutor.py progress --json`，不得创建
quiz session 或写入 `.study/`。“安排训练” / “今天学什么” / 继续答题才进入答题循环：
先读取 `tutor/quiz-loop-sop.md`，用 `progress --json` 取得 `next_action`，再按路由调用
一次 `quiz-prepare`、`case-prepare` 或论文入口；收到作答后使用对应的判分/记档命令。
正常循环不再拆分调用 `recommend` / `sanitize` / `status` / `diagnose`。

Private learner data belongs only in the Git-ignored `.study/` directory.
