# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## 0.6.0 - 2026-10-27

### Added
- `runs/report.html`: the whole run on one page for the morning (#11). It shows the outcome,
  time, cost and branch, then a section per ticket and for the final review. Each step has
  its result, duration and links to its logs, and the agent's report is rendered from
  markdown. Retries and stops are highlighted, then the commits and the files changed. It
  is self-contained and works in light and dark themes. The runner rewrites it after every
  ticket; `run-tickets report <feature> [--open]` rebuilds it.
- omp and pi run with `--mode json` (the new `output = "pi-json"`): each tool call shows up
  as it starts, live and in `status`, like Claude's, and their cost is reported and counts
  toward `max_cost` (#10). A config that overrides their `command` without `--mode json`
  keeps working: the output is shown as text.
- Ready configs in `examples/configs/`: one for each of `claude`, `codex`, `omp` and `pi`,
  a cheaper model for implement with a stronger one for the reviews, hard budget limits,
  and an unattended overnight setup (#15). A new Recipes section in the README covers
  tests in Docker (and when `worktree = true` does not fit), a monorepo, `worktree_setup`
  for Node and Python, Slack and Telegram notifications, and several features in one
  run. A test loads every example and every config block in the READMEs, so they keep
  up with the runner.
- `CONTRIBUTING.md`, issue templates (a bug report, a feature request, an agent preset)
  and a pull request template, and `docs/why.md` (`docs/why.ru.md`): why a runner, if the
  agent can do it itself (#16).
- Experimental presets for more agents: `gemini` (Gemini CLI), `qwen` (Qwen Code),
  `opencode`, `kilo` (Kilo CLI), `cursor` (Cursor CLI), `amp`, `droid` (Factory),
  `copilot` (GitHub Copilot CLI) and `goose`. They are made from each tool's
  documentation; `run-tickets config` marks them, and `[agents.<name>]` fixes a flag that
  has changed. Gemini's, opencode's/Kilo's, Cursor's and Amp's JSON streams are read:
  tool calls show live, and opencode/Kilo report cost (new `output` kinds `gemini-json`
  and `opencode-json`).

## 0.5.0 - 2026-10-27

### Added
- On PyPI: `uv tool install run-tickets`, `pipx install run-tickets`, `uvx run-tickets`.
  Pushing a version tag publishes the release to PyPI and creates the GitHub Release from
  this changelog (#8).
- After a step that did not finish and left uncommitted edits, the runner asks on a
  terminal: continue with them, set them aside with `git stash` and redo the step, or quit
  (#6). Without a terminal, or with the new `--no-input`, it stops as before.
- A graceful stop (#7): the first Ctrl+C lets the current step finish and its checks run,
  then stops before the next step (a failed attempt is not retried); the status line says
  so. A second Ctrl+C stops at once, as Ctrl+C did before. SIGTERM and SIGHUP stop at once.

### Fixed
- In the live view, lines of the agent's output wider than the terminal are wrapped by the
  runner, so every screen line keeps the step's gutter and the rest of a list item lines
  up under its text. Styles open at a break go on in the next line.
- Emphasis around inline code (`**the commit `abc123`**`) is rendered.
- The list of uncommitted files kept losing the leading space of its first line, and
  the check of `protected` paths read the first uncommitted file's name without its
  first letter (#9).

## 0.4.0 - 2026-10-27

### Added
- A live view on the terminal. The agent's markdown is rendered (headings, lists,
  checkboxes, code blocks, tables). Each ticket and step gets a header, and every step
  ends with a pass or fail line. A status line at the bottom shows the ticket, the step,
  the attempt, the time it has run, how long the agent has been silent, the cost, and
  progress across the feature. Stops, pauses and the end of a run are shown in a box,
  with the command to go on. `status` gets the same look.
- `ui = "auto" | "live" | "plain"` in the config, `--ui` on the command line, and
  `RUN_TICKETS_UI` in the environment. Piped or redirected output stays plain lines, as
  before. `NO_COLOR` turns colors off.
- `rich` as a dependency: uv and pipx install it, and the live view uses it to highlight
  code blocks and frame tables. A copied `run_tickets.py` run with plain `python` works
  without it, with its own simpler rendering.
- Agents get `markdown = false` to have their output shown raw; codex has it.

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
