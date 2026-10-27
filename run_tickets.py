#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ['tomli>=1.1; python_version < "3.11"', 'rich>=13']
# ///
"""run-tickets: implements a feature's tickets one after another with a coding agent.

Works on Linux, macOS and Windows with Python 3.9+ and git. Installed with uv or pipx
it brings `rich` (code highlighting and framed tables on the terminal); run as a plain
file it works without any third-party package (a .ticket-runner.toml config then needs
Python 3.11+ or the `tomli` package).

A feature is a directory with issues/NN-<slug>.md tickets (the /to-tickets format of
Matt Pocock's skills). Each ticket gets two fresh agent processes, so nothing carries
over from the previous ticket or step:
  1. implement - the implement skill on the ticket, work committed;
  2. review    - the code-review skill on the ticket's commits, findings fixed and
                 committed, every checkbox verified and ticked, Status set to done.
The next ticket starts only after the previous one passed both steps and every check
the runner makes itself (including the project's tests, when test_command is set).
The agent's own words about the repository are never trusted. When every ticket is
done, a final review looks at the whole feature. Everything stays local: the work ends
up as a branch in the repository, and pushing it is up to you.

  run-tickets run    .scratch/my-feature     # run until done, blocked or stopped
  run-tickets status .scratch/my-feature     # what is done, what is next, what is stuck
  run-tickets config .scratch/my-feature     # effective settings and the agent commands
  run-tickets retry  .scratch/my-feature 05  # rerun the step ticket 05 stopped at
  run-tickets init                           # write an example .ticket-runner.toml

State survives crashes: <feature>/runs/state.json is rewritten atomically before and
after every step, so a run killed at any point (Ctrl+C, kill -9, power loss) resumes
at the interrupted step of the same ticket with the same base commit.

Exit codes: 0 all tickets done; 1 stopped on a failed check; 2 usage or setup error;
3 nothing runnable but tickets remain open; 4 paused (pause_after); 130 interrupted.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

__version__ = "0.4.0"

IS_WINDOWS = os.name == "nt"

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_PAUSED = 4
EXIT_INTERRUPTED = 130

# Statuses of the local tracker. "done" is the only one the runner treats as finished.
RUNNABLE = "ready-for-agent"
DONE = "done"
NEW_TICKET_STATUS = "needs-triage"  # tickets an agent creates during a run wait for a human
CLOSED = {"done", "wontfix"}

STEPS = ("implement", "review", "verify", "final")
# Phases of a ticket the runner is in the middle of. After implement comes review, or verify
# when there is no diff to review (the implement step made no commits, or the ticket is a
# report): a report, a test-bench run or new tickets live outside git.
TICKET_PHASES = ("implement", "review", "verify")
FINAL = "final-review"  # the state entry of the feature-wide final review

CONFIG_NAMES = (".ticket-runner.toml",)

# ---------------------------------------------------------------------------
# Agents

# Each preset is a command template. Placeholders: {{prompt}} {{repo}} {{session_dir}}
# {{final_file}}. model_args are inserted only when a model is set, budget_args only
# when max_step_cost is set: at a "{{model_args}}" / "{{budget_args}}" item, or else
# right after the executable. prompt_via: "arg" or "stdin".
AGENT_PRESETS: Dict[str, Dict[str, Any]] = {
    "omp": {
        "command": ["omp", "-p", "--auto-approve", "--session-dir", "{{session_dir}}",
                    "--cwd", "{{repo}}", "{{prompt}}"],
        "model_args": ["--model", "{{model}}"],
        "prompt_via": "arg",
        "skill_format": "/skill:{{skill}} {{args}}",
        "output": "text",
        # Run in the session dir; omp writes omp-session-<name>.html into the cwd.
        "export_command": ["omp", "--export", "{{session_file}}"],
    },
    "pi": {
        "command": ["pi", "-p", "--session-dir", "{{session_dir}}", "{{prompt}}"],
        "model_args": ["--model", "{{model}}"],
        "prompt_via": "arg",
        "skill_format": "/skill:{{skill}} {{args}}",
        "output": "text",
        "export_command": ["pi", "--export", "{{session_file}}", "{{html_file}}"],
    },
    "claude": {
        "command": ["claude", "-p", "--output-format", "stream-json", "--verbose",
                    "--permission-mode", "bypassPermissions"],
        "model_args": ["--model", "{{model}}"],
        "budget_args": ["--max-budget-usd", "{{budget}}"],
        "prompt_via": "stdin",
        "skill_format": "/{{skill}} {{args}}",
        "output": "claude-stream-json",
        "export_command": [],
    },
    "codex": {
        "command": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                    "-C", "{{repo}}", "--output-last-message", "{{final_file}}", "-"],
        "model_args": ["-m", "{{model}}"],
        "prompt_via": "stdin",
        "skill_format": "Use the {{skill}} skill. Arguments: {{args}}",
        "output": "text",
        # codex exec prints its commands' raw output and diffs, not just markdown.
        "markdown": False,
        "export_command": [],
    },
}

DEFAULT_LIMIT_PATTERNS = [
    r"usage limit", r"rate[ _-]?limit", r"\b429\b", r"quota", r"too many requests",
    r"hit your limit", r"limit (?:reached|exceeded)", r"overloaded", r"\bresets? (?:at|in)\b",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "agent": "omp",
    "model": "",
    "max_time": "3h",
    "retries": 1,
    "retry_delay": "1m",
    "desktop_notify": True,
    "skills": {"implement": "implement", "review": "code-review"},
    "steps": {},
    "report_heading": "Step report",
    "report_language": "English",
    "preflight": [],
    "notify_command": "",
    "extra_instructions": "",
    "review_extra_instructions": "",
    "final_review_extra_instructions": "",
    # The runner's own test gate.
    "test_command": "",
    "test_timeout": "30m",
    "test_baseline": True,
    # Provider limits.
    "limit_wait": True,
    "limit_patterns": DEFAULT_LIMIT_PATTERNS,
    "limit_default_wait": "30m",
    "limit_max_wait": "12h",
    # Git flow.
    "branch": "",
    "worktree": False,
    "worktree_dir": "{{repo}}/../{{repo_name}}-worktrees/{{feature}}",
    "worktree_setup": [],
    # The end of the feature.
    "final_review": True,
    "pause_after": [],
    # Guards.
    "max_cost": 0,
    "max_step_cost": 0,
    "protected": [],
    "suspicious_patterns": [
        r"pytest\.mark\.skip", r"\bxfail\b", r"\.skip\(", r"\bxit\(", r"\bxdescribe\(",
        r"@Disabled", r"@Ignore", r"t\.Skip\(", r"#\[ignore\]", r"\bunittest\.skip",
    ],
    # runs/ housekeeping.
    "log_retention": "30d",
    "log_max_mb": 2000,
    # The console: "auto" (live on a terminal, plain lines when piped), "live", "plain".
    "ui": "auto",
}

COMMON_PROMPT = """You run unattended: nobody will answer questions. Decide by the spec ({{spec}}), the repo's ADRs and docs.
If you cannot finish, set the ticket's status line to `**Status:** needs-info`, append the reason under a `## Comments` heading at the end of the ticket file, and stop.
End your final answer with a section headed `## {{report_heading}}`, written in {{report_language}}, for whoever reads the run in the morning: what you did (commit hashes), which checks you ran and their results, what is left or failed, and - if you stopped early or left anything uncommitted - exactly why.
{{git_rules}}
{{new_tickets_rule}}
{{extra_instructions}}"""

GIT_RULES = """Never run git stash, git reset --hard, git clean, git rebase, git commit --amend, git push, git checkout of another branch, or git checkout/restore of files you did not change: the working tree, the branch and the stash list hold the user's own work. To test the code without your changes, use git worktree add in a temporary directory and remove it afterwards.
Never weaken a test, raise a threshold or add a skip to make tests pass; if you believe one is wrong, say why under `## Comments` in the ticket and in your step report.{{test_rule}}"""

IMPLEMENT_PROMPT = """{{implement_command}}

{{common}}
Skip the code review at the end: a separate step reviews this ticket after you. Do not change the ticket's status line and do not tick its checkboxes; the review step does that.
Commit all your work to the current branch. A ticket whose result is not code (a report, a test-bench run, new tickets) may end with no commit; a separate step then checks its result.
{{kind_note}}
{{resume_note}}"""

REVIEW_PROMPT = """{{review_command}}

{{common}}
After the review, without asking:
1. Fix every finding that is a real defect or a deviation from the ticket, the spec or the repo's standards. Skip only findings you verify to be wrong, and list them with the reason under `## Comments` in the ticket.
2. Run the tests of the touched modules and make them pass.
3. Commit the fixes to the current branch.
4. Check every `- [ ]` acceptance criterion of the ticket against the code and the test results. Tick it (`- [x]`) only when it is really met. If any criterion is not met, finish it first; if you cannot, follow the instruction above for unfinished work.
5. When every criterion is ticked, set the ticket's status line to `**Status:** done`.
{{review_extra_instructions}}
{{resume_note}}"""

VERIFY_PROMPT = """Verify the ticket {{ticket}}. Its implement step made no commits{{verify_reason}}, so there is no diff to review: its result, if any, is in files outside git, in the files the ticket names, and in the ticket itself.

{{common}}
Without asking:
1. Read the ticket, its `## Comments` and every file it names.
2. Check each `- [ ]` acceptance criterion against what actually exists: the files, their contents, results you can re-check yourself. The implement step's word is not evidence.
3. Tick a criterion (`- [x]`) only when it is really met. Finish what is missing if you can, and commit code if you change any.
4. When every criterion is ticked, set the ticket's status line to `**Status:** done`; otherwise follow the instruction above for unfinished work.
{{review_extra_instructions}}
{{resume_note}}"""

FINAL_PROMPT = """{{final_command}}

This is the final review of the whole feature. Its tickets ({{tickets}}) were implemented and reviewed one at a time, each by a fresh agent, so nobody has yet looked at them together. Review the change since {{base}} as a whole against the spec ({{spec}}): behaviour the spec asks for that no ticket covered, gaps and mismatches between tickets, duplicated or inconsistent code, missing wiring, dead code left by earlier tickets.
You run unattended: nobody will answer questions.
Without asking:
1. Fix every finding that is a real defect or a deviation from the spec or the repo's standards, and commit the fixes to the current branch.
2. Run the full test suite and make it pass.
3. Do not edit the ticket files.
4. If something cannot be fixed here, say what and why in your report.
End your final answer with a section headed `## {{report_heading}}`, written in {{report_language}}: what you found, what you fixed (commit hashes), which checks you ran and their results, and what is left.
{{git_rules}}
{{new_tickets_rule}}
{{final_review_extra_instructions}}
{{resume_note}}"""

# ---------------------------------------------------------------------------
# Small helpers


class UsageError(Exception):
    pass


class Interrupted(Exception):
    pass


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(t: Optional[dt.datetime] = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


def parse_duration(text: Any) -> int:
    """'3h', '90m', '1h30m', '45s', '30d', '600' -> seconds."""
    text = str(text).strip().lower()
    if text.isdigit():
        return int(text)
    parts = re.findall(r"(\d+)\s*([dhms])", text)
    if not parts or "".join(n + u for n, u in parts) != re.sub(r"\s+", "", text):
        raise UsageError(f"bad duration '{text}', use e.g. 3h, 90m, 1h30m, 30d, 600")
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def fmt_cost(cost: Optional[float]) -> str:
    return f"${cost:.2f}" if cost else "-"


def fill(template: str, values: Dict[str, str]) -> str:
    """Replaces {{name}} placeholders; unknown ones stay as they are."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def atomic_write(path: Path, text: str, newline: Optional[str] = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_durable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def tail_text(path: Optional[Path], chars: int) -> str:
    if path is None or not path.exists():
        return ""
    with open(path, "rb") as f:
        try:
            f.seek(-chars * 4, os.SEEK_END)
        except OSError:
            f.seek(0)
        return f.read().decode("utf-8", errors="replace")[-chars:]


# ---------------------------------------------------------------------------
# The console. Piped or redirected output (a log file, CI, tests) gets plain lines. A
# terminal gets the live view: colors, the agent's markdown rendered, and a status line
# under the output with the ticket, the step, the time it has run and the progress.

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# name: (on, off). Each off code ends only its own style, so styles nest.
STYLES = {
    "bold": ("1", "22"), "dim": ("2", "22"), "italic": ("3", "23"), "underline": ("4", "24"),
    "reverse": ("7", "27"), "strike": ("9", "29"),
    "red": ("31", "39"), "green": ("32", "39"), "yellow": ("33", "39"), "blue": ("34", "39"),
    "magenta": ("35", "39"), "cyan": ("36", "39"), "gray": ("90", "39"),
}
STEP_COLORS = {"implement": "blue", "review": "magenta", "verify": "cyan", "final": "yellow"}
UI_MODES = ("auto", "live", "plain")


def char_width(c: str) -> int:
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def visible_len(text: str) -> int:
    return sum(char_width(c) for c in ANSI_RE.sub("", text))


def clip(text: str, width: int) -> str:
    """`text` cut to `width` terminal columns, its escape codes kept."""
    if visible_len(text) <= width:
        return text
    out, used, i = [], 0, 0
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = char_width(text[i])
        if used + w > width - 1:
            break
        out.append(text[i])
        used += w
        i += 1
    return "".join(out) + "…" + ("\x1b[0m" if "\x1b" in text else "")


