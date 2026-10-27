"""A stand-in for a coding agent, so the runner can be tried without a model.

It does what a real agent would be asked to do, crudely: the implement step writes a file
and commits it, the review and verify steps tick the ticket's checkboxes and set it done,
the final review reports no findings. On ticket 02 it slips once, the way real agents do:
it reports success but leaves a criterion unticked, and the runner catches it. The runner
checks all of it exactly as it would check a real agent. Used by examples/demo.py.

Called as: fake_agent.py --cwd REPO PROMPT
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

DELAY = float(os.environ.get("DEMO_DELAY", "0.25"))


def say(line: str) -> None:
    print(line, flush=True)
    time.sleep(DELAY)


def git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def main() -> int:
    args = sys.argv[1:]
    cwd = args[args.index("--cwd") + 1]
    prompt = args[-1]
    first = prompt.splitlines()[0]

    if "for the whole feature" in first:
        say("reading the spec and the diff of the whole feature")
        say("running the full test suite: 9 passed")
        say("\n## Step report\nNo findings across tickets; nothing to fix.")
        return 0

    ticket = Path(re.search(r"(\S+\.md)", first).group(1))
    text = ticket.read_text(encoding="utf-8")
    title = re.search(r"^#\s+\d+:\s*(.+)$", text, re.MULTILINE).group(1)

    if first.split()[0].endswith("implement"):
        say(f"reading {ticket.name}: {title}")
        out = Path(cwd, "src", f"{ticket.stem}.txt")
        out.parent.mkdir(exist_ok=True)
        out.write_text(f"{title}\n", encoding="utf-8")
        say(f"writing src/{out.name} and its tests")
        say("running the tests: passed")
        git(cwd, "add", "src")
        git(cwd, "commit", "-qm", f"{ticket.stem}: {title}")
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                             capture_output=True, text=True).stdout.strip()
        say(f"committed {sha}")
        say(f"\n## Step report\n**Done:** {title.lower()}, in commit `{sha}`.\n\n"
            "| check | result |\n|---|---|\n| unit tests | 9 passed |\n| lint | clean |")
        return 0

    say(f"reviewing {ticket.name}: no findings")
    if ticket.stem.startswith("02") and "failed the runner's check" not in prompt:
        # The demo's slip: claim success but leave a criterion unticked. The runner
        # does not take the agent's word for it and retries the step.
        text = re.sub(r"^- \[ \]", "- [x]", text, count=1, flags=re.MULTILINE)
        text = text.replace("**Status:** ready-for-agent", "**Status:** done")
        ticket.write_text(text, encoding="utf-8")
        say("all done, every criterion is met")
        say(f"\n## Step report\n{ticket.stem} reviewed and done.")
        return 0
    text = re.sub(r"^- \[ \]", "- [x]", text, flags=re.MULTILINE)
    text = text.replace("**Status:** ready-for-agent", "**Status:** done")
    ticket.write_text(text, encoding="utf-8")
    say("every criterion met; ticked, status done")
    say(f"\n## Step report\n{ticket.stem} reviewed and done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
