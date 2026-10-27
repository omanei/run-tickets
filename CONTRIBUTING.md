# Contributing

Thanks for looking. Bug reports with a `runs/REPORT.md` excerpt are as useful as code: the
runner exists to survive what agents do at night, and every new way they fail is worth
knowing about.

## Run the tests and the demo

```bash
python -m pip install pytest "tomli; python_version < '3.11'"
python -m pytest                 # without rich: the live view's plain fallbacks
python -m pip install "rich>=13"
python -m pytest                 # with rich: two more tests, code highlighting and tables
python examples/demo.py          # a whole run in a throwaway repository, no model needed
```

The tests run the real runner against a stand-in agent, `tests/stub_agent.py`. A test
says what the agent does on each call through `$STUB_PLAN` (`{"01-first:implement":
["exit1", "ok"]}`): commit, leave a criterion unticked, hang, hit a provider limit, leave
the tree dirty, press Ctrl+C. The stub then does exactly that, and the test checks what
the runner made of it. CI runs the suite on Linux, macOS and Windows with Python 3.9
(without rich) and 3.13 (with it), and builds and installs the package.

## The rules of the code

- **One file.** `run_tickets.py` is the whole program: people copy it into a repository
  and run it with plain `python`. It uses the standard library, `tomli` on Python < 3.11
  only to read the config, and `rich` if it is there. Every use of `rich` has a fallback
  that works without it. No other dependencies.
- **Python 3.9.** No `match`, no `X | Y` outside annotations, no 3.10+ library calls. The
  3.9 CI jobs catch most of it.
- **Linux, macOS and Windows.** Use `Path` and argument lists, not shell strings, for the
  runner's own commands. Process trees are killed through `kill_tree`; do not assume
  `os.killpg` or signals exist.
- **Local only.** The runner never pushes, opens pull requests or calls a service of its
  own. The network is the agent's business, plus `notify_command` if the user sets one.
- **The agent is not trusted.** A step passes on what the runner checks itself (the
  ticket file, git, the tests), never on what the agent says it did. A new check belongs
  in `check_step` with a test that makes the stub lie.
- **State is crash-safe.** Write `state.json` through `State` (atomic writes). Anything a
  run needs after `kill -9` goes there before the step that needs it.

## Plain output is an interface

The plain lines (`==> 01-first: implement with claude (log: …)`, `Done in this run:`,
`open:`, the `status` text) are what people grep in `run.out`, and what scripts and CI read.
Keep them byte for byte; many tests assert them. If one has to change, say so under
"Changed" in `CHANGELOG.md`.

Runner code does not print. It tells its view what happened (`self.view.step_passed(…)`).
`PlainView` turns that into the plain lines, and `LiveView` turns it into the live view.
A new event gets a method on both. The live view is free to change. Check it on a real
terminal, or record the demo (`python examples/record_demo.py`, needs `pillow` and
`pyte`) and look at the frames.

## Adding an agent preset

1. **The preset.** Add an entry in `AGENT_PRESETS` with `command`, `model_args`,
   `prompt_via`, `skill_format`, `output` and `export_command`. The command must run one
   prompt without a terminal and without approval prompts. Add the name to
   `EXPERIMENTAL_AGENTS` until it has been run against the real tool.
2. **Its output.** If the tool streams JSON events, map them in `stream_items`, or reuse
   `claude-stream-json` when the format follows Claude Code's. Make it report the final
   reply (`stream_result`) and the cost, if it has one. Give `tests/stub_agent.py` a
   `STUB_FORMAT` that prints a few real-looking events, taken from the tool's output, not
   invented. Then add the format to `test_other_agents_streams`.
3. **Tests.** Add a row in `test_preset_argv` with the exact command line, with and without
   a model.
4. **Docs.** Add a row in the Agents table of `README.md` and `README.ru.md`, mention it in
   the cost and live-tool-call sentences under the table if it applies, and add a line
   in `CHANGELOG.md` under "Unreleased".

`run-tickets config <feature>` shows the command the preset produces. Running it on a
one-ticket feature with the real tool is the test that counts; say in the pull request
whether you did.

## Pull requests

- One change per pull request, with a test that fails without it.
- `README.md` and `README.ru.md` change together. If you don't write Russian, say so in
  the pull request.
- A user-visible change gets a line in `CHANGELOG.md` under "Unreleased".
- Releases are made by the maintainer: see [Releasing](README.md#releasing).