def enable_vt() -> bool:
    """Escape codes work in the console (Windows 10+ needs them switched on)."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def rich_available() -> bool:
    """The rich package: installers (uv, pipx) bring it; a copied run_tickets.py run with
    plain `python` works without it, with simpler code blocks and tables."""
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


class Console:
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self) -> None:
        self.live = False  # the live view; False: plain lines
        self.color = False
        self.tty = False  # the status line is drawn only on a terminal
        self.rich = False  # code blocks highlighted and tables framed by the rich package
        self.hidden = False  # the cursor, while the status line is drawn
        self.lock = threading.RLock()
        self.status: Optional[Callable[[int], List[str]]] = None
        self.shown = 0  # status lines on the screen now
        self.frame = 0

    def setup(self, mode: str) -> None:
        mode = (mode or "auto").lower()
        if mode not in UI_MODES:
            raise UsageError(f"ui must be one of {', '.join(UI_MODES)}, not '{mode}'")
        self.tty = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        if mode == "auto":
            self.live = self.tty and enable_vt()
        else:
            self.live = mode == "live"
            if self.live:
                enable_vt()
        self.color = self.live and not os.environ.get("NO_COLOR")
        self.rich = self.color and rich_available()

    def rich_render(self, renderable: Any, width: int) -> List[str]:
        """Lines of escape-coded text that rich draws for `renderable`."""
        import io
        from rich.console import Console as RichConsole
        buf = io.StringIO()
        RichConsole(file=buf, force_terminal=True, color_system="256", width=max(20, width),
                    highlight=False, markup=False, emoji=False, legacy_windows=False).print(renderable)
        return buf.getvalue().rstrip("\n").split("\n")

    def style(self, text: str, *names: str) -> str:
        if not self.color or not text or not names:
            return text
        on = ";".join(STYLES[n][0] for n in names)
        off = ";".join(dict.fromkeys(STYLES[n][1] for n in names))
        return f"\x1b[{on}m{text}\x1b[{off}m"

    def width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 24)).columns)

    def path_arg(self, p: Any) -> str:
        """A path to paste into a shell command: short when it can be, quoted when it must
        be (a quoted ~/ would not expand, so a quoted path is absolute)."""
        short = self.path(p)
        if re.fullmatch(r"[\w@%+=:,./~\\-]+", short):
            return short
        full = str(p)
        return f'"{full}"' if IS_WINDOWS else shlex.quote(full)

    def path(self, p: Any) -> str:
        """A path as short as it can be read: relative to the current directory, or ~/."""
        p = Path(str(p))
        for base, prefix in ((Path.cwd(), ""), (Path.home(), "~" + os.sep)):
            try:
                rel = p.relative_to(base)
            except ValueError:
                continue
            if rel.parts:
                return prefix + str(rel)
        return str(p)

    # -- writing, around the status lines -----------------------------------

    def _clear(self) -> str:
        if not self.shown:
            return ""
        up = f"\x1b[{self.shown - 1}A" if self.shown > 1 else ""
        self.shown = 0
        return "\r" + up + "\x1b[J"

    def _draw(self) -> str:
        if self.status is None:
            return ""
        try:
            lines = self.status(self.frame)
        except Exception:  # a status line must never take the run down
            lines = []
        width = self.width() - 1
        lines = [clip(line, width) for line in lines]
        self.shown = len(lines)
        return "\n".join(lines)

    def write(self, text: str, err: bool = False) -> None:
        with self.lock:
            if self.shown:
                sys.stdout.write(self._clear())
                sys.stdout.flush()
            stream = sys.stderr if err else sys.stdout
            stream.write(text + "\n")
            stream.flush()
            if self.status is not None:
                sys.stdout.write(self._draw())
                sys.stdout.flush()

    @contextlib.contextmanager
    def showing(self, status: Callable[[int], List[str]]) -> Iterator[None]:
        """Keeps `status(frame)` drawn under the output, redrawn ~8 times a second. Not
        when the live view goes to a file (--ui live > log): there it would be noise."""
        if not (self.live and self.tty):
            yield
            return
        outer = self.status
        with self.lock:
            self.status = status
            if outer is None:
                self.hidden = True
            sys.stdout.write(self._clear() + ("" if outer else "\x1b[?25l") + self._draw())
            sys.stdout.flush()
        if outer is not None:  # the outer ticker keeps drawing, now this status
            try:
                yield
            finally:
                with self.lock:
                    self.status = outer
            return
        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(0.12):
                with self.lock:
                    self.frame += 1
                    sys.stdout.write(self._clear() + self._draw())
                    sys.stdout.flush()

        ticker = threading.Thread(target=tick, daemon=True)
        ticker.start()
        try:
            yield
        finally:
            stop.set()
            ticker.join(timeout=2)
            with self.lock:
                self.status = None
                self.hidden = False
                sys.stdout.write(self._clear() + "\x1b[?25h")
                sys.stdout.flush()

    # -- pieces of the live view ---------------------------------------------

    def rule(self, title: str, right: str = "", color: str = "blue") -> str:
        width = min(self.width() - 1, 100)
        head = f"━━ {title} "
        tail = f" {right} ━━" if right else ""
        fill = max(3, width - visible_len(head) - visible_len(tail))
        return self.style(head, color, "bold") + self.style("━" * fill, color) + self.style(tail, color)

    def box(self, lines: List[str], title: str = "", color: str = "gray") -> List[str]:
        """`lines` framed on the left, under a title. Nothing is aligned on the right, so
        long lines (paths, commands) wrap by themselves and copy without frame chars."""
        top = "╭─" + (f" {title} " if title else "")
        width = min(self.width() - 1, 100, max([visible_len(l) + 2 for l in lines] + [visible_len(top) + 3]))
        rail = self.style("│", color)
        return ([self.style(top + "─" * max(3, width - visible_len(top)), color)]
                + [f"{rail} {line}" if line else rail for line in lines]
                + [self.style("╰" + "─" * (width - 1), color)])

    def bar(self, done: int, total: int, width: int = 16) -> str:
        filled = round(width * done / total) if total else 0
        if not self.color:
            return "█" * filled + "░" * (width - filled)
        return self.style("━" * filled, "green") + self.style("━" * (width - filled), "gray")

    def chip(self, step: str) -> str:
        color = STEP_COLORS.get(step, "gray")
        return self.style(f" {step} ", "reverse", color, "bold") if self.color else f"[{step}]"

    def ticket_icon(self, status: str, current: bool = False) -> str:
        if current:
            return self.style("●", "yellow")
        if status == DONE:
            return self.style("✓", "green")
        if status == "wontfix":
            return self.style("–", "gray")
        if status == RUNNABLE:
            return "○"
        return self.style("!", "yellow")


UI = Console()
atexit.register(lambda: UI.hidden and sys.stdout.write("\x1b[?25h"))  # a cursor hidden by a crash


def say(msg: str, live: Optional[str] = None) -> None:
    """A line of output; `live` is how the live view shows it, when that differs."""
    if not UI.live:
        print(msg, flush=True)
        return
    if live is None:
        live = msg
        m = re.match(r"^(\s*)==> (.*)$", msg, re.DOTALL)
        if m:
            live = m.group(1) + UI.style("›", "cyan") + " " + m.group(2)
    UI.write(live)


def warn(msg: str) -> None:
    if UI.live:
        UI.write(UI.style(msg, "yellow"), err=True)
    else:
        print(msg, file=sys.stderr, flush=True)


class Markdown:
    """Renders markdown for the terminal line by line, as an agent prints it."""

    # An info string after backticks has no backticks: "```bash ls```" is inline code.
    FENCE_RE = re.compile(r"^(\s*)(`{3,}(?=[^`]*$)|~{3,})\s*(\S*)")
    DIFF_START_RE = re.compile(r"^(diff --git |@@ -\d)")
    DIFF_LINE_RE = re.compile(r"^([-+ @\\]|diff |index |new file|deleted file|similarity |rename )")
    HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
    RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
    TASK_RE = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
    BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
    ORDERED_RE = re.compile(r"^(\s*)(\d+)([.)])\s+(.*)$")
    QUOTE_RE = re.compile(r"^(\s*)>\s?(.*)$")
    TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")

    def __init__(self, ui: Console):
        self.ui = ui
        self.fence = ""
        self.lang = ""
        self.code: List[str] = []  # a code block waiting for its end, to be highlighted (rich)
        self.table: List[str] = []
        self.diff = False  # inside a diff the agent printed: colored, not markdown

    def feed(self, line: str) -> List[str]:
        out: List[str] = []
        if self.table and (self.fence or not self.TABLE_RE.match(line)):
            out += self.flush_table()
        st = self.ui.style
        if not self.fence:
            self.diff = bool(self.DIFF_START_RE.match(line) or (self.diff and self.DIFF_LINE_RE.match(line)))
            if self.diff:
                color = {"+": "green", "-": "red", "@": "cyan"}.get(line[:1])
                return out + [st(line, color) if color and not line.startswith(("+++", "---")) else
                              st(line, "bold") if line.startswith(("diff ", "+++", "---")) else line]
        if self.fence:
            if re.match(rf"^\s*{re.escape(self.fence[0])}{{{len(self.fence)},}}\s*$", line):
                self.fence = ""
                return out + self.flush_code() + [st("└─", "gray")]
            if self.ui.rich:
                self.code.append(line)
                return out
            return out + [st("│ ", "gray") + line]
        m = self.FENCE_RE.match(line)
        if m:
            self.fence = m.group(2)
            self.lang = m.group(3).lstrip("`~")
            return out + [st("┌─" + (f" {self.lang}" if self.lang else ""), "gray")]
        if self.TABLE_RE.match(line):
            self.table.append(line)
            return out
        return out + [self.block(line)]

    def flush(self) -> List[str]:
        """What is still held back when a piece of output ends (the agent's output, or one
        of Claude's messages): a table, an unclosed code block."""
        out = self.flush_table() + self.flush_code()
        self.fence = ""
        return out

    def flush_code(self) -> List[str]:
        code, self.code = self.code, []
        if not code:
            return []
        try:
            from rich.syntax import Syntax
            lines = self.ui.rich_render(Syntax("\n".join(code), self.lang or "text", theme="ansi_dark",
                                               background_color="default", word_wrap=True),
                                        self.ui.width() - 8)
        except Exception:  # noqa: BLE001 - rich is a nicety; the plain lines always work
            lines = code
        return [self.ui.style("│ ", "gray") + line for line in lines]

    def flush_table(self) -> List[str]:
        rows, self.table = self.table, []
        if not rows:
            return []
        if len(rows) < 2 or not self.is_separator(rows[1]):
            return [self.inline(r) for r in rows]
        cells = [[self.inline(c.strip()) for c in r.strip().strip("|").split("|")]
                 for i, r in enumerate(rows) if i != 1]
        ncols = max(len(r) for r in cells)
        cells = [r + [""] * (ncols - len(r)) for r in cells]
        indent = re.match(r"^\s*", rows[0]).group(0)  # type: ignore[union-attr]
        if self.ui.rich:
            try:
                return [indent + line for line in self.rich_table(cells, rows[1])]
            except Exception:  # noqa: BLE001
                pass
        widths = [max(visible_len(r[i]) for r in cells) for i in range(ncols)]
        sep = self.ui.style(" │ ", "gray")

        def row(r: List[str], bold: bool = False) -> str:
            text = sep.join(c + " " * (w - visible_len(c)) for c, w in zip(r, widths))
            return indent + (self.ui.style(text, "bold") if bold else text)

        line = self.ui.style("─┼─".join("─" * w for w in widths), "gray")
        return [row(cells[0], True), indent + line] + [row(r) for r in cells[1:]]

    def rich_table(self, cells: List[List[str]], separator: str) -> List[str]:
        """The table drawn by rich: a frame, cells wrapped to the terminal's width."""
        from rich import box
        from rich.table import Table
        from rich.text import Text
        aligns = []
        for spec in separator.strip().strip("|").split("|"):
            spec = spec.strip()
            aligns.append("center" if spec.startswith(":") and spec.endswith(":") else
                          "right" if spec.endswith(":") else "left")
        table = Table(box=box.ROUNDED, border_style="bright_black", header_style="bold",
                      show_header=True, pad_edge=True)
        for i, head in enumerate(cells[0]):
            table.add_column(Text.from_ansi(head), justify=aligns[i] if i < len(aligns) else "left")  # type: ignore[arg-type]
        for row in cells[1:]:
            table.add_row(*(Text.from_ansi(c) for c in row))
        return self.ui.rich_render(table, self.ui.width() - 8)

    @staticmethod
    def is_separator(line: str) -> bool:
        return "-" in line and not set(line.strip()) - set("|-: ")

    def block(self, line: str) -> str:
        st = self.ui.style
        m = self.HEADING_RE.match(line)
        if m:
            level, text = len(m.group(1)), self.inline(m.group(2))
            if level == 1:
                return st(text, "bold", "underline", "magenta")
            return st(text, "bold", "blue") if level == 2 else st(text, "bold")
        if self.RULE_RE.match(line):
            return st("─" * min(self.ui.width() - 8, 60), "gray")
        m = self.TASK_RE.match(line)
        if m:
            done = m.group(2) != " "
            mark = st("✓", "green") if done else "○"
            return f"{m.group(1)}{mark} {self.inline(m.group(3))}"
        m = self.BULLET_RE.match(line)
        if m:
            dot = "•◦▪"[min(len(m.group(1).expandtabs(4)) // 2, 2)]
            return f"{m.group(1)}{st(dot, 'cyan')} {self.inline(m.group(2))}"
        m = self.ORDERED_RE.match(line)
        if m:
            return f"{m.group(1)}{st(m.group(2) + m.group(3), 'cyan')} {self.inline(m.group(4))}"
        m = self.QUOTE_RE.match(line)
        if m:
            return f"{m.group(1)}{st('▎', 'gray')} {st(self.inline(m.group(2)), 'italic')}"
        return self.inline(line)

    # Emphasis opens only after a space, a bracket or a quote and closes only before one
    # or before punctuation, so paths, globs and URLs (pkg/__init__.py, src/*.py,
    # http://h/_a_) stay as they are.
    _OPEN, _CLOSE = r"(?<![^\s(\[{\"'])", r"(?![^\s.,;:!?)\]}\"'])"
    EMPHASIS = [(re.compile(_OPEN + r"\*\*(?![\s*])(.+?)(?<![\s*])\*\*" + _CLOSE), "bold"),
                (re.compile(_OPEN + r"__(?![\s_])(.+?)(?<![\s_])__" + _CLOSE), "bold"),
                (re.compile(_OPEN + r"\*(?![\s*])(.+?)(?<![\s*])\*" + _CLOSE), "italic"),
                (re.compile(_OPEN + r"_(?![\s_])(.+?)(?<![\s_])_" + _CLOSE), "italic"),
                (re.compile(_OPEN + r"~~(?!\s)(.+?)(?<!\s)~~" + _CLOSE), "strike")]

    def inline(self, text: str) -> str:
        st = self.ui.style
        out = []
        for i, part in enumerate(re.split(r"(`+)(.+?)\1", text)):
            kind = i % 3  # re.split with two groups: text, the backticks, the code
            if kind == 1:
                continue
            if kind == 2:
                out.append(st(part, "cyan"))
                continue
            # Links: text, label, url, text, ...; the url is printed as it is.
            pieces = re.split(r"\[([^\]\n]+)\]\(([^)\s]+)\)", part)
            for j in range(0, len(pieces), 3):
                out.append(self.emphasis(pieces[j]))
                if j + 2 < len(pieces):
                    label, url = pieces[j + 1], pieces[j + 2]
                    out.append(st(self.emphasis(label), "underline")
                               + (st(f" ({url})", "gray") if url != label else ""))
        return "".join(out)

    def emphasis(self, text: str) -> str:
        for pattern, style in self.EMPHASIS:
            text = pattern.sub(lambda m, s=style: self.ui.style(m.group(1), s), text)
        return text


def path_matches(path: str, pattern: str) -> bool:
    """'migrations/' matches that directory at any depth; other patterns are globs
    (fnmatch, where * also crosses /), matched against the path and its parents."""
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/"):
        return ("/" + path).find("/" + pattern) >= 0
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/") + "/*")


# Test files, for the "constants changed in tests" check.
TEST_FILE_RE = re.compile(r"(^|/)(tests?|specs?|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
NUMBER_RE = re.compile(r"\d+(?:[._]\d+)*")


# ---------------------------------------------------------------------------
# Provider limits


def parse_limit_wait(text: str, default: int, at: Optional[dt.datetime] = None) -> int:
    """Seconds to wait for a provider limit to lift, from the agent's error text."""
    at = at or now()
    m = re.search(r"\|(\d{10})\b", text)  # "usage limit reached|1760000000"
    if m:
        return max(1, int(m.group(1)) - int(at.timestamp()) + 60)
    m = re.search(r"retry[- ]after[\"':= ]+(\d+)", text, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"(?:resets?|try again|retry)\s+in\s+(\d+)\s*(h(?:ours?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)\b",
                  text, re.IGNORECASE)
    if m:
        unit = m.group(2)[0].lower()
        return max(1, int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[unit])
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text, re.IGNORECASE)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            target = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= at:
                target += dt.timedelta(days=1)
            return int((target - at).total_seconds()) + 120
    return default


# ---------------------------------------------------------------------------
# Tickets

# "**Status:** done", "**Status**: done", "Status: done", "- Status: done".
STATUS_RE = re.compile(r"^\s*(?:[-*]\s+)?\**\s*status\s*(?::\s*\**|\**\s*:)\s*\**\s*([a-z][a-z_-]*)",
                       re.IGNORECASE)
KIND_RE = re.compile(r"^\s*(?:[-*]\s+)?\**\s*kind\s*(?::\s*\**|\**\s*:)\s*\**\s*([a-z][a-z_-]*)",
                     re.IGNORECASE)
BLOCKED_RE = re.compile(r"^\s*(?:[-*]\s+)?\**\s*blocked[ -]by\s*(?::\s*\**|\**\s*:)\s*(.*)$",
                        re.IGNORECASE)
NO_BLOCKERS_RE = re.compile(r"^\W*(none|no blockers?|nothing|n/?a)\b|^\W*$", re.IGNORECASE)
BLOCKED_HEADING_RE = re.compile(r"^#+\s*blocked[ -]by\s*:?\s*$", re.IGNORECASE)
# A ticket number on the "Blocked by" line: "04", "#4", "04-slug"; not the digits of "PROJ-1234".
BLOCKER_NUM_RE = re.compile(r"(?<![\w-])#?(\d{1,4})(?=$|[^\w-]|-[A-Za-z])")
UNCHECKED_RE = re.compile(r"^\s*[-*+]\s+\[ \]", re.MULTILINE)
CHECKED_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\]", re.MULTILINE)
TICKET_NAME_RE = re.compile(r"^(\d+)-.*\.md$")


class Ticket:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.number = int(TICKET_NAME_RE.match(path.name).group(1))  # type: ignore[union-attr]
        self.cached: Optional[str] = None  # set only inside Feature.snapshot()

    def text(self) -> str:
        if self.cached is not None:
            return self.cached
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def status(self) -> str:
        for line in self.text().splitlines():
            m = STATUS_RE.match(line)
            if m:
                return m.group(1).lower()
        return ""

    def kind(self) -> str:
        """`**Kind:** report` marks a ticket whose result is not code."""
        for line in self.text().splitlines():
            m = KIND_RE.match(line)
            if m:
                return m.group(1).lower()
        return ""

    def title(self) -> str:
        """'# 06: User exports a report' -> 'User exports a report'."""
        for line in self.text().splitlines():
            m = re.match(r"^#\s+(?:\d+\s*[:.)-]\s*)?(.+?)\s*$", line)
            if m:
                return m.group(1)
        return ""

    def blocked_text(self) -> str:
        """The text of the "Blocked by" line, or of the "## Blocked by" section."""
        lines = self.text().splitlines()
        for i, line in enumerate(lines):
            m = BLOCKED_RE.match(line)
            if m:
                return m.group(1).strip()
            if BLOCKED_HEADING_RE.match(line):
                # "## Blocked by" with "- Blocked by #12" items below (the GitHub-issue form).
                items = []
                for item in lines[i + 1:]:
                    if item.lstrip().startswith("#"):
                        break
                    items.append(item.strip())
                return " ".join(i for i in items if i)
        return ""

    def blockers(self) -> List[int]:
        """Ticket numbers named on the "Blocked by" line."""
        return sorted({int(n) for n in BLOCKER_NUM_RE.findall(self.blocked_text())})

    def unchecked(self) -> int:
        return len(UNCHECKED_RE.findall(self.text()))

    def checked(self) -> int:
        return len(CHECKED_RE.findall(self.text()))

    # Edits made by the retry / reset / skip commands, never during a run.

    def _rewrite(self, text: str) -> None:
        atomic_write(self.path, text, newline="")

    def _raw(self) -> str:
        with open(self.path, encoding="utf-8", newline="") as f:
            return f.read()

    def set_status(self, status: str) -> None:
        text = self._raw()
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            m = STATUS_RE.match(line)
            if m:
                lines[i] = line[:m.start(1)] + status + line[m.end(1):]
                self._rewrite("".join(lines))
                return
        nl = "\r\n" if "\r\n" in text else "\n"
        at = 1 if lines and lines[0].startswith("#") else 0
        lines.insert(at, f"{nl}**Status:** {status}{nl}")
        self._rewrite("".join(lines))

    def untick(self) -> None:
        self._rewrite(re.sub(r"^(\s*[-*+]\s+)\[[xX]\]", r"\1[ ]", self._raw(), flags=re.MULTILINE))

    def add_comment(self, comment: str) -> None:
        text = self._raw()
        nl = "\r\n" if "\r\n" in text else "\n"
        if not re.search(r"^##\s+Comments\s*$", text, re.MULTILINE):
            text = text.rstrip("\r\n") + f"{nl}{nl}## Comments{nl}"
        self._rewrite(text.rstrip("\r\n") + f"{nl}{nl}{comment}{nl}")


class FinalUnit:
    """The feature-wide final review, run like a ticket step but with no ticket file."""

    def __init__(self, feature: "Feature"):
        self.name = FINAL
        self.path = feature.dir
        self.number = 10 ** 6

    def status(self) -> str:
        return ""

    def kind(self) -> str:
        return ""


class Feature:
    def __init__(self, path: Path):
        self.dir = path.resolve()
        self.issues = self.dir / "issues"
        self.runs = self.dir / "runs"
        if not self.issues.is_dir():
            raise UsageError(f"{self.dir} has no issues/ directory")
        self._snapshot: Optional[List[Ticket]] = None

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[None]:
        """Inside it each ticket file is read once, however often the tickets are asked
        about (blockers look at every other ticket). For views only: the runner's own
        checks always read the files afresh."""
        tickets = self.tickets()
        for t in tickets:
            t.cached = t.text()
        self._snapshot = tickets
        try:
            yield
        finally:
            self._snapshot = None

    @property
    def name(self) -> str:
        return self.dir.name

    def spec(self) -> Optional[Path]:
        p = self.dir / "spec.md"
        return p if p.exists() else None

    def spec_title(self) -> str:
        spec = self.spec()
        if spec:
            for line in spec.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^#\s+(.+?)\s*$", line)
                if m:
                    return m.group(1)
        return self.name

    def tickets(self) -> List[Ticket]:
        if self._snapshot is not None:
            return list(self._snapshot)
        found = [Ticket(p) for p in self.issues.iterdir()
                 if p.is_file() and TICKET_NAME_RE.match(p.name)]
        return sorted(found, key=lambda t: (t.number, t.name))

    def find(self, ref: str) -> Ticket:
        """A ticket by '5', '05', '05-slug' or '05-slug.md'."""
        ref = ref.strip()
        if ref.endswith(".md"):
            ref = ref[:-3]
        matches = [t for t in self.tickets()
                   if t.name == ref or (ref.isdigit() and t.number == int(ref))]
        if len(matches) != 1:
            raise UsageError(f"no single ticket matches '{ref}' in {self.issues}"
                             + (f" ({', '.join(t.name for t in matches)})" if matches else ""))
        return matches[0]

    def by_number(self) -> Dict[int, List[Ticket]]:
        out: Dict[int, List[Ticket]] = {}
        for t in self.tickets():
            out.setdefault(t.number, []).append(t)
        return out

    def blockers_of(self, ticket: Ticket) -> Tuple[List[int], str]:
        """(blocking ticket numbers, unrecognised "Blocked by" text). /to-tickets lists
        blockers by number or by title; text naming neither is unrecognised, and a ticket
        with unrecognised blockers waits: running it early is worse than a stop."""
        text = ticket.blocked_text()
        if not text or NO_BLOCKERS_RE.match(text):
            return [], ""
        found = set(ticket.blockers())
        low = re.sub(r"\s+", " ", text.lower())
        for other in self.tickets():
            title = re.sub(r"\s+", " ", other.title().lower())
            slug = other.name.split("-", 1)[-1].lower()
            if (len(title) >= 4 and title in low) or (len(slug) >= 4 and slug in low):
                found.add(other.number)
        found.discard(ticket.number)
        return sorted(found), ("" if found else text)

    def waiting_on(self, ticket: Ticket, index: Dict[int, List[Ticket]]) -> List[str]:
        """Why a ticket cannot start yet: its blockers that are not done."""
        numbers, unknown = self.blockers_of(ticket)
        out = [f"unrecognised 'Blocked by: {unknown}'"] if unknown else []
        for n in numbers:
            if n == ticket.number:
                continue
            group = index.get(n)
            if not group:
                out.append(f"{n:02d} (no such ticket)")
            else:
                out += [f"{b.name} ({b.status() or 'no status'})" for b in group if b.status() != DONE]
        return out

    def next_ticket(self) -> Optional[Ticket]:
        index = self.by_number()
        for t in self.tickets():
            if t.status() == RUNNABLE and not self.waiting_on(t, index):
                return t
        return None

    def open_tickets(self) -> List[Ticket]:
        return [t for t in self.tickets() if t.status() not in CLOSED]


# ---------------------------------------------------------------------------
# Git


class Git:
    def __init__(self, cwd: Path):
        if not shutil.which("git"):
            raise UsageError("git is not on PATH")
        top = self._run(["rev-parse", "--show-toplevel"], cwd=cwd)
        if top.returncode != 0:
            raise UsageError(f"{cwd} is not inside a git repository")
        self.repo = Path(top.stdout.strip()).resolve()
        # Per worktree: a runner in another worktree of the repo works on another branch.
        self.git_dir = Path(self.out("rev-parse", "--absolute-git-dir"))

    @staticmethod
    def _run(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(list(args), cwd=self.repo)

    def out(self, *args: str, keep_indent: bool = False) -> str:
        r = self.run(*args)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.rstrip() if keep_indent else r.stdout.strip()

    def porcelain(self, *args: str) -> str:
        """`git status --porcelain`: the leading space of " M file" is part of each line."""
        return self.out("status", "--porcelain", *args, keep_indent=True)

    def head(self) -> str:
        r = self.run("rev-parse", "--verify", "-q", "HEAD")
        if r.returncode != 0:
            raise UsageError("the repository has no commits yet; make an initial commit first")
        return r.stdout.strip()

    def branch(self) -> str:
        r = self.run("symbolic-ref", "-q", "--short", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else "(detached)"

    def has_branch(self, name: str) -> bool:
        return self.run("rev-parse", "--verify", "-q", f"refs/heads/{name}").returncode == 0

    def stash(self) -> str:
        r = self.run("rev-parse", "-q", "--verify", "refs/stash")
        return r.stdout.strip() if r.returncode == 0 else ""

    def is_ancestor(self, a: str, b: str = "HEAD") -> bool:
        return bool(a) and self.run("merge-base", "--is-ancestor", a, b).returncode == 0

    def tracked_changes(self, exclude: Optional[str] = None) -> str:
        """Uncommitted changes to tracked files, ignoring `exclude` (the feature dir,
        relative to the repo root: the agent edits the ticket, the runner writes runs/)."""
        args = ["--untracked-files=no", "--", "."]
        if exclude:
            args.append(":(exclude)" + exclude)
        return self.porcelain(*args)

    def changed_files(self, since: str) -> List[str]:
        """Files changed in commits since `since` plus uncommitted tracked changes."""
        files = set(self.out("diff", "--name-only", f"{since}..HEAD").splitlines())
        for line in self.porcelain("--untracked-files=no").splitlines():
            files.add(line[3:].split(" -> ")[-1].strip('"'))
        return sorted(f for f in files if f)

    def commits(self, base: str) -> List[str]:
        out = self.out("log", "--format=%h %s", f"{base}..HEAD")
        return [line for line in out.splitlines() if line]

    def hunks(self, base: str) -> List[Tuple[str, List[str], List[str]]]:
        """(file, removed lines, added lines) of every hunk between base and HEAD."""
        out = self.out("diff", "--unified=0", "--no-color", f"{base}..HEAD")
        result: List[Tuple[str, List[str], List[str]]] = []
        current = ""
        for line in out.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("--- ") or line.startswith("diff "):
                continue
            elif line.startswith("@@"):
                result.append((current, [], []))
            elif result and line.startswith("-"):
                result[-1][1].append(line[1:])
            elif result and line.startswith("+"):
                result[-1][2].append(line[1:])
        return result


# ---------------------------------------------------------------------------
# Locks: one runner per feature, and one per working tree (two runners would fight
# over the same branch).


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    def __init__(self, path: Path, feature: Path):
        self.path = path
        self.feature = feature
        self.held = False

    def acquire(self) -> None:
        me = {"pid": os.getpid(), "host": socket.gethostname(), "feature": str(self.feature),
              "started": iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._take_over_stale():
                    raise UsageError(self._busy_message())
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(me, f)
            self.held = True
            return
        raise UsageError(self._busy_message())

    def read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def alive(self) -> bool:
        info = self.read()
        return bool(info) and info.get("host") == socket.gethostname() and pid_alive(int(info.get("pid", 0)))

    def _take_over_stale(self) -> bool:
        info = self.read()
        if not info:
            # Unreadable or half-written: only stale when old enough to not be a race.
            try:
                if time.time() - self.path.stat().st_mtime < 60:
                    return False
            except OSError:
                return True
        elif info.get("host") != socket.gethostname() or pid_alive(int(info.get("pid", 0))):
            return False
        warn(f"removing a stale lock left by pid {info.get('pid')} ({self.path})")
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def _busy_message(self) -> str:
        info = self.read()
        return (f"another run-tickets holds {self.path}: pid {info.get('pid')} on "
                f"{info.get('host')} since {info.get('started')}, feature {info.get('feature')}.\n"
                f"If that process is gone (another machine sharing the repo?), delete the file.")

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False


# ---------------------------------------------------------------------------
# State: what the runner knows that the ticket files do not.


class State:
    """runs/state.json, rewritten atomically on every change.

    tickets.<name> (and tickets.final-review for the final review):
      phase      implement | review | verify | final | done
      running    true while an agent process for `phase` may be alive
      base       HEAD before the ticket's first implement attempt (kept across attempts)
      step_head  HEAD before the running step
      stash      refs/stash before the running step (to check it after a crash too)
      branch     the branch the ticket runs on
      last_stop  the last failed check, if any
      runs, seconds, cost   totals over all agent runs of the ticket
    feature_base  HEAD before the feature's first ticket (the final review's base)
    git           branch, base_branch, worktree of the run
    current       the agent process running right now, for `status`
    waiting       a provider-limit wait in progress
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {"version": 1, "tickets": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as e:
                raise UsageError(f"{path} is corrupt ({e}); fix or move it away to start over")
            self.data.setdefault("tickets", {})

    def ticket(self, name: str) -> Dict[str, Any]:
        return self.data["tickets"].setdefault(name, {})

    def update(self, name: str, **fields: Any) -> None:
        entry = self.ticket(name)
        entry.update(fields)
        entry["updated"] = iso()
        self.save()

    def set(self, **fields: Any) -> None:
        self.data.update(fields)
        self.save()

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Config


def load_toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            raise UsageError(f"{path} needs Python 3.11+ or `pip install tomli` to be read")
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise UsageError(f"{path}: {e}")


def find_config(feature: Path, repo: Path, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise UsageError(f"config {p} not found")
        return p
    for d in (feature, repo):
        for name in CONFIG_NAMES:
            if (d / name).is_file():
                return d / name
    return None


def resolve_agent(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    agent = dict(AGENT_PRESETS.get(name, {}))
    agent.update((cfg.get("agents") or {}).get(name, {}))
    if not agent.get("command"):
        raise UsageError(f"unknown agent '{name}'; known: {', '.join(AGENT_PRESETS)}, "
                         f"or define [agents.{name}] command = [...] in the config")
    agent.setdefault("model_args", ["--model", "{{model}}"])
    agent.setdefault("budget_args", [])
    agent.setdefault("prompt_via", "arg")
    agent.setdefault("skill_format", "/{{skill}} {{args}}")
    agent.setdefault("output", "text")
    agent.setdefault("export_command", [])
    agent.setdefault("markdown", True)
    if agent["prompt_via"] not in ("arg", "stdin"):
        raise UsageError(f"[agents.{name}] prompt_via must be 'arg' or 'stdin'")
    return agent


# Command-line flags that override config keys.
CLI_KEYS = ("agent", "model", "max_time", "retries", "branch", "worktree", "final_review",
            "test_command", "ui")


def build_config(path: Optional[Path], args: argparse.Namespace) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = load_toml(path) if path else {}
    unknown = sorted(set(raw) - set(cfg) - {"agents", "prompts"})
    if unknown:
        raise UsageError(f"{path}: unknown key(s) {', '.join(unknown)}; "
                         f"see `run-tickets init` for the list")
    sources = {key: "default" for key in cfg}
    for key, value in raw.items():
        sources[key] = "config file"
        if key in ("skills", "steps") and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    for key in CLI_KEYS:
        v = getattr(args, key, None)
        if v is not None and v != "":
            cfg[key] = v
            sources[key] = "command line"
            if key in ("agent", "model", "max_time"):
                for step in cfg["steps"].values():
                    if isinstance(step, dict):
                        step.pop(key, None)
    for ref in getattr(args, "pause_after", None) or []:
        cfg["pause_after"] = list(cfg["pause_after"]) + [ref]
        sources["pause_after"] = "command line"
    cfg["sources"] = sources

    for step, over in cfg["steps"].items():
        if step not in STEPS or not isinstance(over, dict):
            raise UsageError(f"[steps.{step}]: steps are {', '.join(STEPS)}")
        bad = set(over) - {"agent", "model", "max_time"}
        if bad:
            raise UsageError(f"[steps.{step}]: unknown key(s) {', '.join(sorted(bad))}; "
                             f"a step can set agent, model, max_time")
    cfg["step"] = {}
    for step in STEPS:
        over = cfg["steps"].get(step, {})
        if step == "verify":  # the review-like step for tickets without a diff
            over = {**cfg["steps"].get("review", {}), **over}
        name = over.get("agent", cfg["agent"])
        max_time = over.get("max_time", cfg["max_time"])
        cfg["step"][step] = {"agent": name, "model": over.get("model", cfg["model"]),
                             "max_time": max_time, "max_seconds": parse_duration(max_time),
                             "spec": resolve_agent(cfg, name)}
    cfg["agent_spec"] = cfg["step"]["implement"]["spec"]

    cfg["max_seconds"] = parse_duration(cfg["max_time"])
    cfg["retries"] = max(0, int(cfg["retries"]))
    for key in ("retry_delay", "test_timeout", "limit_default_wait", "limit_max_wait",
                "log_retention"):
        cfg[key + "_seconds"] = parse_duration(cfg[key])
    for key in ("preflight", "protected", "pause_after", "worktree_setup"):
        if isinstance(cfg.get(key), str):
            cfg[key] = [cfg[key]]
    cfg["pause_after"] = [str(p) for p in cfg["pause_after"]]
    cfg["max_cost"] = float(cfg["max_cost"] or 0)
    cfg["max_step_cost"] = float(cfg["max_step_cost"] or 0)
    cfg["config_path"] = str(path) if path else ""
    if str(cfg["ui"]).lower() not in UI_MODES:
        raise UsageError(f"ui must be one of {', '.join(UI_MODES)}, not '{cfg['ui']}'")
    return cfg


EXAMPLE_CONFIG = '''# run-tickets config: https://github.com/omanei/run-tickets#configuration
# Looked up as: --config, else .ticket-runner.toml in the feature dir, else in the repo
# root. Command-line flags win over this file. Every key is optional: the commented
# lines show the defaults; uncomment and change only what your project needs.
# `run-tickets config <feature dir>` prints the effective values and the agent commands.

agent = "{{init_agent}}"           # omp | pi | claude | codex, or a name under [agents.<name>]
# model = ""                # passed to the agent (its model_args) when set
# max_time = "3h"           # per step; the runner kills the agent's process tree after it
# retries = 1               # fresh-process retries of a failed step before the run stops
# retry_delay = "1m"        # pause before a retry
# desktop_notify = true     # macOS notification / notify-send / Windows balloon
# ui = "auto"               # live: colors, rendered markdown, a status line; plain: plain
#                           # lines; auto: live on a terminal, plain when piped (--ui)

# ---- Tests: the runner runs them itself and does not take the agent's word for it.
# test_command = ""         # e.g. "make test"; empty = no test gate
# test_timeout = "30m"
# test_baseline = true      # also before each ticket: failing tests there stop the run
#                           # before an agent is blamed for them

# ---- Provider limits: wait for a usage/rate limit to lift instead of stopping.
# limit_wait = true
# limit_default_wait = "30m"   # when the agent's error names no reset time
# limit_max_wait = "12h"       # per step; past it the run stops

# ---- Git: everything stays local. The runner never pushes; the result is a branch in
# the repository, and pushing it is up to you.
# branch = ""               # e.g. "feat/{{feature}}"; empty = the current branch
# worktree = false          # true: work in a separate git worktree on that branch, so
#                           # your own checkout stays untouched (and usable) all night;
#                           # when every ticket is done the worktree folder is removed and
#                           # the branch stays in the repository. Not for projects whose
#                           # tests run in a container that mounts your main checkout:
#                           # those tests would see your checkout's code, not the worktree's
# worktree_dir = "{{repo}}/../{{repo_name}}-worktrees/{{feature}}"
# worktree_setup = []       # shell commands run once in a new worktree, which has only
#                           # tracked files; $RUN_TICKETS_MAIN_REPO is your checkout, e.g.
#                           # ['cp "$RUN_TICKETS_MAIN_REPO/.env" .', "npm ci"]

# ---- The end of the feature.
# final_review = true       # one more review of the whole feature against the spec
# pause_after = []          # e.g. ["03"]: stop after these tickets for a human look

# ---- Guards.
# max_cost = 0              # USD per run, for agents that report cost (claude); 0 = none
# max_step_cost = 0         # USD per step, passed to agents that take it (claude)
# protected = []            # e.g. ["migrations/", ".github/", "*.lock"]: touching them stops the run

# ---- runs/ housekeeping: logs, transcripts and sessions older than this are deleted.
# log_retention = "30d"     # "0" keeps everything
# log_max_mb = 2000

# The agent ends each step with a report under this heading, in this language.
# report_heading = "Step report"     # e.g. "Отчёт о шаге"
# report_language = "English"        # e.g. "Russian"

# Shell commands that must succeed before the run and before every ticket; a failure
# stops the run before any agent starts. Run in the working tree (cmd.exe on Windows).
# preflight = [
#   "docker compose ps --status running --services | grep -q app",
# ]

# A shell command run on stop, pause and at the end. The text is in $RUN_TICKETS_MESSAGE
# (%RUN_TICKETS_MESSAGE% in cmd.exe); $RUN_TICKETS_SUMMARY and $RUN_TICKETS_REPORT hold
# the paths of SUMMARY.md and REPORT.md.
# notify_command = 'curl -s -X POST -H "Content-type: application/json" --data "{\\"text\\": \\"$RUN_TICKETS_MESSAGE\\"}" "$SLACK_WEBHOOK_URL"'

# Project rules, appended to the prompts.
# extra_instructions = ""                  # implement and review
# review_extra_instructions = ""           # e.g. "Run the tests only inside Docker."
# final_review_extra_instructions = ""

# Skill names. Skills from Claude Code plugins carry the plugin prefix: "<plugin>:code-review".
# [skills]
# implement = "implement"
# review = "code-review"

# Another agent, model or time limit for one step (implement, review, verify, final;
# verify, the check of a ticket without commits, takes review's settings unless set):
# [steps.review]
# agent = "codex"
# model = ""
# max_time = "1h"

# Change a preset or add your own agent. Placeholders: {{prompt}} {{repo}}
# {{session_dir}} {{final_file}}. model_args / budget_args go at a "{{model_args}}" /
# "{{budget_args}}" item, or right after the executable, and only when a model /
# max_step_cost is set. export_command gets {{session_file}} and {{html_file}}.
# [agents.omp]
# command = ["omp", "-p", "--auto-approve", "--session-dir", "{{session_dir}}", "--cwd", "{{repo}}", "{{prompt}}"]
# model_args = ["--model", "{{model}}"]
# budget_args = []
# prompt_via = "arg"                         # or "stdin"
# skill_format = "/skill:{{skill}} {{args}}"
# output = "text"                            # or "claude-stream-json"
# export_command = ["omp", "--export", "{{session_file}}"]

# Replace the prompt templates entirely. Placeholders: {{implement_command}}
# {{review_command}} {{final_command}} {{common}} {{git_rules}} {{ticket}} {{tickets}}
# {{base}} {{spec}} {{feature_dir}} {{repo}} {{resume_note}} {{report_heading}}
# {{report_language}} and the *_instructions keys.
# [prompts]
# common = """..."""
# implement = """..."""
# review = """..."""
# final = """..."""
'''


# ---------------------------------------------------------------------------
# Processes


def resolve_executable(name: str) -> List[str]:
    """argv prefix for an executable, with npm .cmd shims unwrapped on Windows: cmd.exe
    cannot pass a multi-line argument, so `node <script>` is called directly instead."""
    path = shutil.which(name)
    if path is None:
        raise UsageError(f"'{name}' is not on PATH")
    if IS_WINDOWS and path.lower().endswith((".cmd", ".bat")):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r'"%(?:~?dp0)%\\?([^"]+?\.(?:c|m)?js)"', text, re.IGNORECASE)
        if m:
            script = Path(path).parent / m.group(1).replace("\\", os.sep)
            local_node = Path(path).parent / "node.exe"
            node = str(local_node) if local_node.exists() else shutil.which("node")
            if node and script.exists():
                return [node, str(script)]
        return [path]
    return [path]


def _insert_args(template: List[str], marker: str, args: List[str]) -> None:
    if marker in template:
        i = template.index(marker)
        template[i:i + 1] = args
    else:
        # Right after the executable, before any positional prompt.
        template[1:1] = args


def fill_command(cfg: Dict[str, Any], step: str, prompt: str, repo: str, session_dir: str,
                 final_file: str) -> List[str]:
    """The agent's argv for a step, before the executable is resolved."""
    sc = cfg["step"][step]
    agent = sc["spec"]
    budget = cfg.get("max_step_cost") or 0
    values = {"prompt": prompt, "repo": repo, "session_dir": session_dir,
              "final_file": final_file, "model": sc["model"] or "",
              "budget": f"{budget:g}"}
    template = list(agent["command"])
    if agent["prompt_via"] == "stdin":
        template = [a for a in template if a != "{{prompt}}"]
    elif "{{prompt}}" not in template:
        template.append("{{prompt}}")
    _insert_args(template, "{{budget_args}}", list(agent["budget_args"]) if budget else [])
    _insert_args(template, "{{model_args}}", list(agent["model_args"]) if values["model"] else [])
    return [fill(a, values) for a in template]


def popen_group(argv: Any, **kwargs: Any) -> subprocess.Popen:
    """A process in its own group, so the whole tree can be killed."""
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def stream_items(line: str) -> List[Tuple[str, str, str]]:
    """What a claude stream-json line shows: ("text", markdown, ""), ("tool", name, hint)
    or ("result", summary, "")."""
    try:
        ev = json.loads(line)
    except ValueError:
        return [("text", line.rstrip("\n"), "")]
    if not isinstance(ev, dict):
        return []
    items = []
    message = ev.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if ev.get("type") == "assistant" and isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                hint = inp.get("description") or inp.get("command") or inp.get("file_path") or ""
                lines = str(hint).splitlines()
                items.append(("tool", str(c.get("name")), lines[0][:120] if lines else ""))
            elif c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].strip():
                items.append(("text", c["text"].rstrip(), ""))
    elif ev.get("type") == "result":
        try:
            cost = float(ev.get("total_cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        items.append(("result", f"{ev.get('subtype')}, {ev.get('num_turns')} turns, ${cost:.2f}", ""))
    return items


def describe_stream_event(line: str) -> Optional[str]:
    """One console line of live progress for a claude stream-json event."""
    parts = [f"  · {a}: {b}" if kind == "tool" else f"  · result: {a}" if kind == "result" else a
             for kind, a, b in stream_items(line)]
    return "\n".join(parts) if parts else None


def stream_result(log: Path) -> Tuple[str, bool, Optional[float]]:
    """(final answer, is_error, cost in USD) from a claude stream-json log."""
    text, is_error, cost = "", False, None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            text = ev.get("result") or text
            is_error = bool(ev.get("is_error"))
            try:
                cost = float(ev["total_cost_usd"]) if ev.get("total_cost_usd") is not None else cost
            except (TypeError, ValueError):
                pass
    return text, is_error, cost


def last_activity(log: Path, stream: bool) -> str:
    """The last meaningful line of a step's log, for `status`."""
    lines = [l for l in tail_text(log, 16000).splitlines() if l.strip()]
    for line in reversed(lines):
        shown = describe_stream_event(line) if stream else line
        if shown and shown.strip():
            return shown.strip().splitlines()[-1][:160]
    return ""


class StepRun:
    def __init__(self):
        self.code: Optional[int] = None
        self.timed_out = False
        self.log: Optional[Path] = None
        self.final: str = ""
        self.transcript: Optional[Path] = None
        self.seconds: float = 0
        self.cost: Optional[float] = None


# ---------------------------------------------------------------------------
# The working tree: the current checkout, a branch in it, or a separate worktree.


def prepare_workdir(feature: Feature, main: Git, cfg: Dict[str, Any], state: State) -> Git:
    values = {"feature": feature.name, "repo": str(main.repo), "repo_name": main.repo.name}
    branch = fill(cfg["branch"], values).strip()
    info = state.data.setdefault("git", {})
    exclude = feature_rel(feature, main)

    if cfg["worktree"]:
        branch = branch or f"run-tickets/{feature.name}"
        path = Path(os.path.normpath(fill(cfg["worktree_dir"], values)))
        if (path / ".git").exists():
            work = Git(path)
            if work.branch() != branch:
                raise UsageError(f"the worktree {path} is on branch {work.branch()}, not {branch}")
        else:
            if path.exists() and any(path.iterdir()):
                raise UsageError(f"{path} exists and is not a git worktree; set worktree_dir")
            start = main.branch()
            if main.has_branch(branch):
                r = main.run("worktree", "add", str(path), branch)
            else:
                r = main.run("worktree", "add", "-b", branch, str(path), "HEAD")
                info.setdefault("base_branch", "" if start == "(detached)" else start)
            if r.returncode != 0:
                raise UsageError(f"git worktree add {path} failed: {r.stderr.strip()}")
            say(f"==> working in the worktree {path} on branch {branch}")
            work = Git(path)
            info["worktree_setup_done"] = False
        info.update(branch=branch, worktree=str(work.repo))
        state.save()
        if info.get("worktree_setup_done") is False:  # created, setup not yet passed
            setup_worktree(work, main, cfg)
            info["worktree_setup_done"] = True
            state.save()
        return work

    if branch and main.branch() != branch:
        if main.tracked_changes(exclude):
            raise UsageError(f"cannot switch to branch {branch}: the working tree has "
                             f"uncommitted changes")
        start = main.branch()
        if main.has_branch(branch):
            r = main.run("checkout", "-q", branch)
        else:
            r = main.run("checkout", "-q", "-b", branch)
            info.setdefault("base_branch", "" if start == "(detached)" else start)
        if r.returncode != 0:
            raise UsageError(f"git checkout {branch} failed: {r.stderr.strip()}")
        say(f"==> switched to branch {branch}")
    info.update(branch=main.branch(), worktree="")
    state.save()
    return main


def setup_worktree(work: Git, main: Git, cfg: Dict[str, Any]) -> None:
    """worktree_setup commands, once per new worktree: a worktree has only tracked files,
    so .env files, dependencies and virtualenvs have to be brought in."""
    env = dict(os.environ, RUN_TICKETS_MAIN_REPO=str(main.repo), RUN_TICKETS_WORKTREE=str(work.repo))
    for cmd in cfg.get("worktree_setup") or []:
        say(f"==> worktree setup: {cmd}")
        r = subprocess.run(cmd, shell=True, cwd=str(work.repo), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise UsageError(f"worktree_setup `{cmd}` exited with {r.returncode} in {work.repo}:\n"
                             f"{(r.stdout + r.stderr).strip()[-1500:]}\n"
                             f"Fix it and run again: the setup is repeated until it succeeds.")


def feature_rel(feature: Feature, git: Git) -> Optional[str]:
    """The feature dir relative to a repo root (posix), if it is inside that repo."""
    try:
        rel = os.path.relpath(str(feature.dir), str(git.repo))
    except ValueError:  # another drive on Windows
        return None
    if rel == "." or rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


# ---------------------------------------------------------------------------
# The runner


class Runner:
    def __init__(self, feature: Feature, git: Git, cfg: Dict[str, Any], opts: argparse.Namespace,
                 state: Optional[State] = None, main_git: Optional[Git] = None):
        self.feature = feature
        self.git = git  # the working tree the agent works in
        self.main_git = main_git or git  # the checkout that holds the feature dir
        self.cfg = cfg
        self.opts = opts
        self.state = state or State(feature.runs / "state.json")
        self.report = feature.runs / "REPORT.md"
        self.summary_path = feature.runs / "SUMMARY.md"
        self.events = feature.runs / "events.jsonl"
        # Paths of the feature dir inside the working tree, excluded from "dirty" checks:
        # the agent edits tickets there when they are tracked.
        self.exclude = feature_rel(feature, git) or feature_rel(feature, self.main_git)
        self.proc: Optional[subprocess.Popen] = None
        self.run_id = uuid.uuid4().hex[:8]
        self.run_cost = 0.0
        self.done_this_run: List[Tuple[str, float, int]] = []
        self.failure_detail = ""  # test output of the last failed check, for the retry
        self.last_tests: Optional[Tuple[Path, bool]] = None
        self.last_output = time.monotonic()  # of the running agent, for the status line

    # -- bookkeeping -------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        rec = {"time": iso(), "run": self.run_id, "event": kind, **fields}
        append_durable(self.events, json.dumps(rec, ensure_ascii=False) + "\n")

    def record(self, unit: Any, step: str, result: str, run: Optional[StepRun],
               base: str = "") -> None:
        lines = [f"## {now():%Y-%m-%d %H:%M} {unit.name} - {step}: {result}", ""]
        if run is not None:
            if run.log:
                lines.append(f"- log: {run.log}")
            if run.transcript:
                lines.append(f"- transcript: {run.transcript}")
            lines.append(f"- duration: {fmt_duration(run.seconds)}, exit code: {run.code}"
                         + (" (timed out)" if run.timed_out else "")
                         + (f", cost: {fmt_cost(run.cost)}" if run.cost else ""))
        if self.last_tests is not None:
            log, ok = self.last_tests
            lines.append(f"- tests: {'passed' if ok else 'FAILED'} ({log})")
            self.last_tests = None
        if base:
            try:
                commits = self.git.commits(base)
                lines.append(f"- commits since {base[:10]}: {len(commits)}")
                lines += [f"  - {c}" for c in commits[:30]]
                suspicious = self.suspicious(base)
                if suspicious:
                    lines.append("- **check these test changes** (skips, ignores, changed constants):")
                    lines += [f"  - `{f}`: `{l[:160]}`" for f, l in suspicious[:20]]
            except RuntimeError:
                pass
        if run is not None and run.final:
            report = self.extract_report(run.final)
            if report:
                lines += ["", *("> " + l if l else ">" for l in report.splitlines())]
        lines.append("")
        append_durable(self.report, "\n".join(lines) + "\n")

    def extract_report(self, text: str) -> str:
        heading = re.escape(self.cfg["report_heading"])
        m = list(re.finditer(rf"^#+\s*{heading}\s*$", text, re.MULTILINE | re.IGNORECASE))
        if not m:
            return text.strip()[-3000:]
        return text[m[-1].end():].strip()

    def suspicious(self, base: str) -> List[Tuple[str, str]]:
        """Added skips/ignores, and numbers changed in otherwise unchanged test lines
        (a raised threshold or budget)."""
        pats = [re.compile(p) for p in self.cfg.get("suspicious_patterns") or []]
        out = []
        for path, removed, added in self.git.hunks(base):
            out += [(path, l.strip()) for l in added if any(p.search(l) for p in pats)]
            if TEST_FILE_RE.search(path):
                for old, new in zip(removed, added):
                    if old != new and NUMBER_RE.sub("N", old) == NUMBER_RE.sub("N", new):
                        out.append((path, f"{old.strip()}  ->  {new.strip()}"))
        return out

    def notify(self, message: str) -> None:
        if not UI.live:  # the live view shows every outcome in a box of its own
            say(message)
        title = "run-tickets"
        try:
            if not self.cfg.get("desktop_notify", True):
                pass
            elif sys.platform == "darwin" and shutil.which("osascript"):
                safe = message.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                               capture_output=True, timeout=10)
            elif sys.platform.startswith("linux") and shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
            elif IS_WINDOWS:
                safe = message.replace("'", "''")
                ps = ("[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                      "$n=New-Object System.Windows.Forms.NotifyIcon;"
                      "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                      f"$n.ShowBalloonTip(10000,'{title}','{safe}',"
                      "[System.Windows.Forms.ToolTipIcon]::None);Start-Sleep 5;$n.Dispose()")
                subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
        cmd = self.cfg.get("notify_command")
        if cmd:
            env = dict(os.environ, RUN_TICKETS_MESSAGE=message,
                       RUN_TICKETS_REPORT=str(self.report),
                       RUN_TICKETS_SUMMARY=str(self.summary_path))
            try:
                subprocess.run(cmd, shell=True, env=env, timeout=60, capture_output=True)
            except (OSError, subprocess.SubprocessError) as e:
                warn(f"notify_command failed: {e}")

    def account(self, unit: Any, run: StepRun) -> None:
        entry = self.state.ticket(unit.name)
        entry["runs"] = entry.get("runs", 0) + 1
        entry["seconds"] = round(entry.get("seconds", 0) + run.seconds)
        if run.cost is not None:
            entry["cost"] = round(entry.get("cost", 0) + run.cost, 4)
            self.run_cost += run.cost
        self.state.save()

    def write_summary(self, outcome: str, detail: str = "") -> str:
        """SUMMARY.md: the whole feature at a glance. Returns a one-line version."""
        tickets = self.feature.tickets()
        done = sum(1 for t in tickets if t.status() == DONE)
        entries = self.state.data["tickets"]
        total_cost = sum(e.get("cost", 0) for e in entries.values())
        git = self.state.data.get("git", {})
        lines = [f"# {self.feature.name}: run-tickets summary", "",
                 f"- outcome: **{outcome}**" + (f" - {detail}" if detail else ""),
                 f"- updated: {iso()}",
                 f"- tickets done: {done}/{len(tickets)}",
                 f"- cost: {fmt_cost(self.run_cost)} this run, {fmt_cost(total_cost)} in total"]
        if git.get("branch"):
            lines.append(f"- branch: {git['branch']}" + (f" (worktree {git['worktree']})" if git.get("worktree") else ""))
        lines += ["", "| Ticket | Status | Runner | Agent runs | Time | Cost |",
                  "|---|---|---|---|---|---|"]
        for t in tickets:
            e = entries.get(t.name, {})
            runner = e.get("phase", "")
            if e.get("last_stop") and runner != "done":
                runner += f" (stopped: {e['last_stop'].get('reason', '').splitlines()[0][:80]})"
            lines.append(f"| {t.name} | {t.status() or '-'} | {runner or '-'} | {e.get('runs', 0)} | "
                         f"{fmt_duration(e.get('seconds', 0))} | {fmt_cost(e.get('cost'))} |")
        fe = entries.get(FINAL)
        if fe:
            lines.append(f"| final review | | {fe.get('phase', '')} | {fe.get('runs', 0)} | "
                         f"{fmt_duration(fe.get('seconds', 0))} | {fmt_cost(fe.get('cost'))} |")
        lines += ["", f"Details: {self.report}", ""]
        atomic_write(self.summary_path, "\n".join(lines))
        short = f"{self.feature.name}: {done}/{len(tickets)} done"
        if outcome != "done" or detail:
            short += f"; {outcome}" + (f" - {detail}" if detail else "")
        if self.run_cost:
            short += f"; {fmt_cost(self.run_cost)}"
        return short

    # -- the agent ---------------------------------------------------------

    def skill_command(self, step: str, skill: str, args: str) -> str:
        return fill(self.cfg["step"][step]["spec"]["skill_format"],
                    {"skill": self.cfg["skills"][skill], "args": args}).strip()

    def build_prompt(self, step: str, unit: Any, base: str, resume_note: str) -> str:
        spec = self.feature.spec()
        test_rule = ""
        if self.cfg.get("test_command"):
            test_rule = (f"\nAfter you finish, the runner itself runs `{self.cfg['test_command']}` "
                         f"in the working tree; the step fails if it does not pass.")
        values = {
            "ticket": str(unit.path),
            "tickets": ", ".join(t.name for t in self.feature.tickets()),
            "feature_dir": str(self.feature.dir),
            "spec": str(spec) if spec else "the feature's spec",
            "base": base,
            "repo": str(self.git.repo),
            "report_heading": self.cfg["report_heading"],
            "report_language": self.cfg["report_language"],
            "extra_instructions": self.cfg.get("extra_instructions", ""),
            "review_extra_instructions": self.cfg.get("review_extra_instructions", ""),
            "final_review_extra_instructions": self.cfg.get("final_review_extra_instructions", ""),
            "resume_note": resume_note,
            "new_tickets_rule": (f"If you create new tickets in {self.feature.issues}, give each one the "
                                 f"status line `**Status:** {NEW_TICKET_STATUS}`: the runner does not "
                                 f"pick such tickets up, a human triages them first."),
            "kind_note": ("This is a report ticket (Kind: report): its result is the report and "
                          "the files the ticket names, not code; commit only code you change."
                          if unit.kind() == "report" else ""),
            "verify_reason": (" (it is a report ticket)" if unit.kind() == "report"
                              and self.git.head() != base else ""),
            "implement_command": self.skill_command(step, "implement", str(unit.path)),
            "review_command": self.skill_command(step, "review", f"since {base} for the ticket {unit.path}"),
            "final_command": self.skill_command(
                step, "review", f"since {base} for the whole feature {spec or self.feature.dir}"),
        }
        values["git_rules"] = fill(GIT_RULES, {"test_rule": test_rule})
        prompts = self.cfg.get("prompts") or {}
        values["common"] = fill(prompts.get("common", COMMON_PROMPT), values).strip()
        default = {"implement": IMPLEMENT_PROMPT, "review": REVIEW_PROMPT, "verify": VERIFY_PROMPT,
                   "final": FINAL_PROMPT}[step]
        text = fill(prompts.get(step, default), values)
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    def agent_argv(self, step: str, prompt: str, session_dir: Path, final_file: Path) -> List[str]:
        argv = fill_command(self.cfg, step, prompt, str(self.git.repo), str(session_dir), str(final_file))
        return resolve_executable(argv[0]) + argv[1:]

    def run_agent(self, unit: Any, step: str, prompt: str, attempt: int) -> StepRun:
        sc = self.cfg["step"][step]
        agent = sc["spec"]
        runs = self.feature.runs
        stamp = f"{unit.name}-{step}-{now():%Y%m%d-%H%M%S}"
        if (runs / f"{stamp}.log").exists():  # two attempts within a second
            stamp += f"-{uuid.uuid4().hex[:4]}"
        session_dir = runs / "sessions" / stamp
        session_dir.mkdir(parents=True, exist_ok=True)
        run = StepRun()
        run.log = runs / f"{stamp}.log"
        final_file = session_dir / "final.md"
        atomic_write(runs / "prompts" / f"{stamp}.md", prompt)

        argv = self.agent_argv(step, prompt, session_dir, final_file)
        stream = agent["output"] == "claude-stream-json"
        who = sc["agent"] + (f" ({sc['model']})" if sc["model"] else "")
        attempts = f"attempt {attempt}/{self.cfg['retries'] + 1}"
        say(f"==> {unit.name}: {step} with {who} (log: {run.log})",
            live=f"{UI.chip(step)} {UI.style(f'{who} · {attempts}', 'gray')}\n"
                 f"  {UI.style('log ' + UI.path(run.log), 'gray')}")
        self.event("agent_start", ticket=unit.name, step=step, log=str(run.log),
                   argv=[a if a != prompt else "<prompt>" for a in argv])
        self.state.set(current={"ticket": unit.name, "step": step, "attempt": attempt,
                                "started": iso(), "log": str(run.log), "stream": stream,
                                "agent": sc["agent"]})

        started = time.monotonic()
        try:
            with open(run.log, "w", encoding="utf-8", newline="\n") as log:
                self.proc = popen_group(
                    argv, cwd=str(self.git.repo),
                    stdin=subprocess.PIPE if agent["prompt_via"] == "stdin" else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=dict(os.environ, RUN_TICKETS_TICKET=str(unit.path), RUN_TICKETS_STEP=step))
                if agent["prompt_via"] == "stdin":
                    assert self.proc.stdin is not None
                    try:
                        self.proc.stdin.write(prompt.encode("utf-8"))
                        self.proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass

                proc = self.proc
                md = Markdown(UI)
                self.last_output = time.monotonic()

                def pump() -> None:
                    assert proc.stdout is not None
                    try:
                        for raw in iter(proc.stdout.readline, b""):
                            line = raw.decode("utf-8", errors="replace")
                            log.write(line)
                            log.flush()
                            self.last_output = time.monotonic()
                            try:
                                if UI.live:
                                    self.show_output(md, line, stream, step, agent["markdown"])
                                    continue
                                shown = describe_stream_event(line) if stream else line.rstrip("\n")
                                if shown is not None:
                                    say(shown)
                            except Exception:  # noqa: BLE001 - showing a line must never stop
                                # the reading: an undrained pipe would block the agent.
                                pass
                    except (OSError, ValueError):
                        pass
                    if UI.live:
                        try:
                            for rendered in md.flush():
                                UI.write(self.gutter(step) + rendered)
                        except Exception:  # noqa: BLE001
                            pass

                reader = threading.Thread(target=pump, daemon=True)
                reader.start()
                started_step = time.monotonic()
                # Read once: the ticket files change between steps, not during the redraws.
                board = self.board(unit.name) if UI.live else ""
                label = (f"{unit.number:02d} {unit.title()}" if unit.name != FINAL
                         else "final review of the whole feature")

                def status(frame: int) -> List[str]:
                    return self.step_status(frame, label, step, attempt, started_step, board)

                with UI.showing(status):
                    try:
                        run.code = proc.wait(timeout=sc["max_seconds"])
                    except subprocess.TimeoutExpired:
                        run.timed_out = True
                        say(f"==> {unit.name}: {step} hit max_time {sc['max_time']}, killing it",
                            live=UI.style(f"⏱ {step} hit max_time {sc['max_time']}; "
                                          f"killing the agent", "red"))
                        kill_tree(proc)
                        run.code = proc.wait()
                    except BaseException:
                        kill_tree(proc)
                        proc.wait()
                        reader.join(timeout=10)
                        raise
                    reader.join(timeout=30)
        finally:
            self.proc = None
            self.state.set(current=None)
        run.seconds = time.monotonic() - started

        if final_file.exists() and final_file.stat().st_size:
            run.final = final_file.read_text(encoding="utf-8", errors="replace")
        if stream:
            text, is_error, run.cost = stream_result(run.log)
            run.final = run.final or text
            if is_error and run.code == 0:
                run.code = 1
        if not run.final:
            run.final = run.log.read_text(encoding="utf-8", errors="replace")
        run.transcript = self.export_transcript(agent, session_dir, stamp) or (run.log if stream else None)
        self.event("agent_end", ticket=unit.name, step=step, code=run.code,
                   timed_out=run.timed_out, seconds=round(run.seconds), cost=run.cost)
        return run

    # -- the live view ------------------------------------------------------

    @staticmethod
    def gutter(step: str) -> str:
        return "  " + UI.style("│", STEP_COLORS.get(step, "gray")) + " "

    def show_output(self, md: Markdown, line: str, stream: bool, step: str, markdown: bool) -> None:
        """One line of the agent's output in the live view: markdown rendered (unless the
        agent prints raw tool output: markdown = false), tool calls on a line of their own."""
        line = line.rstrip("\r\n").split("\r")[-1]  # progress bars redraw with \r
        items = stream_items(line) if stream else [("text", line, "")]
        for kind, a, b in items:
            if kind != "text":
                for rendered in md.flush():
                    UI.write(self.gutter(step) + rendered)
            if kind == "tool":
                UI.write(self.gutter(step) + UI.style("▸ ", "cyan") + UI.style(a, "bold")
                         + (" " + UI.style(b, "gray") if b else ""))
            elif kind == "result":
                UI.write(self.gutter(step) + UI.style(f"▸ result: {a}", "gray"))
            else:
                for sub in a.split("\n"):
                    if "\x1b" in sub and UI.color:  # the agent colors its output: keep the colors only
                        kept = ANSI_RE.sub(lambda m: m.group(0) if SGR_RE.fullmatch(m.group(0)) else "", sub)
                        UI.write(self.gutter(step) + kept + "\x1b[0m")
                        continue
                    sub = ANSI_RE.sub("", sub)
                    if not markdown:
                        UI.write(self.gutter(step) + sub)
                        continue
                    for rendered in md.feed(sub):
                        UI.write(self.gutter(step) + rendered)

    def show_step_result(self, unit: Any, step: str, run: StepRun) -> None:
        bits = [fmt_duration(run.seconds)]
        try:
            n = len(self.git.commits(self.state.ticket(unit.name).get("step_head") or "HEAD"))
            bits.append(f"{n} commit{'' if n == 1 else 's'}" if n else "no commits")
        except RuntimeError:
            pass
        if run.cost:
            bits.append(fmt_cost(run.cost))
        UI.write(f"  {UI.style('✓', 'green')} {UI.style(f'{step} passed', 'green', 'bold')} "
                 + UI.style(" · ".join(bits), "gray"))

    def board(self, current: str = "") -> str:
        """The progress bar and an icon per ticket, for the status line."""
        with self.feature.snapshot():
            tickets = self.feature.tickets()
            done = sum(t.status() == DONE for t in tickets)
            icons = [UI.ticket_icon(t.status(), t.name == current)
                     + (UI.style(f"{t.number:02d}", "gray") if len(tickets) <= 12 else "")
                     for t in tickets]
        return f"{UI.bar(done, len(tickets))} {done}/{len(tickets)} done  " + " ".join(icons)

    def step_status(self, frame: int, label: str, step: str, attempt: int, started: float,
                    board: str) -> List[str]:
        color = STEP_COLORS.get(step, "gray")
        bits = [UI.style(label, "bold"),
                UI.style(step, color),
                f"attempt {attempt}/{self.cfg['retries'] + 1}",
                fmt_duration(time.monotonic() - started)]
        quiet = time.monotonic() - self.last_output
        if quiet >= 60:
            bits.append(UI.style(f"no output for {fmt_duration(quiet)}", "yellow"))
        if self.run_cost:
            bits.append(fmt_cost(self.run_cost))
        spin = UI.style(Console.SPINNER[frame % len(Console.SPINNER)], color, "bold")
        return [f"{spin} " + UI.style(" · ", "gray").join(bits),
                f"  {board}" + UI.style("   ctrl+c stops; run it again to resume", "gray")]

    def waiting_status(self, what: str, until: float) -> Callable[[int], List[str]]:
        def status(frame: int) -> List[str]:
            left = max(0, until - time.monotonic())
            spin = UI.style(Console.SPINNER[frame % len(Console.SPINNER)], "yellow")
            return [f"{spin} {what} · {fmt_duration(left)} left",
                    UI.style("  ctrl+c stops; run it again to resume", "gray")]
        return status

    def export_transcript(self, agent: Dict[str, Any], session_dir: Path, stamp: str) -> Optional[Path]:
        cmd = agent.get("export_command") or []
        sessions = sorted(session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not cmd or not sessions:
            return None
        html = self.feature.runs / f"{stamp}.html"
        values = {"session_file": str(sessions[-1]), "html_file": str(html)}
        try:
            argv = [fill(a, values) for a in cmd]
            argv = resolve_executable(argv[0]) + argv[1:]
            subprocess.run(argv, cwd=str(session_dir), capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError, UsageError):
            return None
        if not html.exists():
            produced = sorted(session_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
            if not produced:
                return None
            shutil.move(str(produced[-1]), str(html))
        return html

    def run_shell(self, cmd: str, log: Path, timeout: int) -> Tuple[int, bool]:
        """A shell command in the working tree, output to `log`; (exit code, timed out)."""
        with open(log, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"$ {cmd}\n")
            f.flush()
            self.proc = popen_group(cmd, shell=True, cwd=str(self.git.repo), stdin=subprocess.DEVNULL,
                                    stdout=f, stderr=subprocess.STDOUT)
            proc = self.proc
            try:
                return proc.wait(timeout=timeout), False
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.wait()
                return -1, True
            except BaseException:
                kill_tree(proc)
                proc.wait()
                raise
            finally:
                self.proc = None

    def run_tests(self, label: str) -> Optional[str]:
        """Runs test_command; None when it passes (or none is set), else the reason."""
        cmd = self.cfg.get("test_command")
        if not cmd:
            return None
        log = self.feature.runs / f"{label}-tests-{now():%Y%m%d-%H%M%S}.log"
        if log.exists():
            log = log.with_name(log.stem + f"-{uuid.uuid4().hex[:4]}.log")
        say(f"==> {label}: running the tests ({cmd})",
            live=f"  {UI.style('⧗', 'cyan')} running the tests {UI.style(cmd, 'gray')}")
        started = time.monotonic()

        def status(frame: int) -> List[str]:
            spin = UI.style(Console.SPINNER[frame % len(Console.SPINNER)], "cyan")
            return [f"{spin} {label}: running the tests · {fmt_duration(time.monotonic() - started)}"]

        with UI.showing(status):
            code, timed_out = self.run_shell(cmd, log, self.cfg["test_timeout_seconds"])
        took = fmt_duration(time.monotonic() - started)
        self.last_tests = (log, code == 0 and not timed_out)
        self.event("tests", label=label, code=code, timed_out=timed_out, log=str(log))
        if UI.live:
            UI.write(f"  {UI.style('✓', 'green')} tests passed {UI.style(took, 'gray')}"
                     if code == 0 and not timed_out else
                     f"  {UI.style('✗', 'red')} tests failed {UI.style(f'{took} · log {UI.path(log)}', 'gray')}")
        if code == 0 and not timed_out:
            return None
        self.failure_detail = tail_text(log, 3000)
        how = f"timed out after {self.cfg['test_timeout']}" if timed_out else f"exited with {code}"
        return f"the tests failed: `{cmd}` {how} (log: {log})"

    # -- checks ------------------------------------------------------------

    def common_checks(self, unit: Any, entry: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
        """(reason, retryable) of the first failed check shared by all steps, or None."""
        if not unit.path.exists():
            return "the ticket file is gone", False
        if self.git.stash() != entry.get("stash", ""):
            return ("the git stash list changed - check 'git stash list' and "
                    "'git reflog show refs/stash'; the stash holds the user's own work"), False
        if self.git.branch() != entry.get("branch"):
            return f"the branch changed from {entry.get('branch')} to {self.git.branch()}", False
        if not self.git.is_ancestor(entry["base"]):
            return f"history was rewritten: the base {entry['base'][:10]} is no longer an ancestor of HEAD", False
        protected = self.cfg.get("protected") or []
        if protected:
            since = entry.get("step_head") or entry["base"]
            if not self.git.is_ancestor(since):
                since = entry["base"]
            hits = [f for f in self.git.changed_files(since)
                    if any(path_matches(f, p) for p in protected)]
            if hits:
                return f"protected paths were changed: {', '.join(hits[:10])}", False
        return None

    @staticmethod
    def agent_failed(run: Optional[StepRun], max_time: str) -> Optional[Tuple[str, bool]]:
        if run is not None and run.timed_out:
            return f"the agent ran out of max_time ({max_time})", True
        if run is not None and run.code != 0:
            return f"the agent exited with {run.code}", True
        return None

    def check_step(self, step: str, unit: Any, entry: Dict[str, Any],
                   run: Optional[StepRun]) -> Optional[Tuple[str, bool]]:
        self.failure_detail = ""
        failed = (self.common_checks(unit, entry)
                  or self.agent_failed(run, self.cfg["step"][step]["max_time"]))
        if failed:
            return failed
        if step == "implement":
            status = unit.status()
            if status != RUNNABLE:
                return f"the ticket status is now '{status or 'missing'}'", False
            # No commit is not a failure: the verify step checks a result outside git.
        elif step in ("review", "verify"):
            status = unit.status()
            if status != DONE:
                # ready-for-agent: the agent did not finish; anything else: it asked for a human.
                return f"the ticket status is '{status or 'missing'}', not done", status == RUNNABLE
            left = unit.unchecked()
            if left:
                return (f"{left} acceptance criteri{'on is' if left == 1 else 'a are'} "
                        f"not ticked"), True
        if self.git.tracked_changes(self.exclude):
            return "uncommitted changes were left", True
        tests = self.run_tests(f"{unit.name}-{step}")
        if tests:
            return tests, True
        return None

    def limit_wait(self, run: StepRun) -> Optional[int]:
        """Seconds to wait when the agent stopped on a provider limit, else None."""
        if not self.cfg.get("limit_wait") or run.timed_out or run.code == 0:
            return None
        # Only the end: a full log can mention "rate limit" in the code the agent read.
        text = run.final[-3000:] + "\n" + tail_text(run.log, 2000)
        pats = self.cfg.get("limit_patterns") or []
        if not any(re.search(p, text, re.IGNORECASE) for p in pats):
            return None
        return parse_limit_wait(text, self.cfg["limit_default_wait_seconds"])

    # -- the loop ----------------------------------------------------------

    def preflight(self, when: str) -> Optional[str]:
        for cmd in self.cfg.get("preflight") or []:
            r = subprocess.run(cmd, shell=True, cwd=str(self.git.repo), capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                tail = (r.stdout + r.stderr).strip()[-1500:]
                return f"preflight ({when}) failed: `{cmd}` exited with {r.returncode}\n{tail}"
        return None

    def stop(self, unit: Any, step: str, reason: str, run: Optional[StepRun] = None,
             base: str = "") -> int:
        if unit is not None:
            self.state.update(unit.name, running=False,
                              last_stop={"step": step, "reason": reason, "time": iso()})
            self.record(unit, step, f"STOPPED - {reason}", run, base)
        else:
            append_durable(self.report, f"## {now():%Y-%m-%d %H:%M} STOPPED - {reason}\n\n")
        self.event("stopped", ticket=unit.name if unit else None, step=step, reason=reason)
        where = f"{unit.name} ({step})" if unit else step
        self.notify(self.write_summary("stopped", f"at {where}: {reason.splitlines()[0]}"))
        if UI.live:
            lines = [UI.style(f"✗ stopped at {where}", "red", "bold"), *reason.splitlines(), ""]
            if run is not None:
                lines.append(UI.style("log         ", "gray") + UI.path(run.log))
                lines.append(UI.style("transcript  ", "gray")
                             + (UI.path(run.transcript) if run.transcript else "none"))
            lines.append(UI.style("report      ", "gray") + UI.path(self.report))
            if unit is not None:
                ref = "final" if unit.name == FINAL else f"{unit.number:02d}"
                lines += ["", UI.style("fix the cause, then rerun the step:", "gray"),
                          f"  run-tickets retry {UI.path_arg(self.feature.dir)} {ref}"]
            UI.write("\n" + "\n".join(UI.box(lines, "stopped", "red")))
            return EXIT_STOPPED
        if run is not None:
            say(f"log: {run.log}")
            say(f"transcript: {run.transcript or 'none'}")
        say(f"report: {self.report}")
        return EXIT_STOPPED

    def run(self) -> int:
        git_info = self.state.data.get("git", {})
        if UI.live:
            with self.feature.snapshot():
                UI.write("\n".join(live_header(self.feature, self.state.data, self.cfg)
                                   + live_ticket_rows(self.feature, self.state.data)))
        else:
            say(f"run-tickets {__version__}: {self.feature.dir} with agent '{self.cfg['agent']}'"
                + (f" ({self.cfg['config_path']})" if self.cfg["config_path"] else "")
                + (f", branch {git_info['branch']}" if git_info.get("branch") else ""))
        self.event("run_start", agent=self.cfg["agent"], pid=os.getpid())
        self.rotate_logs()
        if self.state.data.get("waiting"):
            self.state.set(waiting=None)
        for name, entry in list(self.state.data["tickets"].items()):
            if entry.get("tickets_before") is not None:  # a step cut off before its triage
                unit = FinalUnit(self.feature) if name == FINAL else Ticket(self.feature.issues / f"{name}.md")
                self.triage_new_tickets(unit, entry)
        recovered = self.recover_finished_step()
        if recovered is not None:
            return recovered

        dirty = self.git.tracked_changes(self.exclude)
        if dirty:
            pending = [n for n, e in self.state.data["tickets"].items()
                       if e.get("phase") in TICKET_PHASES + ("final",)
                       and (e.get("running") or e.get("last_stop"))]
            if not (pending and self.opts.adopt_dirty):
                warn(f"the working tree {self.git.repo} has uncommitted changes; commit or stash them first:")
                warn(dirty)
                if pending:
                    warn(f"\n{pending[0]} was interrupted or stopped mid-step, so these may be the "
                         f"agent's unfinished edits. To let the agent continue with them, rerun "
                         f"with --adopt-dirty.")
                return EXIT_USAGE
        pre = self.preflight("start")
        if pre:
            return self.stop(None, "preflight", pre)

        while True:
            ticket = self.feature.next_ticket()
            if ticket is None:
                return self.finish()
            code = self.run_ticket(ticket)
            if code != EXIT_OK:
                return code
            self.write_summary("running")
            if self.opts.once:
                self.print_done()
                return EXIT_OK

    def recover_finished_step(self) -> Optional[int]:
        """A step that was running when the runner died but whose ticket is already done
        (the agent finished, the runner did not live to check it): check it now, so it is
        neither skipped unchecked nor rerun."""
        for name, entry in list(self.state.data["tickets"].items()):
            if not entry.get("running") or name == FINAL:
                continue
            path = self.feature.issues / f"{name}.md"
            if not path.exists():
                continue
            ticket = Ticket(path)
            if ticket.status() == RUNNABLE:
                continue  # resumed by the loop
            step = entry.get("phase") if entry.get("phase") in TICKET_PHASES else "implement"
            say(f"==> {name}: the runner stopped during its {step} step; checking it now")
            failed = self.check_step(step, ticket, entry, None)
            if failed:
                return self.stop(ticket, step, f"{failed[0]} (checked after an interrupted run)",
                                 None, entry.get("base", ""))
            if step != "implement":
                self.state.update(name, phase="done", running=False, last_stop=None, finished=iso())
                self.record(ticket, step, "ok - ticket done (checked after an interrupted run)",
                            None, entry["base"])
            else:
                nxt = "verify" if ticket.kind() == "report" or self.git.head() == entry["base"] else "review"
                self.state.update(name, phase=nxt, running=False)
        return None

    def run_ticket(self, ticket: Ticket) -> int:
        if UI.live:
            tickets = self.feature.tickets()
            done = sum(t.status() == DONE for t in tickets)
            UI.write("\n" + UI.rule(f"{ticket.number:02d} {ticket.title()}", f"{done}/{len(tickets)} done"))
        entry = self.state.ticket(ticket.name)
        resuming = (entry.get("phase") in TICKET_PHASES and entry.get("base")
                    and self.git.is_ancestor(entry["base"]))
        if not resuming:
            if entry.get("phase") in TICKET_PHASES:
                say(f"==> {ticket.name}: earlier base {str(entry.get('base'))[:10]} is not in "
                    f"HEAD's history any more; starting the ticket over")
            entry.clear()
            head = self.git.head()
            self.state.update(ticket.name, phase="implement", base=head,
                              branch=self.git.branch(), running=False, started=iso())
            entry = self.state.ticket(ticket.name)
            fb = self.state.data.get("feature_base")
            if not fb or not self.git.is_ancestor(fb):
                self.state.set(feature_base=head)
        elif entry.get("branch") and entry["branch"] != self.git.branch():
            return self.stop(ticket, entry["phase"],
                             f"the ticket was started on branch {entry.get('branch')}, "
                             f"now on {self.git.branch()}; switch back or run "
                             f"`run-tickets reset {self.feature.dir} {ticket.number:02d}`")
        else:
            if not entry.get("branch"):  # adopted: it runs on whatever branch the run uses
                self.state.update(ticket.name, branch=self.git.branch())
            say(f"==> {ticket.name}: resuming at {entry['phase']} (base {entry['base'][:10]})")

        pre = self.preflight(ticket.name)
        if pre:
            return self.stop(ticket, entry["phase"], pre, None, entry["base"])
        baseline_stop = (entry.get("last_stop") or {}).get("step") == "baseline"
        if (not resuming or baseline_stop) and self.cfg.get("test_baseline") and self.cfg.get("test_command"):
            failed = self.run_tests(f"{ticket.name}-baseline")
            if failed:
                self.last_tests = None
                return self.stop(ticket, "baseline",
                                 f"{failed} before the agent started on this ticket: the base "
                                 f"or the environment is broken", None, entry["base"])
            self.last_tests = None

        started = time.monotonic()
        if entry["phase"] == "implement":
            code = self.run_step(ticket, "implement")
            if code != EXIT_OK:
                return code
        if entry["phase"] == "review" and self.git.head() == entry["base"]:
            # Nothing to review (an adopted or older state): check the result instead.
            self.state.update(ticket.name, phase="verify")
        code = self.run_step(ticket, entry["phase"])
        if code != EXIT_OK:
            return code
        commits = len(self.git.commits(entry["base"]))
        self.state.update(ticket.name, commits=commits)
        self.done_this_run.append((ticket.name, time.monotonic() - started, commits))
        took = fmt_duration(time.monotonic() - started)
        say(f"==> {ticket.name} done: {commits} commit(s)",
            live=f"{UI.style('✓', 'green', 'bold')} {UI.style(f'{ticket.number:02d} {ticket.title()}', 'bold')} "
                 + UI.style(f"done · {commits} commit{'' if commits == 1 else 's'} · {took}", "green"))
        if self.should_pause(ticket) and not entry.get("paused"):
            self.state.update(ticket.name, paused=True)
            self.event("paused", ticket=ticket.name)
            self.notify(self.write_summary("paused", f"after {ticket.name} (pause_after); "
                                                     f"run the same command to continue"))
            self.print_done()
            if UI.live:
                UI.write("\n" + "\n".join(UI.box([UI.style(f"⏸ paused after {ticket.name} (pause_after)", "yellow", "bold"),
                                                  "have a look, then run the same command to continue"],
                                                 "paused", "yellow")))
            return EXIT_PAUSED
        return EXIT_OK

    def should_pause(self, ticket: Ticket) -> bool:
        for ref in self.cfg.get("pause_after") or []:
            ref = ref.strip()
            if ref == ticket.name or (ref.isdigit() and int(ref) == ticket.number):
                return True
        return False

    def run_step(self, unit: Any, step: str) -> int:
        entry = self.state.ticket(unit.name)
        note = self.resume_note(unit, step, entry)
        attempts = self.cfg["retries"] + 1
        attempt, waited = 0, 0
        while True:
            if self.cfg["max_cost"] and self.run_cost >= self.cfg["max_cost"]:
                return self.stop(unit, step, f"the run's budget is spent: {fmt_cost(self.run_cost)} "
                                             f"of max_cost {fmt_cost(self.cfg['max_cost'])}",
                                 None, entry["base"])
            prompt = self.build_prompt(step, unit, entry["base"], note)
            self.state.update(unit.name, phase=step, running=True, stash=self.git.stash(),
                              branch=self.git.branch(), step_head=self.git.head(),
                              attempt=attempt + 1,
                              tickets_before=[t.name for t in self.feature.tickets()])
            run = self.run_agent(unit, step, prompt, attempt + 1)
            self.account(unit, run)
            self.triage_new_tickets(unit, entry)
            failed = self.check_step(step, unit, entry, run)
            if failed is None:
                if UI.live:
                    self.show_step_result(unit, step, run)
                return self.step_passed(unit, step, run, entry)
            reason, retryable = failed
            if UI.live:
                UI.write(f"  {UI.style('✗', 'red')} {UI.style(f'{step} failed:', 'red', 'bold')} "
                         f"{reason.splitlines()[0]} {UI.style(fmt_duration(run.seconds), 'gray')}")

            wait = self.limit_wait(run) if retryable and run.code != 0 else None
            if wait is not None:
                if waited + wait > self.cfg["limit_max_wait_seconds"]:
                    return self.stop(unit, step, f"the provider's limit did not lift within "
                                                 f"limit_max_wait ({self.cfg['limit_max_wait']})",
                                     run, entry["base"])
                until = now() + dt.timedelta(seconds=wait)
                self.state.update(unit.name, running=False)
                self.state.set(waiting={"ticket": unit.name, "step": step, "until": iso(until)})
                self.record(unit, step, f"PROVIDER LIMIT - waiting {fmt_duration(wait)}, "
                                        f"until {until:%Y-%m-%d %H:%M}", run, entry["base"])
                self.event("limit_wait", ticket=unit.name, step=step, seconds=wait)
                say(f"==> {unit.name}: the provider's limit was hit; waiting until {until:%H:%M}",
                    live=f"  {UI.style('⏸', 'yellow')} the provider's limit was hit; "
                         f"waiting until {until:%H:%M}, then the step goes on")
                with UI.showing(self.waiting_status(f"waiting for the provider's limit "
                                                    f"(until {until:%H:%M})", time.monotonic() + wait)):
                    time.sleep(wait)
                self.state.set(waiting=None)
                waited += wait
                note = (f"A previous {step} run of this ticket was cut off by the provider's usage "
                        f"limit. Its partial work may be in the commits since {entry['base'][:10]} "
                        f"and in the working tree: continue from there.")
                continue

            attempt += 1
            if not retryable or attempt >= attempts:
                return self.stop(unit, step, reason, run, entry["base"])
            self.state.update(unit.name, running=False)
            self.record(unit, step, f"FAILED, retrying ({attempt}/{attempts - 1}) - {reason}", run,
                        entry["base"])
            delay = self.cfg["retry_delay_seconds"]
            say(f"==> {unit.name}: {step} failed ({reason}); retrying with a fresh process"
                + (f" in {fmt_duration(delay)}" if delay else ""),
                live=f"  {UI.style('↻', 'yellow')} retrying with a fresh process "
                     f"({attempt}/{attempts - 1})" + (f" in {fmt_duration(delay)}" if delay else ""))
            with UI.showing(self.waiting_status("retrying soon", time.monotonic() + delay)):
                time.sleep(delay)
            note = self.retry_note(step, reason, run)

    def triage_new_tickets(self, unit: Any, entry: Dict[str, Any]) -> None:
        """Tickets the agent created during the step wait for a human: the runner sets them
        to needs-triage whatever status the agent gave them, so they are never picked up."""
        before = entry.get("tickets_before")
        if before is None:
            return
        new = [t for t in self.feature.tickets() if t.name not in set(before)]
        entry.pop("tickets_before", None)
        self.state.save()
        if not new:
            return
        changed = []
        for t in new:
            if t.status() != NEW_TICKET_STATUS:
                changed.append(f"{t.name} ({t.status() or 'no status'})")
                t.set_status(NEW_TICKET_STATUS)
        names = ", ".join(t.name for t in new)
        say(f"==> {unit.name}: new ticket(s) from the agent, left for triage: {names}")
        self.event("new_tickets", ticket=unit.name, new=[t.name for t in new], changed=changed)
        append_durable(self.report, f"## {now():%Y-%m-%d %H:%M} {unit.name} - new tickets: {names}\n\n"
                       f"The agent created them; they are {NEW_TICKET_STATUS} and wait for a human."
                       + (f" Set to {NEW_TICKET_STATUS} by the runner: {', '.join(changed)}." if changed else "")
                       + "\n\n")

    def step_passed(self, unit: Any, step: str, run: StepRun, entry: Dict[str, Any]) -> int:
        if step == "implement":
            nxt = "verify" if unit.kind() == "report" or self.git.head() == entry["base"] else "review"
            self.state.update(unit.name, phase=nxt, running=False, last_stop=None,
                              implemented=self.git.head())
            self.record(unit, step, "ok" if nxt == "review" else
                        "ok - no commits" + (" (report ticket)" if unit.kind() == "report" else "")
                        + ", the verify step checks the result", run, entry["base"])
        elif step in ("review", "verify"):
            self.state.update(unit.name, phase="done", running=False, last_stop=None,
                              finished=iso())
            self.record(unit, step, "ok - ticket done", run, entry["base"])
        else:
            self.state.update(unit.name, phase="done", running=False, last_stop=None,
                              finished=iso(), done_head=self.git.head())
            self.record(unit, step, "ok - feature reviewed", run, entry["base"])
        return EXIT_OK

    def resume_note(self, unit: Any, step: str, entry: Dict[str, Any]) -> str:
        stop = entry.get("last_stop") or {}
        what = "this ticket" if unit.name != FINAL else "the final review"
        if entry.get("running"):
            lead = f"A previous {step} run of {what} was interrupted before it finished."
        elif stop.get("step") == step:
            lead = f"A previous {step} run of {what} was stopped by the runner: {stop.get('reason')}."
        else:
            return ""
        if step in ("implement", "final") and self.git.head() != entry.get("step_head", entry["base"]):
            lead += (f" Commits since {entry['base'][:10]} may hold its partial work: read them "
                     f"and continue from there instead of starting over.")
        if self.git.tracked_changes(self.exclude):
            lead += " The uncommitted changes in the working tree are its unfinished edits."
        return lead

    def retry_note(self, step: str, reason: str, run: StepRun) -> str:
        excerpt = self.extract_report(run.final)[-1500:] if run.final else ""
        note = (f"A previous {step} attempt failed the runner's check: {reason}. "
                f"Find out why and fix it; commits since the base may hold that attempt's work.")
        if self.failure_detail:
            note += f"\nThe end of the test output:\n{self.failure_detail}"
        if excerpt:
            note += f"\nIts final report was:\n{excerpt}"
        return note

    # -- the end of the feature ----------------------------------------------

    def finish(self) -> int:
        left = self.feature.open_tickets()
        if left:
            self.event("nothing_runnable", open=[t.name for t in left])
            self.notify(self.write_summary("blocked", f"no runnable ticket, {len(left)} open"))
            self.print_done()
            print_open(self.feature, left)
            return EXIT_BLOCKED
        if self.cfg["final_review"]:
            code = self.run_final()
            if code != EXIT_OK:
                return code
        branch = self.git.branch()
        base_branch = self.state.data.get("git", {}).get("base_branch", "")
        self.release_worktree()
        self.event("all_done", branch=branch)
        self.notify(self.write_summary("done", f"every ticket is done, the work is on branch {branch}"))
        self.print_done()
        if UI.live:
            tickets = self.feature.tickets()
            lines = [UI.style(f"✓ every ticket is done ({len(tickets)})", "green", "bold"),
                     f"the work is on branch {UI.style(branch, 'bold')} in {UI.path(self.main_git.repo)}", ""]
            if base_branch:
                lines += [f"  git log --oneline {base_branch}..{branch}", f"  git diff {base_branch}...{branch}"]
            lines.append(f"  git push -u origin {branch}  " + UI.style("# when you want it on the server", "gray"))
            UI.write("\n" + "\n".join(UI.box(lines, "done", "green")))
            return EXIT_OK
        say(f"\nThe work is on branch {branch} in {self.main_git.repo}:")
        if base_branch:
            say(f"  git log --oneline {base_branch}..{branch}")
            say(f"  git diff {base_branch}...{branch}")
        say(f"  git push -u origin {branch}     # when you want it on the server")
        return EXIT_OK

    def release_worktree(self) -> None:
        """Every ticket is done: the worktree folder goes, the branch stays in the repo."""
        info = self.state.data.get("git", {})
        path = info.get("worktree")
        if not path:
            return
        leftovers = self.git.porcelain()
        if leftovers:
            msg = (f"keeping the worktree {path}: it has files that are not committed "
                   f"(remove it yourself with `git worktree remove --force {path}`):\n{leftovers}")
            warn(msg)
            append_durable(self.report, f"## {now():%Y-%m-%d %H:%M} WORKTREE KEPT\n\n> "
                                        + msg.replace("\n", "\n> ") + "\n\n")
            return
        r = self.main_git.run("worktree", "remove", path)
        if r.returncode != 0:
            warn(f"could not remove the worktree {path}: {r.stderr.strip()}")
            return
        self.main_git.run("worktree", "prune")
        info["worktree"] = ""
        self.state.save()
        self.event("worktree_removed", path=path)
        say(f"==> removed the worktree {path}; the branch {info.get('branch')} stays in the repository")

    def feature_base(self) -> str:
        base = self.state.data.get("feature_base", "")
        if base and self.git.is_ancestor(base):
            return base
        # An older state: the base of the earliest ticket the runner did.
        bases = [e.get("base") for n, e in sorted(self.state.data["tickets"].items())
                 if n != FINAL and e.get("base") and self.git.is_ancestor(e["base"])]
        for b in bases:
            if all(self.git.is_ancestor(b, o) for o in bases):
                return b
        return ""

    def run_final(self) -> int:
        unit = FinalUnit(self.feature)
        entry = self.state.ticket(FINAL)
        head = self.git.head()
        if entry.get("phase") == "done" and entry.get("done_head") == head:
            return EXIT_OK
        base = self.feature_base()
        if not base:
            say("==> final review skipped: the runner did not do these tickets, so it has no base")
            return EXIT_OK
        if entry.get("phase") != "final" or entry.get("base") != base:
            entry.clear()
            self.state.update(FINAL, phase="final", base=base, branch=self.git.branch(),
                              running=False, started=iso())
        pre = self.preflight("final review")
        if pre:
            return self.stop(unit, "final", pre, None, base)
        if UI.live:
            UI.write("\n" + UI.rule("final review of the whole feature", f"since {base[:10]}", "yellow"))
        return self.run_step(unit, "final")

    def print_done(self) -> None:
        if not self.done_this_run:
            return
        if UI.live:
            width = max(len(n) for n, _, _ in self.done_this_run)
            lines = [UI.style("Done in this run", "bold")]
            for name, seconds, commits in self.done_this_run:
                lines.append(f"  {UI.style('✓', 'green')} {name.ljust(width)}  "
                             + UI.style(f"{fmt_duration(seconds)} · {commits} commit{'' if commits == 1 else 's'}",
                                        "gray"))
            if self.run_cost:
                lines.append(UI.style(f"  cost {fmt_cost(self.run_cost)}", "gray"))
            lines.append(UI.style(f"summary {UI.path(self.summary_path)}", "gray"))
            UI.write("\n" + "\n".join(lines))
            return
        say("\nDone in this run:")
        for name, seconds, commits in self.done_this_run:
            say(f"  {name}: {fmt_duration(seconds)}, {commits} commit(s)")
        say(f"summary: {self.summary_path}")

    # -- housekeeping ------------------------------------------------------

    def rotate_logs(self) -> None:
        """Deletes step logs, transcripts, prompts and sessions older than log_retention,
        then the oldest ones while runs/ is over log_max_mb (never anything from the last
        day, nor REPORT.md, SUMMARY.md, state.json, events.jsonl)."""
        retention = self.cfg["log_retention_seconds"]
        cap = float(self.cfg.get("log_max_mb") or 0) * 1024 * 1024
        if not retention and not cap:
            return
        keep = {"REPORT.md", "SUMMARY.md", "state.json", "events.jsonl", ".lock"}
        items: List[Path] = []
        for p in self.feature.runs.iterdir():
            if p.name in keep or p.name.endswith(".tmp"):
                continue
            if p.is_dir() and p.name in ("sessions", "prompts"):
                items += list(p.iterdir())
            elif p.is_file():
                items.append(p)

        def size(p: Path) -> int:
            try:
                return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
            except OSError:
                return 0

        def remove(p: Path) -> None:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass

        nowts = time.time()
        removed = 0
        alive = []
        for p in items:
            try:
                age = nowts - p.stat().st_mtime
            except OSError:
                continue
            if retention and age > retention:
                remove(p)
                removed += 1
            else:
                alive.append((p.stat().st_mtime, p, size(p)))
        if cap:
            total = sum(s for _, _, s in alive)
            for mtime, p, s in sorted(alive, key=lambda x: x[0]):
                if total <= cap or nowts - mtime < 86400:
                    break
                remove(p)
                removed += 1
                total -= s
        if removed:
            self.event("logs_rotated", removed=removed)
            say(f"==> removed {removed} old log file(s)/dir(s) from {self.feature.runs}")

    def interrupt(self) -> None:
        if self.proc is not None:
            kill_tree(self.proc)
        try:
            self.state.set(current=None, waiting=None)
        except OSError:
            pass


def print_open(feature: Feature, tickets: List[Ticket]) -> None:
    index = feature.by_number()
    if UI.live:
        lines = [UI.style(f"nothing runnable; {len(tickets)} ticket(s) open", "yellow", "bold")]
        for t in tickets:
            why = feature.waiting_on(t, index)
            extra = f", waiting on {', '.join(why)}" if why and t.status() == RUNNABLE else ""
            lines.append(f"  {UI.ticket_icon(t.status())} {t.name} {UI.style((t.status() or 'no status') + extra, 'gray')}")
        UI.write("\n" + "\n".join(UI.box(lines, "blocked", "yellow")))
        return
    say("open:")
    for t in tickets:
        why = feature.waiting_on(t, index)
        extra = f", waiting on {', '.join(why)}" if why and t.status() == RUNNABLE else ""
        say(f"  {t.name} - {t.status() or 'no status'}{extra}")


# ---------------------------------------------------------------------------
# Commands


def load_state_data(feature: Feature) -> Dict[str, Any]:
    state_path = feature.runs / "state.json"
    return State(state_path).data if state_path.exists() else {"tickets": {}}


def running_lines(feature: Feature, data: Dict[str, Any]) -> Tuple[List[str], bool]:
    """What the runner of this feature does now, if one runs; (lines, running)."""
    out: List[str] = []
    running = Lock(feature.runs / ".lock", feature.dir).alive()
    cur = data.get("current")
    waiting = data.get("waiting")
    if running and cur:
        log = Path(cur.get("log", ""))
        started = dt.datetime.fromisoformat(cur["started"])
        line = (f"running now: {cur['ticket']} {cur['step']} (attempt {cur.get('attempt', 1)}, "
                f"{cur.get('agent', '')}) for {fmt_duration((now() - started).total_seconds())}")
        try:
            idle = time.time() - log.stat().st_mtime
            line += f"; last output {fmt_duration(idle)} ago"
        except OSError:
            pass
        out.append(line)
        act = last_activity(log, bool(cur.get("stream")))
        if act:
            out.append(f"  last: {act}")
    elif running and waiting:
        out.append(f"running now: waiting for the provider's limit until {waiting.get('until')} "
                   f"({waiting.get('ticket')} {waiting.get('step')})")
    elif running:
        out.append("running now: between steps (tests, checks or preflight)")
    elif cur:
        out.append(f"not running; the last run was cut off during {cur['ticket']} {cur['step']}")
    return out, running


def status_text(feature: Feature) -> List[str]:
    data = load_state_data(feature)
    index = feature.by_number()
    tickets = feature.tickets()
    out = [f"{feature.dir}"]
    now_lines, running = running_lines(feature, data)
    out += now_lines
    git_info = data.get("git") or {}
    if git_info.get("branch"):
        out.append(f"branch: {git_info['branch']}" + (f", worktree {git_info['worktree']}" if git_info.get("worktree") else ""))
    for t in tickets:
        status = t.status() or "no status"
        mark = {"done": "x", "wontfix": "-"}.get(status, " ")
        line = f"  [{mark}] {t.name} - {status}"
        if t.status() not in CLOSED:
            total = t.checked() + t.unchecked()
            if total:
                line += f", {t.checked()}/{total} criteria"
            why = feature.waiting_on(t, index)
            if why:
                line += f", waiting on {', '.join(why)}"
        entry = data["tickets"].get(t.name) or {}
        if entry.get("phase") in TICKET_PHASES:
            line += f"\n        runner: at {entry['phase']}" + (" (interrupted)" if entry.get("running") and not running else "")
            if entry.get("last_stop"):
                line += f", stopped {entry['last_stop'].get('time')}: {entry['last_stop'].get('reason', '').splitlines()[0]}"
        out.append(line)
        if len(index.get(t.number, [])) > 1 and t is index[t.number][0]:
            out.append(f"      warning: {len(index[t.number])} tickets share number {t.number:02d}")
    fe = data["tickets"].get(FINAL)
    if fe:
        line = f"  final review: {fe.get('phase')}"
        if fe.get("last_stop") and fe.get("phase") != "done":
            line += f", stopped {fe['last_stop'].get('time')}: {fe['last_stop'].get('reason', '').splitlines()[0]}"
        out.append(line)
    nxt = feature.next_ticket()
    out.append(f"next: {nxt.name if nxt else 'none runnable'}")
    done = sum(1 for t in tickets if t.status() == DONE)
    cost = sum(e.get("cost", 0) for e in data["tickets"].values())
    out.append(f"done: {done}/{len(tickets)}" + (f", cost so far {fmt_cost(cost)}" if cost else ""))
    for name in ("SUMMARY.md", "REPORT.md"):
        if (feature.runs / name).exists():
            out.append(f"{name.split('.')[0].lower()}: {feature.runs / name}")
    return out


def live_header(feature: Feature, data: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """The box at the top of a run and of `status`: the feature, the agent, the branch."""
    git_info = data.get("git") or {}
    title = feature.spec_title()
    lines = [UI.style(feature.name, "bold") + (UI.style(f"  {title}", "gray") if title != feature.name else "")]
    bits = []
    if cfg:
        bits.append(f"agent {UI.style(cfg['agent'], 'cyan')}")
    if git_info.get("branch"):
        bits.append(f"branch {UI.style(git_info['branch'], 'cyan')}")
    if git_info.get("worktree"):
        bits.append(f"worktree {UI.path(git_info['worktree'])}")
    if cfg and cfg.get("config_path"):
        bits.append(UI.style(f"config {UI.path(cfg['config_path'])}", "gray"))
    if bits:
        lines.append(UI.style(" · ", "gray").join(bits))
    tickets = feature.tickets()
    done = sum(t.status() == DONE for t in tickets)
    cost = sum(e.get("cost", 0) for e in data["tickets"].values())
    lines.append(f"{UI.bar(done, len(tickets), 24)} {done}/{len(tickets)} done"
                 + (UI.style(f" · cost so far {fmt_cost(cost)}", "gray") if cost else ""))
    return UI.box(lines, f"run-tickets {__version__}", "cyan")


def live_ticket_rows(feature: Feature, data: Dict[str, Any], running: bool = False) -> List[str]:
    """A line per ticket: an icon, the number, the title, what holds it."""
    index = feature.by_number()
    tickets = feature.tickets()
    cur = (data.get("current") or {}).get("ticket") if running else None
    width = min(max([visible_len(t.title()) for t in tickets] + [0]), 44)
    rows = []
    for t in tickets:
        status = t.status() or "no status"
        detail = []
        if t.status() not in CLOSED:
            if status != RUNNABLE:
                detail.append(UI.style(status, "yellow"))
            total = t.checked() + t.unchecked()
            if total:
                detail.append(f"{t.checked()}/{total} criteria")
            # "03-third (ready-for-agent)" -> "03": the row above or below says the rest.
            why = [re.sub(r"^(\d+)-\S+ \(.*\)$", r"\1", w) for w in feature.waiting_on(t, index)]
            if why:
                detail.append(f"waiting on {', '.join(why)}")
        elif status == "wontfix":
            detail.append("wontfix")
        entry = data["tickets"].get(t.name) or {}
        if entry.get("phase") in TICKET_PHASES and t.name != cur:
            interrupted = entry.get("running") and not running
            detail.append(UI.style(f"at {entry['phase']}" + (" (interrupted)" if interrupted else ""), "cyan"))
            stop = entry.get("last_stop")
            if stop:
                detail.append(UI.style(f"stopped: {stop.get('reason', '').splitlines()[0]}", "red"))
        elif t.name == cur:
            detail.append(UI.style(f"{(data.get('current') or {}).get('step')} now", "yellow"))
        title = clip(t.title(), width)
        title += " " * (width - visible_len(title))
        row = (f"  {UI.ticket_icon(t.status(), t.name == cur)} {UI.style(f'{t.number:02d}', 'gray')} "
               f"{title}  " + UI.style(" · ", "gray").join(UI.style(d, "gray") if "\x1b" not in d else d
                                                          for d in detail))
        rows.append(clip(row, UI.width() - 1))
        if len(index.get(t.number, [])) > 1 and t is index[t.number][0]:
            rows.append(UI.style(f"     warning: {len(index[t.number])} tickets share number {t.number:02d}",
                                 "yellow"))
    return rows


def status_live(feature: Feature) -> List[str]:
    data = load_state_data(feature)
    now_lines, running = running_lines(feature, data)
    out = live_header(feature, data)
    for i, line in enumerate(now_lines):
        out.append(UI.style(line, "gray") if i else
                   UI.style("● " if running else "○ ", "yellow" if running else "gray") + line)
    out += live_ticket_rows(feature, data, running)
    fe = data["tickets"].get(FINAL)
    if fe:
        line = (f"  {UI.ticket_icon(DONE if fe.get('phase') == 'done' else RUNNABLE)} "
                f"{UI.style('··', 'gray')} final review  {UI.style(str(fe.get('phase')), 'gray')}")
        if fe.get("last_stop") and fe.get("phase") != "done":
            line += UI.style(f" · stopped: {fe['last_stop'].get('reason', '').splitlines()[0]}", "red")
        out.append(line)
    nxt = feature.next_ticket()
    out.append("")
    out.append(f"next {UI.style(nxt.name, 'bold') if nxt else UI.style('none runnable', 'yellow')}")
    for name in ("SUMMARY.md", "REPORT.md"):
        if (feature.runs / name).exists():
            out.append(UI.style(f"{name.split('.')[0].lower()} {UI.path(feature.runs / name)}", "gray"))
    return out


def configured_ui(feature: Feature) -> str:
    """The `ui` key of the config `run` would read, for `status`."""
    try:
        path = find_config(feature.dir, Git(feature.dir).repo, None)
    except UsageError:
        return "auto"
    return str(load_toml(path).get("ui", "auto")) if path else "auto"


def cmd_status(feature: Feature, watch: Optional[float]) -> int:
    view = status_live if UI.live else status_text

    def render(f: Feature) -> List[str]:
        with f.snapshot():
            return view(f)

    if not watch:
        for line in render(feature):
            say(line)
        return EXIT_OK
    try:
        while True:
            lines = render(feature)
            if IS_WINDOWS:
                os.system("cls")
            else:
                sys.stdout.write("\033[2J\033[H")
            say(f"{now():%H:%M:%S}  (Ctrl+C to quit)")
            for line in lines:
                say(line)
            time.sleep(watch)
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_init(target: Optional[str], agent: Optional[str]) -> int:
    if target:
        where = Path(target)
    else:  # the repo root, where every feature of the repo finds it
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        where = Path(r.stdout.strip()) if r.returncode == 0 else Path.cwd()
    path = where / CONFIG_NAMES[0]
    if path.exists():
        warn(f"{path} already exists")
        return EXIT_USAGE
    text = fill(EXAMPLE_CONFIG, {"init_agent": agent or DEFAULT_CONFIG["agent"]})
    atomic_write(path, text)
    say(f"wrote {path}")
    say("check what the runner will do with it: run-tickets config <feature dir>")
    return EXIT_OK


def cmd_config(feature: Feature, args: argparse.Namespace) -> int:
    """The effective config: where each value comes from, the agents' command lines and,
    with --prompts, the exact prompts of the next ticket."""
    git = Git(feature.dir)
    path = find_config(feature.dir, git.repo, args.config)
    cfg = build_config(path, args)
    say("config lookup: --config, then " + ", then ".join(
        str(d / CONFIG_NAMES[0]) for d in (feature.dir, git.repo)))
    say(f"config file:   {path or 'none found, using defaults'}")
    say("")
    shown = ("agent", "model", "max_time", "retries", "retry_delay", "test_command", "test_timeout",
             "test_baseline", "limit_wait", "limit_default_wait", "limit_max_wait", "branch",
             "worktree", "worktree_dir", "worktree_setup",
             "final_review", "pause_after", "max_cost", "max_step_cost", "protected",
             "log_retention", "log_max_mb", "report_heading", "report_language",
             "desktop_notify", "notify_command", "ui", "preflight", "extra_instructions",
             "review_extra_instructions", "final_review_extra_instructions")
    for key in shown:
        value = cfg.get(key)
        say(f"  {key:<32} = {json.dumps(value, ensure_ascii=False):<40} # {cfg['sources'].get(key, 'default')}")
    say(f"  {'skills.implement':<32} = {json.dumps(cfg['skills']['implement'])}")
    say(f"  {'skills.review':<32} = {json.dumps(cfg['skills']['review'])}")
    ok = True
    for step in STEPS:
        if step == "final" and not cfg["final_review"]:
            continue
        sc = cfg["step"][step]
        agent = sc["spec"]
        argv = fill_command(cfg, step, "<prompt>", str(git.repo),
                            str(feature.runs / "sessions" / "<step>"),
                            str(feature.runs / "sessions" / "<step>" / "final.md"))
        try:
            found = " ".join(resolve_executable(argv[0]))
        except UsageError:
            found, ok = "NOT FOUND on PATH", False
        say("")
        say(f"{step} step: agent {sc['agent']}, max_time {sc['max_time']}"
            f" ({'prompt on stdin' if agent['prompt_via'] == 'stdin' else 'prompt as the last argument'}):")
        say("  " + (subprocess.list2cmdline(argv) if IS_WINDOWS else shlex.join(argv)))
        say(f"  executable: {found}")
        if cfg["max_step_cost"] and not agent["budget_args"]:
            say(f"  note: agent {sc['agent']} takes no budget flag; max_step_cost does not apply")
        if cfg["max_cost"] and agent["output"] != "claude-stream-json":
            say(f"  note: agent {sc['agent']} reports no cost; its steps do not count toward max_cost")
    if getattr(args, "prompts", False):
        ticket = feature.next_ticket() or (feature.tickets() or [None])[0]
        if ticket is None:
            say("\nno tickets to build prompts for")
        else:
            runner = Runner(feature, git, cfg, args)
            base = git.head()
            for step in ("implement", "review", "verify"):
                say(f"\n----- {step} prompt for {ticket.name} -----")
                say(runner.build_prompt(step, ticket, base, ""))
            if cfg["final_review"]:
                say("\n----- final review prompt -----")
                say(runner.build_prompt("final", FinalUnit(feature), base, ""))
    return EXIT_OK if ok else EXIT_USAGE


def cmd_ticket(feature: Feature, action: str, ref: str, reason: str) -> int:
    """retry / reset / skip: edits the ticket and the runner's state between runs."""
    lock = Lock(feature.runs / ".lock", feature.dir)
    lock.acquire()
    try:
        state = State(feature.runs / "state.json")
        if ref.lower() in ("final", FINAL):
            if action == "skip":
                raise UsageError("set final_review = false to skip the final review")
            state.data["tickets"].pop(FINAL, None)
            state.save()
            say(f"final review: {'state cleared' if action == 'reset' else 'will run again'}; "
                f"`run-tickets run {feature.dir}` to continue")
            return EXIT_OK
        ticket = feature.find(ref)
        entry = state.data["tickets"].get(ticket.name)
        if action == "retry":
            pending = bool(entry) and entry.get("phase") in TICKET_PHASES
            if ticket.status() == DONE and not pending:
                raise UsageError(f"{ticket.name} is done; use `reset` to do it again")
            if ticket.status() != RUNNABLE:
                ticket.set_status(RUNNABLE)
            if entry:
                entry.pop("last_stop", None)
                entry.pop("paused", None)
                state.save()
            where = entry.get("phase") if entry and entry.get("phase") in TICKET_PHASES else "implement"
            say(f"{ticket.name}: ready-for-agent; the next run resumes it at {where}")
        elif action == "reset":
            state.data["tickets"].pop(ticket.name, None)
            state.save()
            ticket.set_status(RUNNABLE)
            ticket.untick()
            say(f"{ticket.name}: reset (ready-for-agent, criteria unticked); the next run starts "
                f"it from implement on the current HEAD")
        elif action == "skip":
            state.data["tickets"].pop(ticket.name, None)
            state.save()
            ticket.set_status("wontfix")
            ticket.add_comment(f"- {now():%Y-%m-%d %H:%M} skipped with run-tickets"
                               + (f": {reason}" if reason else ""))
            say(f"{ticket.name}: wontfix; tickets blocked by it will wait")
        say(f"run: run-tickets run {feature.dir}")
        return EXIT_OK
    finally:
        lock.release()


def cmd_adopt(feature: Feature, feature_base: Optional[str], resume: List[str]) -> int:
    """Takes over a feature another runner (or a person) started: the final review's base,
    and tickets to continue at a given step from a given base commit."""
    if not feature_base and not resume:
        raise UsageError("adopt needs --feature-base and/or --resume NN:PHASE:COMMIT")
    git = Git(feature.dir)
    lock = Lock(feature.runs / ".lock", feature.dir)
    lock.acquire()
    try:
        state = State(feature.runs / "state.json")
        branch = (state.data.get("git") or {}).get("branch") or "HEAD"

        def commit(rev: str, what: str) -> str:
            r = git.run("rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
            if r.returncode != 0:
                raise UsageError(f"{what}: '{rev}' is not a commit in {git.repo}")
            sha = r.stdout.strip()
            if not git.is_ancestor(sha, branch):
                warn(f"warning: {what} {sha[:10]} is not in the history of {branch}; a run "
                     f"ignores a base that is not, and starts that ticket (or the final "
                     f"review's range) from the current HEAD")
            return sha

        plans = []
        for item in resume:
            parts = item.split(":")
            if len(parts) != 3 or parts[1] not in TICKET_PHASES:
                raise UsageError(f"--resume '{item}': use NN:PHASE:COMMIT, PHASE one of "
                                 f"{', '.join(TICKET_PHASES)}")
            ticket = feature.find(parts[0])
            if ticket.status() == DONE:
                raise UsageError(f"{ticket.name} is done; `run-tickets reset` it first to do it again")
            plans.append((ticket, parts[1], commit(parts[2], f"--resume {ticket.name}")))
        base = commit(feature_base, "--feature-base") if feature_base else ""

        if base:
            state.data["feature_base"] = base
            say(f"feature base: {base[:10]} (the final review looks at everything since it)")
        for ticket, phase, sha in plans:
            state.data["tickets"][ticket.name] = {
                "phase": phase, "base": sha, "running": False, "started": iso(),
                "adopted": iso(), "updated": iso()}
            if ticket.status() != RUNNABLE:
                say(f"{ticket.name}: status {ticket.status() or 'missing'} -> {RUNNABLE}")
                ticket.set_status(RUNNABLE)
            say(f"{ticket.name}: the next run continues it at {phase} with base {sha[:10]}"
                + (" (no commits since: verify instead of review)"
                   if phase == "review" and git.is_ancestor(sha, branch)
                   and git.out("rev-parse", branch) == sha else ""))
        state.save()
        say(f"run: run-tickets run {feature.dir}")
        return EXIT_OK
    finally:
        lock.release()


HELP_EPILOG = """examples:
  run-tickets init --agent claude            # .ticket-runner.toml in the repo root
  run-tickets config .scratch/my-feature      # what will run, with which settings
  run-tickets status .scratch/my-feature      # done / next / stuck / running now
  run-tickets status .scratch/my-feature --watch
  run-tickets run    .scratch/my-feature      # run until done, blocked or stopped
  run-tickets run    .scratch/feat-a .scratch/feat-b --keep-going
  run-tickets retry  .scratch/my-feature 05   # rerun the step 05 stopped at
  run-tickets reset  .scratch/my-feature 05   # do 05 again from scratch
  run-tickets skip   .scratch/my-feature 05 --reason "covered by 06"
  run-tickets adopt  .scratch/my-feature --feature-base abc123 --resume 16:review:def456

The feature dir can be given from anywhere; the runner works in the git repo that
contains it. Config: --config, else .ticket-runner.toml in the feature dir, else in
the repo root; command-line flags win over the file.
exit codes: 0 all done, 1 stopped, 2 usage/setup error, 3 open tickets left,
4 paused, 130 interrupted
"""

COMMANDS = ("run", "status", "config", "init", "retry", "reset", "skip", "adopt")


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run-tickets", description=__doc__.split("\n\n")[0],
                                epilog=HELP_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    def config_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", help="config file (default: .ticket-runner.toml in the feature dir or repo root)")

    def ui_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--ui", choices=UI_MODES,
                        help="live: colors and a status line; plain: plain lines "
                             "(default auto: live on a terminal, plain when piped)")

    def overrides(sp: argparse.ArgumentParser) -> None:
        ui_arg(sp)
        sp.add_argument("--agent", help="omp | pi | claude | codex | a name from the config")
        sp.add_argument("--model", help="model name passed to the agent")
        sp.add_argument("--max-time", dest="max_time", help="per step, e.g. 3h, 90m (default 3h)")
        sp.add_argument("--retries", type=int, help="fresh-process retries of a failed step (default 1)")
        sp.add_argument("--test-command", dest="test_command", help="tests the runner runs after every step")
        sp.add_argument("--branch", help="work on this branch, e.g. feat/{{feature}}")
        sp.add_argument("--worktree", action="store_true", default=None,
                        help="work in a separate git worktree on that branch")
        sp.add_argument("--no-final-review", dest="final_review", action="store_false", default=None,
                        help="skip the review of the whole feature at the end")
        sp.add_argument("--pause-after", dest="pause_after", action="append",
                        help="stop after this ticket for a human look (repeatable)")

    r = sub.add_parser("run", help="implement and review tickets until done, blocked or stopped")
    r.add_argument("features", nargs="+", metavar="feature",
                   help="feature directory with issues/NN-*.md; several run one after another")
    config_arg(r)
    overrides(r)
    r.add_argument("--once", action="store_true", help="stop after one ticket (per feature)")
    r.add_argument("--keep-going", action="store_true",
                   help="with several features, go on to the next one after a stop")
    r.add_argument("--dry-run", action="store_true", help="same as the status command")
    r.add_argument("--adopt-dirty", action="store_true",
                   help="after a crash mid-step, let the agent continue with the uncommitted changes")

    s = sub.add_parser("status", help="show tickets, the next one, what runs now and where it stopped")
    s.add_argument("feature")
    ui_arg(s)
    s.add_argument("--watch", nargs="?", type=float, const=5.0, metavar="SECONDS",
                   help="refresh every few seconds (default 5)")

    c = sub.add_parser("config", help="show the effective config and the agent command lines")
    c.add_argument("feature")
    config_arg(c)
    overrides(c)
    c.add_argument("--prompts", action="store_true", help="also print the prompts of the next ticket")

    for name, text in (("retry", "rerun the step a ticket stopped at (sets it ready-for-agent)"),
                       ("reset", "do a ticket again from scratch (state cleared, criteria unticked)"),
                       ("skip", "mark a ticket wontfix with a comment")):
        t = sub.add_parser(name, help=text)
        t.add_argument("feature")
        t.add_argument("ticket", help="number (05), name (05-slug), or 'final' for the final review")
        t.add_argument("--reason", default="", help="why (written to the ticket's Comments)")

    a = sub.add_parser("adopt", help="take over a feature another runner started")
    a.add_argument("feature")
    a.add_argument("--feature-base", dest="feature_base", metavar="COMMIT",
                   help="the commit before the feature's first ticket (the final review's base)")
    a.add_argument("--resume", action="append", default=[], metavar="NN:PHASE:COMMIT",
                   help="continue ticket NN at PHASE (implement, review, verify) with base "
                        "COMMIT, the commit before its implement step (repeatable)")

    i = sub.add_parser("init", help="write an example .ticket-runner.toml")
    i.add_argument("dir", nargs="?", help="where to write it (default: the repo root)")
    i.add_argument("--agent", choices=sorted(AGENT_PRESETS), help="agent to put in the file")

    # `run-tickets <feature> [--opts]` works as `run-tickets run <feature>`.
    if argv and argv[0] not in COMMANDS + ("-h", "--help", "--version"):
        argv = ["run", *argv]
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        raise SystemExit(EXIT_USAGE)
    return args


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def run_feature(path: Path, args: argparse.Namespace) -> int:
    feature_lock = work_lock = None
    try:
        feature = Feature(path)
        main_git = Git(feature.dir)
        main_git.head()
        cfg = build_config(find_config(feature.dir, main_git.repo, args.config), args)
        if not (args.ui or os.environ.get("RUN_TICKETS_UI")):
            UI.setup(cfg["ui"])
        for step in STEPS:
            if step != "final" or cfg["final_review"]:
                resolve_executable(fill(cfg["step"][step]["spec"]["command"][0], {}))
        feature.runs.mkdir(parents=True, exist_ok=True)
        feature_lock = Lock(feature.runs / ".lock", feature.dir)
        feature_lock.acquire()
        state = State(feature.runs / "state.json")
        work = prepare_workdir(feature, main_git, cfg, state)
        work.head()
        work_lock = Lock(work.git_dir / "run-tickets.lock", feature.dir)
        work_lock.acquire()
        runner = Runner(feature, work, cfg, args, state, main_git)
    except UsageError as e:
        for lock in (work_lock, feature_lock):
            if lock:
                lock.release()
        warn(f"run-tickets: {e}")
        return EXIT_USAGE
    try:
        return runner.run()
    except (KeyboardInterrupt, Interrupted) as e:
        runner.interrupt()
        reason = str(e) or "Ctrl+C"
        runner.event("interrupted", reason=reason)
        warn(f"\ninterrupted ({reason}); the state is saved, run the same command to resume")
        return EXIT_INTERRUPTED
    except UsageError as e:
        runner.interrupt()
        warn(f"run-tickets: {e}")
        return EXIT_USAGE
    except Exception as e:
        runner.interrupt()
        runner.event("crash", error=repr(e))
        runner.notify(f"run-tickets crashed: {e!r}")
        raise
    finally:
        work_lock.release()
        feature_lock.release()


# Which exit code wins when a queue of features ends with several.
SEVERITY = [EXIT_OK, EXIT_BLOCKED, EXIT_PAUSED, EXIT_USAGE, EXIT_STOPPED, EXIT_INTERRUPTED]


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        UI.setup(getattr(args, "ui", None) or os.environ.get("RUN_TICKETS_UI") or "auto")
        if args.command == "init":
            return cmd_init(args.dir, args.agent)
        if args.command == "status":
            feature = Feature(Path(args.feature))
            if not (args.ui or os.environ.get("RUN_TICKETS_UI")):
                UI.setup(configured_ui(feature))
            return cmd_status(feature, args.watch)
        if args.command == "config":
            return cmd_config(Feature(Path(args.feature)), args)
        if args.command == "adopt":
            return cmd_adopt(Feature(Path(args.feature)), args.feature_base, args.resume)
        if args.command in ("retry", "reset", "skip"):
            return cmd_ticket(Feature(Path(args.feature)), args.command, args.ticket, args.reason)
        if args.dry_run:
            for f in args.features:
                cmd_status(Feature(Path(f)), None)
            return EXIT_OK
    except UsageError as e:
        warn(f"run-tickets: {e}")
        return EXIT_USAGE

    def on_signal(signum: int, _frame: Any) -> None:
        raise Interrupted(signal.Signals(signum).name)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (OSError, ValueError):
                pass

    worst = EXIT_OK
    for i, f in enumerate(args.features):
        if len(args.features) > 1:
            say(f"\n===== feature {i + 1}/{len(args.features)}: {f} =====")
        code = run_feature(Path(f), args)
        if SEVERITY.index(code) > SEVERITY.index(worst):
            worst = code
        if code == EXIT_INTERRUPTED:
            break
        if code in (EXIT_STOPPED, EXIT_USAGE, EXIT_PAUSED) and not args.keep_going:
            if i + 1 < len(args.features):
                say(f"not starting the remaining {len(args.features) - i - 1} feature(s); "
                    f"--keep-going would go on")
            break
    return worst


if __name__ == "__main__":
    sys.exit(main())
