"""A fake coding agent for the run-tickets tests.

Called as: stub_agent.py --session-dir DIR --cwd REPO [PROMPT]  (prompt from stdin when absent)
The step comes from the prompt's first line (/skill:implement or /skill:code-review), the
ticket from the first *.md path in it. What it does is set by $STUB_PLAN, a JSON file
{"<ticket>:<step>": ["action of call 1", "action of call 2", ...]}; default "ok".
Every call is appended to $STUB_CALLS as a JSON line with the full prompt.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--export":
        Path(Path(args[1]).stem + ".html").write_text("<html>transcript</html>")
        return 0
    opts, rest = {}, []
    i = 0
    while i < len(args):
        if args[i] in ("--session-dir", "--cwd", "--model"):
            opts[args[i]] = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    prompt = rest[-1] if rest else sys.stdin.read()
    cwd = opts["--cwd"] if "--cwd" in opts else os.getcwd()
    first = prompt.splitlines()[0]
    step = "implement" if first.split()[0].endswith("implement") else "review"
    ticket = Path(re.search(r"(\S+\.md)", first).group(1))
    key = f"{ticket.stem}:{step}"

    calls = Path(os.environ["STUB_CALLS"])
    n = sum(1 for line in calls.read_text().splitlines() if json.loads(line)["key"] == key) \
        if calls.exists() else 0
    with calls.open("a") as f:
        f.write(json.dumps({"key": key, "prompt": prompt, "model": opts.get("--model")}) + "\n")
    plan = json.loads(Path(os.environ["STUB_PLAN"]).read_text()) if os.environ.get("STUB_PLAN") else {}
    actions = plan.get(key, ["ok"])
    action = actions[min(n, len(actions) - 1)]

    if "--session-dir" in opts:
        Path(opts["--session-dir"], "session.jsonl").write_text('{"type":"session"}\n')

    text = ticket.read_text(encoding="utf-8")
    if action == "exit1":
        print("provider error")
        return 1
    if action == "hang":
        Path(os.environ["STUB_PIDFILE"]).write_text(str(os.getpid()))
        time.sleep(120)
        return 0
    if action == "needs-info":
        text = re.sub(r"(\*\*Status:\*\*) \S+", r"\1 needs-info", text)
        ticket.write_text(text + "\n## Comments\n\nunclear\n", encoding="utf-8")
    elif step == "implement":
        if action in ("ok", "dirty", "commit-then-hang"):
            with open(Path(cwd, "code.txt"), "a") as f:
                f.write(ticket.stem + "\n")
            git(cwd, "add", "code.txt")
            git(cwd, "commit", "-qm", f"implement {ticket.stem}")
        if action == "dirty":
            with open(Path(cwd, "code.txt"), "a") as f:
                f.write("half-done\n")
        if action == "stash-pop":
            git(cwd, "stash", "pop")
        if action == "commit-then-hang":
            Path(os.environ["STUB_PIDFILE"]).write_text(str(os.getpid()))
            time.sleep(120)
    else:
        if action == "ok":
            text = re.sub(r"^- \[ \]", "- [x]", text, flags=re.MULTILINE)
            text = text.replace("**Status:** ready-for-agent", "**Status:** done")
        elif action == "unticked":
            text = text.replace("**Status:** ready-for-agent", "**Status:** done")
        ticket.write_text(text, encoding="utf-8")
    final = f"Done.\n\n## Step report\n- step: {step} {ticket.stem} ({action})"
    if os.environ.get("STUB_STREAM"):  # Claude Code's --output-format stream-json
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git commit"}}]}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": final, "num_turns": 3, "total_cost_usd": 0.5}))
    else:
        print(final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
