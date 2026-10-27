# Why a runner, if the agent can do it itself

A coding agent can read a folder of tickets and work through them: "implement the tickets
in `issues/`, one by one, run the tests, commit each". For two small tickets this works.
For nine tickets overnight it tends to go wrong, and in the same few ways every time.
[run-tickets](https://github.com/omanei/run-tickets) is the loop around the agent that is
built around those ways.

## 1. A fresh context for every step

In one long session the context fills up. Ticket 7 is written with what the agent still
remembers about ticket 2, or with a compacted summary of it that dropped the detail that
mattered. And when the agent that wrote the code reviews it, it mostly confirms what it
meant to write.

The runner starts a new agent process for every step. Each ticket gets an implement step,
then a review step, and nothing carries over between them, or from the previous ticket.
The repository is the memory: the spec, the ticket file, the commits. The reviewer sees the
ticket's diff the way a colleague would. It fixes what it finds, ticks only the criteria
that are really met and sets the ticket to `done`. Once every ticket is done, one more
fresh agent reviews the whole feature against the spec. That is where gaps between tickets
show up.

## 2. Checks instead of trust

"All tests pass, every criterion is met" at the end of an agent's reply is a claim. Agents
make it about tests they did not run, about checkboxes they did not tick, and once in a
while about a commit that is not there.

After every step, the runner looks for itself:

- the ticket's status and every checkbox;
- no uncommitted changes left behind;
- the branch, the history and your `git stash` untouched;
- no changes to the paths you protected;
- your test command, run by the runner (and before each ticket too, so a base that was
  already broken is not blamed on the agent).

A failed check means a retry in a fresh process, told what failed and given the end of
the test output, or a stop with the reason written down. Skipped tests and raised
thresholds are flagged in the report for a human to look at.

## 3. A night survives what nights bring

At 2 a.m. the provider's usage limit is hit, or the agent hangs on a command that never
returns, or the laptop goes to sleep. In a plain session the night ends there.

The runner waits out a usage limit until the reset time the agent's error names. It kills
the whole process tree of a step that runs past `max_time`. It writes its state atomically
before and after every step, so after a crash, a `kill -9` or a power cut, the same command
resumes at the same step from the same commit.

## 4. The morning: a branch you can review

Nothing leaves your machine. The runner never pushes and never opens a pull request. The
night's work is a branch in your repository, with a commit or more per ticket, so you
review it the way you'd review a colleague's branch, and push it when you are happy with
it. If you set `worktree = true`, it is built in a separate worktree and your own checkout
stays usable all night.

Next to it are `runs/SUMMARY.md` and `runs/report.html`: which tickets passed, which steps
were retried and why, where and why the run stopped, how long each step took and what it
cost.

## What it is not

- **Not a substitute for good tickets.** It runs tickets in the format of Matt Pocock's
  `/to-tickets`: a spec, numbered tickets with blockers and checkable criteria. Vague
  criteria get vague checks.
- **Not a sandbox.** The agent runs without approval prompts. The prompt forbids
  destructive git commands, and the runner notices their effects, but use it where an agent with
  shell access is acceptable.
- **Not parallel (yet).** One ticket at a time, in blocker order.

It works with Claude Code, Codex, omp, pi and other agent CLIs, on Linux, macOS and Windows.
To try it without a model: clone the repository and run `python examples/demo.py`. The
[README](https://github.com/omanei/run-tickets#quick-start) has the rest.
