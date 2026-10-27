# Example configs

Each file is a working `.ticket-runner.toml`: copy it to your repository root (or to a
feature's directory, for that feature only), change `test_command` and the rest to your
project, and check what the runner will do with `run-tickets config <feature dir>`.

| File | For |
|---|---|
| [`claude.toml`](claude.toml) | Claude Code, with budget limits and plugin skill names |
| [`codex.toml`](codex.toml) | Codex CLI, which reports no cost |
| [`omp.toml`](omp.toml) | omp |
| [`pi.toml`](pi.toml) | pi |
| [`cheap-implement-strong-review.toml`](cheap-implement-strong-review.toml) | a cheaper model for implement, a stronger one for the reviews |
| [`budget.toml`](budget.toml) | hard limits on cost and time, a pause after the first ticket |
| [`overnight.toml`](overnight.toml) | an unattended night in a worktree, with a Slack notification |

More setups (tests in Docker, a monorepo, `worktree_setup` for Node and Python, Telegram,
several features in one run) are in the README's
[Recipes](../../README.md#recipes) section.

A test loads every file here, so they keep up with the runner.
