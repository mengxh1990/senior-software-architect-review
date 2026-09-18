# Claude Code guidance

Follow the repository-wide rules in [AGENTS.md](./AGENTS.md).

For exam tutoring behavior, use the canonical coach instructions in
[`.claude/agents/senior-architect-pass-coach.md`](./.claude/agents/senior-architect-pass-coach.md),
plus [`tutor/PROGRESS_PROTOCOL.md`](./tutor/PROGRESS_PROTOCOL.md),
[`tutor/quiz-loop-sop.md`](./tutor/quiz-loop-sop.md) and
[`tutor/curriculum.json`](./tutor/curriculum.json).

Any turn that will write learner state (`quiz-grade` / `record` / `mock`) is a quiz
loop, even when the learner only says "安排训练" / "看看进度" / "今天学什么": read
`tutor/quiz-loop-sop.md` first, then finish the round with `quiz-prepare` and
`quiz-grade` instead of calling `recommend` / `sanitize` / `record` / `status` / `diagnose`
separately.

Private learner data belongs only in the Git-ignored `.study/` directory.
