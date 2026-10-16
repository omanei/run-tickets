# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## 0.3.0 - 2026-10-16

### Added
- A `verify` step for tickets whose result is not code. An implement step without commits
  no longer fails; when there is no diff to review, a fresh agent checks the ticket's
  criteria against what actually exists instead of running a code review on nothing.
- `**Kind:** report` in a ticket sends it to `verify` even when it has commits.
- Tickets an agent creates in `issues/` during a run are set to `needs-triage` by the
  runner and are never picked up without a human.
- `run-tickets adopt` takes over a feature another runner or a person started: the final
  review's base and tickets to continue at a given phase from a given commit.
- `examples/demo.py`: try the runner in a throwaway repository with a stand-in agent, no
  model needed.
- MIT license, English README (Russian in `README.ru.md`), this changelog.

### Changed
- `[steps.verify]` takes the review step's agent settings unless set.

## 0.2.0 - 2026-10-16

### Added
- The runner runs `test_command` itself after every step and before each ticket.
- Waiting out provider usage/rate limits instead of stopping, using the reset time from
  the agent's error.
- Work on a branch, optionally in a separate git worktree (`worktree_setup` brings in
  untracked files); the worktree folder is removed when the feature is done, the branch
  stays. Nothing is pushed.
- A final review of the whole feature against the spec.
- `retry`, `reset` and `skip` commands; per-step agent, model and time limit; `max_cost`
  and `max_step_cost`; `protected` paths; flagged changes to test constants;
  `pause_after`; `SUMMARY.md` and short notifications; `status --watch`; several features
  in one run; log rotation; unknown config keys are errors.

## 0.1.0 - 2026-10-16

### Added
- The runner: tickets one at a time in blocker order, implement then review, each in a
  fresh agent process, with checks after every step, crash-safe state and resume.
- Presets for omp, pi, Claude Code and Codex; `.ticket-runner.toml`; `status`, `config`
  and `init` commands.
