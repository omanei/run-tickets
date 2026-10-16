#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ['tomli>=1.1; python_version < "3.11"']
# ///
"""run-tickets: implements a feature's tickets one after another with a coding agent.

Works on Linux, macOS and Windows with Python 3.9+ and git; no third-party packages
(a .ticket-runner.toml config needs Python 3.11+ or the `tomli` package).

A feature is a directory with issues/NN-<slug>.md tickets (the /to-tickets format of
Matt Pocock's skills). Each ticket gets two fresh agent processes, so nothing carries
over from the previous ticket or step:
  1. implement - the implement skill on the ticket, work committed;
  2. review    - the code-review skill on the ticket's commits, findings fixed and
                 committed, every checkbox verified and ticked, Status set to done.
The next ticket starts only after the previous one passed both steps and every check
the runner makes itself. The agent's own words about the repository are never trusted.

  run-tickets run    .scratch/my-feature     # run until done, blocked or stopped
  run-tickets status .scratch/my-feature     # what is done, what is next, what is stuck
  run-tickets config .scratch/my-feature     # effective settings and the agent command
  run-tickets run    .scratch/my-feature --once
  run-tickets init                          # write an example .ticket-runner.toml

State survives crashes: <feature>/runs/state.json is rewritten atomically before and
after every step, so a run killed at any point (Ctrl+C, kill -9, power loss) resumes
at the interrupted step of the same ticket with the same base commit.

Exit codes: 0 all tickets done; 1 stopped on a failed check; 2 usage or setup error;
3 nothing runnable but tickets remain open; 130 interrupted.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.1.0"

IS_WINDOWS = os.name == "nt"

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_INTERRUPTED = 130

# Statuses of the local tracker. "done" is the only one the runner treats as finished.
RUNNABLE = "ready-for-agent"
DONE = "done"
CLOSED = {"done", "wontfix"}

CONFIG_NAMES = (".ticket-runner.toml",)

# ---------------------------------------------------------------------------
# Agents

# Each preset is a command template. Placeholders: {{prompt}} {{repo}} {{session_dir}}
# {{final_file}}. model_args are inserted only when a model is set: at a "{{model_args}}"
# item, or else right after the executable. prompt_via: "arg" or "stdin".
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
        "export_command": [],
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "agent": "omp",
    "model": "",
    "max_time": "3h",
    "retries": 1,
    "retry_delay": "1m",
    "desktop_notify": True,
    "skills": {"implement": "implement", "review": "code-review"},
    "report_heading": "Step report",
    "report_language": "English",
    "preflight": [],
    "notify_command": "",
    "extra_instructions": "",
    "review_extra_instructions": "",
    "suspicious_patterns": [
        r"pytest\.mark\.skip", r"\bxfail\b", r"\.skip\(", r"\bxit\(", r"\bxdescribe\(",
        r"@Disabled", r"@Ignore", r"t\.Skip\(", r"#\[ignore\]", r"\bunittest\.skip",
    ],
}

COMMON_PROMPT = """You run unattended: nobody will answer questions. Decide by the spec ({{spec}}), the repo's ADRs and docs.
If you cannot finish, set the ticket's status line to `**Status:** needs-info`, append the reason under a `## Comments` heading at the end of the ticket file, and stop.
End your final answer with a section headed `## {{report_heading}}`, written in {{report_language}}, for whoever reads the run in the morning: what you did (commit hashes), which checks you ran and their results, what is left or failed, and - if you stopped early or left anything uncommitted - exactly why.
Never run git stash, git reset --hard, git clean, git rebase, git commit --amend, git push, git checkout of another branch, or git checkout/restore of files you did not change: the working tree, the branch and the stash list hold the user's own work. To test the code without your changes, use git worktree add in a temporary directory and remove it afterwards.
Never weaken a test, raise a threshold or add a skip to make tests pass; if you believe one is wrong, say why under `## Comments` in the ticket and in your step report.
{{extra_instructions}}"""

IMPLEMENT_PROMPT = """{{implement_command}}

{{common}}
Skip the code review at the end: a separate step reviews this ticket after you. Do not change the ticket's status line and do not tick its checkboxes; the review step does that.
Commit all your work to the current branch.
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


def parse_duration(text: str) -> int:
    """'3h', '90m', '1h30m', '45s', '600' -> seconds."""
    text = str(text).strip().lower()
    if text.isdigit():
        return int(text)
    parts = re.findall(r"(\d+)\s*([hms])", text)
    if not parts or "".join(n + u for n, u in parts) != re.sub(r"\s+", "", text):
        raise UsageError(f"bad duration '{text}', use e.g. 3h, 90m, 1h30m, 600")
    mult = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def fill(template: str, values: Dict[str, str]) -> str:
    """Replaces {{name}} placeholders; unknown ones stay as they are."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
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


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Tickets

# "**Status:** done", "**Status**: done", "Status: done", "- Status: done".
STATUS_RE = re.compile(r"^\s*(?:[-*]\s+)?\**\s*status\s*(?::\s*\**|\**\s*:)\s*\**\s*([a-z][a-z_-]*)",
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

    def text(self) -> str:
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


class Feature:
    def __init__(self, path: Path):
        self.dir = path.resolve()
        self.issues = self.dir / "issues"
        self.runs = self.dir / "runs"
        if not self.issues.is_dir():
            raise UsageError(f"{self.dir} has no issues/ directory")

    def tickets(self) -> List[Ticket]:
        found = [Ticket(p) for p in self.issues.iterdir()
                 if p.is_file() and TICKET_NAME_RE.match(p.name)]
        return sorted(found, key=lambda t: (t.number, t.name))

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

    def out(self, *args: str) -> str:
        r = self.run(*args)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    def head(self) -> str:
        r = self.run("rev-parse", "--verify", "-q", "HEAD")
        if r.returncode != 0:
            raise UsageError("the repository has no commits yet; make an initial commit first")
        return r.stdout.strip()

    def branch(self) -> str:
        r = self.run("symbolic-ref", "-q", "--short", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else "(detached)"

    def stash(self) -> str:
        r = self.run("rev-parse", "-q", "--verify", "refs/stash")
        return r.stdout.strip() if r.returncode == 0 else ""

    def is_ancestor(self, a: str, b: str = "HEAD") -> bool:
        return bool(a) and self.run("merge-base", "--is-ancestor", a, b).returncode == 0

    def tracked_changes(self, exclude: Optional[Path] = None) -> str:
        """Uncommitted changes to tracked files, ignoring the feature dir (the agent edits
        the ticket, and the runner writes runs/ there)."""
        args = ["status", "--porcelain", "--untracked-files=no", "--", "."]
        if exclude is not None:
            try:
                rel = os.path.relpath(str(exclude.resolve()), str(self.repo))
            except ValueError:  # another drive on Windows
                rel = ""
            if rel and rel != "." and not rel.startswith(".."):
                args.append(":(exclude)" + rel.replace(os.sep, "/"))
        return self.out(*args)

    def commits(self, base: str) -> List[str]:
        out = self.out("log", "--format=%h %s", f"{base}..HEAD")
        return [line for line in out.splitlines() if line]

    def diff_stat(self, base: str) -> str:
        return self.out("diff", "--stat", f"{base}..HEAD")

    def added_lines(self, base: str) -> List[Tuple[str, str]]:
        """(file, line) for every line added between base and HEAD."""
        out = self.out("diff", "--unified=0", "--no-color", f"{base}..HEAD")
        result, current = [], ""
        for line in out.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("+") and not line.startswith("+++"):
                result.append((current, line[1:]))
        return result


# ---------------------------------------------------------------------------
# Lock: one runner per repository (two runners would fight over the same branch).


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

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _take_over_stale(self) -> bool:
        info = self._read()
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
        info = self._read()
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
    """runs/state.json, rewritten atomically on every change. Per ticket:
    phase      implement | review | done
    running    true while an agent process for `phase` may be alive
    base       HEAD before the ticket's first implement attempt (kept across attempts)
    stash      refs/stash before the running step (to check it after a crash too)
    branch     the branch the ticket runs on
    last_stop  the last failed check, if any
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
        return tomllib.load(f)


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


def build_config(path: Optional[Path], args: argparse.Namespace) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = load_toml(path) if path else {}
    sources = {key: "default" for key in cfg}
    for key, value in raw.items():
        sources[key] = "config file"
        if key in ("skills",) and isinstance(value, dict):
            cfg[key].update(value)
        elif key == "prompts" and isinstance(value, dict):
            cfg["prompts"] = value
        elif key == "agents" and isinstance(value, dict):
            cfg["agents"] = value
        else:
            cfg[key] = value
    for key in ("agent", "model", "max_time"):
        v = getattr(args, key, None)
        if v:
            cfg[key] = v
            sources[key] = "command line"
    if getattr(args, "retries", None) is not None:
        cfg["retries"] = args.retries
        sources["retries"] = "command line"
    cfg["sources"] = sources

    name = cfg["agent"]
    agent = dict(AGENT_PRESETS.get(name, {}))
    agent.update((cfg.get("agents") or {}).get(name, {}))
    if not agent.get("command"):
        raise UsageError(f"unknown agent '{name}'; known: {', '.join(AGENT_PRESETS)}, "
                         f"or define [agents.{name}] command = [...] in the config")
    agent.setdefault("model_args", ["--model", "{{model}}"])
    agent.setdefault("prompt_via", "arg")
    agent.setdefault("skill_format", "/{{skill}} {{args}}")
    agent.setdefault("output", "text")
    agent.setdefault("export_command", [])
    if agent["prompt_via"] not in ("arg", "stdin"):
        raise UsageError("prompt_via must be 'arg' or 'stdin'")
    cfg["agent_spec"] = agent
    cfg["max_seconds"] = parse_duration(cfg["max_time"])
    cfg["retries"] = max(0, int(cfg["retries"]))
    cfg["retry_delay_seconds"] = parse_duration(cfg["retry_delay"])
    if isinstance(cfg.get("preflight"), str):
        cfg["preflight"] = [cfg["preflight"]]
    cfg["config_path"] = str(path) if path else ""
    return cfg


EXAMPLE_CONFIG = '''# run-tickets config: https://github.com/omanei/run-tickets#configuration
# Looked up as: --config, else .ticket-runner.toml in the feature dir, else in the repo
# root. Command-line flags win over this file. Every key is optional: the commented
# lines show the defaults; uncomment and change only what your project needs.
# `run-tickets config <feature dir>` prints the effective values and the agent command.

agent = "{{init_agent}}"           # omp | pi | claude | codex, or a name under [agents.<name>]
# model = ""                # passed to the agent (its model_args) when set
# max_time = "3h"           # per step; the runner kills the agent's process tree after it
# retries = 1               # fresh-process retries of a failed step before the run stops
# retry_delay = "1m"        # pause before a retry (provider limits, flaky services)
# desktop_notify = true     # macOS notification / notify-send / Windows balloon

# The agent ends each step with a report under this heading, in this language.
# report_heading = "Step report"     # e.g. "Отчёт о шаге"
# report_language = "English"        # e.g. "Russian"

# Shell commands that must succeed before the run and before every ticket; a failure
# stops the run before any agent starts. Run in the repo root (cmd.exe on Windows).
# preflight = [
#   "docker compose ps --status running --services | grep -q app",
# ]

# A shell command run on stop and at the end. The text is in $RUN_TICKETS_MESSAGE
# (%RUN_TICKETS_MESSAGE% in cmd.exe), the report path in $RUN_TICKETS_REPORT.
# notify_command = 'curl -s -X POST -H "Content-type: application/json" --data "{\\"text\\": \\"$RUN_TICKETS_MESSAGE\\"}" "$SLACK_WEBHOOK_URL"'

# Project rules, appended to both prompts / to the review prompt only.
# extra_instructions = ""
# review_extra_instructions = "Run the tests only inside Docker, as the repo's testing docs say."

# Skill names. Skills from Claude Code plugins carry the plugin prefix: "<plugin>:code-review".
# [skills]
# implement = "implement"
# review = "code-review"

# Change a preset or add your own agent. Placeholders: {{prompt}} {{repo}}
# {{session_dir}} {{final_file}}. model_args go at a "{{model_args}}" item, or right
# after the executable, and only when a model is set. export_command gets
# {{session_file}} and {{html_file}} and runs in the step's session dir.
# [agents.omp]
# command = ["omp", "-p", "--auto-approve", "--session-dir", "{{session_dir}}", "--cwd", "{{repo}}", "{{prompt}}"]
# model_args = ["--model", "{{model}}"]
# prompt_via = "arg"                         # or "stdin"
# skill_format = "/skill:{{skill}} {{args}}"
# output = "text"                            # or "claude-stream-json"
# export_command = ["omp", "--export", "{{session_file}}"]

# Replace the prompt templates entirely. Placeholders: {{implement_command}}
# {{review_command}} {{common}} {{ticket}} {{base}} {{spec}} {{feature_dir}} {{repo}}
# {{resume_note}} {{report_heading}} {{report_language}} and the *_instructions keys.
# [prompts]
# common = """..."""
# implement = """..."""
# review = """..."""
'''


# ---------------------------------------------------------------------------
# Running the agent


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


def fill_command(cfg: Dict[str, Any], prompt: str, repo: str, session_dir: str,
                 final_file: str) -> List[str]:
    """The agent's argv from its command template, before the executable is resolved."""
    agent = cfg["agent_spec"]
    values = {"prompt": prompt, "repo": repo, "session_dir": session_dir,
              "final_file": final_file, "model": cfg.get("model") or ""}
    template = list(agent["command"])
    if agent["prompt_via"] == "stdin":
        template = [a for a in template if a != "{{prompt}}"]
    elif "{{prompt}}" not in template:
        template.append("{{prompt}}")
    model_args = list(agent["model_args"]) if values["model"] else []
    if "{{model_args}}" in template:
        i = template.index("{{model_args}}")
        template[i:i + 1] = model_args
    else:
        # Right after the executable, before any positional prompt.
        template[1:1] = model_args
    return [fill(a, values) for a in template]


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


def describe_stream_event(line: str) -> Optional[str]:
    """One console line of live progress for a claude stream-json event."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line.rstrip("\n")
    if ev.get("type") == "assistant":
        parts = []
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                hint = inp.get("description") or inp.get("command") or inp.get("file_path") or ""
                parts.append(f"  · {c.get('name')}: {str(hint).splitlines()[0][:120] if hint else ''}")
            elif c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].rstrip())
        return "\n".join(parts) if parts else None
    if ev.get("type") == "result":
        return f"  · result: {ev.get('subtype')}, {ev.get('num_turns')} turns, ${ev.get('total_cost_usd', 0):.2f}"
    return None


def final_text_from_stream(log: Path) -> Tuple[str, bool]:
    """(final answer, is_error) from a claude stream-json log."""
    text, is_error = "", False
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            text = ev.get("result") or text
            is_error = bool(ev.get("is_error"))
    return text, is_error


class StepRun:
    def __init__(self):
        self.code: Optional[int] = None
        self.timed_out = False
        self.log: Optional[Path] = None
        self.final: str = ""
        self.transcript: Optional[Path] = None
        self.seconds: float = 0


class Runner:
    def __init__(self, feature: Feature, git: Git, cfg: Dict[str, Any], opts: argparse.Namespace):
        self.feature = feature
        self.git = git
        self.cfg = cfg
        self.opts = opts
        self.agent = cfg["agent_spec"]
        self.state = State(feature.runs / "state.json")
        self.report = feature.runs / "REPORT.md"
        self.events = feature.runs / "events.jsonl"
        self.proc: Optional[subprocess.Popen] = None
        self.run_id = uuid.uuid4().hex[:8]
        self.done_this_run: List[Tuple[str, float, int]] = []

    # -- bookkeeping -------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        rec = {"time": iso(), "run": self.run_id, "event": kind, **fields}
        append_durable(self.events, json.dumps(rec, ensure_ascii=False) + "\n")

    def record(self, ticket: Ticket, step: str, result: str, run: Optional[StepRun],
               base: str = "") -> None:
        lines = [f"## {now():%Y-%m-%d %H:%M} {ticket.name} - {step}: {result}", ""]
        if run is not None:
            if run.log:
                lines.append(f"- log: {run.log}")
            if run.transcript:
                lines.append(f"- transcript: {run.transcript}")
            lines.append(f"- duration: {fmt_duration(run.seconds)}, exit code: {run.code}"
                         + (" (timed out)" if run.timed_out else ""))
        if base:
            try:
                commits = self.git.commits(base)
                lines.append(f"- commits since {base[:10]}: {len(commits)}")
                lines += [f"  - {c}" for c in commits[:30]]
                suspicious = self.suspicious(base)
                if suspicious:
                    lines.append("- **check these test changes** (skips, xfail, ignores added):")
                    lines += [f"  - `{f}`: `{l.strip()[:150]}`" for f, l in suspicious[:20]]
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
        pats = [re.compile(p) for p in self.cfg.get("suspicious_patterns") or []]
        if not pats:
            return []
        return [(f, l) for f, l in self.git.added_lines(base) if any(p.search(l) for p in pats)]

    def notify(self, message: str) -> None:
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
                       RUN_TICKETS_REPORT=str(self.report))
            try:
                subprocess.run(cmd, shell=True, env=env, timeout=60, capture_output=True)
            except (OSError, subprocess.SubprocessError) as e:
                warn(f"notify_command failed: {e}")

    # -- the agent ---------------------------------------------------------

    def skill_command(self, skill: str, args: str) -> str:
        return fill(self.agent["skill_format"],
                    {"skill": self.cfg["skills"][skill], "args": args}).strip()

    def build_prompt(self, step: str, ticket: Ticket, base: str, resume_note: str) -> str:
        spec = self.feature.dir / "spec.md"
        values = {
            "ticket": str(ticket.path),
            "feature_dir": str(self.feature.dir),
            "spec": str(spec) if spec.exists() else "the feature's spec",
            "base": base,
            "repo": str(self.git.repo),
            "report_heading": self.cfg["report_heading"],
            "report_language": self.cfg["report_language"],
            "extra_instructions": self.cfg.get("extra_instructions", ""),
            "review_extra_instructions": self.cfg.get("review_extra_instructions", ""),
            "resume_note": resume_note,
            "implement_command": self.skill_command("implement", str(ticket.path)),
            "review_command": self.skill_command(
                "review", f"since {base} for the ticket {ticket.path}"),
        }
        prompts = self.cfg.get("prompts") or {}
        values["common"] = fill(prompts.get("common", COMMON_PROMPT), values).strip()
        template = prompts.get(step, IMPLEMENT_PROMPT if step == "implement" else REVIEW_PROMPT)
        text = fill(template, values)
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    def agent_argv(self, prompt: str, session_dir: Path, final_file: Path) -> List[str]:
        argv = fill_command(self.cfg, prompt, str(self.git.repo), str(session_dir), str(final_file))
        return resolve_executable(argv[0]) + argv[1:]

    def run_agent(self, ticket: Ticket, step: str, prompt: str) -> StepRun:
        runs = self.feature.runs
        stamp = f"{ticket.name}-{step}-{now():%Y%m%d-%H%M%S}"
        if (runs / f"{stamp}.log").exists():  # two attempts within a second
            stamp += f"-{uuid.uuid4().hex[:4]}"
        session_dir = runs / "sessions" / stamp
        session_dir.mkdir(parents=True, exist_ok=True)
        run = StepRun()
        run.log = runs / f"{stamp}.log"
        final_file = session_dir / "final.md"
        atomic_write(runs / "prompts" / f"{stamp}.md", prompt)

        argv = self.agent_argv(prompt, session_dir, final_file)
        say(f"==> {ticket.name}: {step} (log: {run.log})")
        self.event("agent_start", ticket=ticket.name, step=step, log=str(run.log),
                   argv=[a if a != prompt else "<prompt>" for a in argv])

        kwargs: Dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        stream = self.agent["output"] == "claude-stream-json"
        started = time.monotonic()
        with open(run.log, "w", encoding="utf-8", newline="\n") as log:
            self.proc = subprocess.Popen(
                argv, cwd=str(self.git.repo),
                stdin=subprocess.PIPE if self.agent["prompt_via"] == "stdin" else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=dict(os.environ, RUN_TICKETS_TICKET=str(ticket.path), RUN_TICKETS_STEP=step),
                **kwargs)
            if self.agent["prompt_via"] == "stdin":
                assert self.proc.stdin is not None
                try:
                    self.proc.stdin.write(prompt.encode("utf-8"))
                    self.proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            def pump() -> None:
                assert self.proc is not None and self.proc.stdout is not None
                try:
                    for raw in iter(self.proc.stdout.readline, b""):
                        line = raw.decode("utf-8", errors="replace")
                        log.write(line)
                        log.flush()
                        shown = describe_stream_event(line) if stream else line.rstrip("\n")
                        if shown is not None:
                            say(shown)
                except (OSError, ValueError):
                    pass

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            try:
                run.code = self.proc.wait(timeout=self.cfg["max_seconds"])
            except subprocess.TimeoutExpired:
                run.timed_out = True
                say(f"==> {ticket.name}: {step} hit max_time {self.cfg['max_time']}, killing it")
                kill_tree(self.proc)
                run.code = self.proc.wait()
            except BaseException:
                kill_tree(self.proc)
                self.proc.wait()
                reader.join(timeout=10)
                raise
            reader.join(timeout=30)
        self.proc = None
        run.seconds = time.monotonic() - started

        if final_file.exists() and final_file.stat().st_size:
            run.final = final_file.read_text(encoding="utf-8", errors="replace")
        elif stream:
            run.final, is_error = final_text_from_stream(run.log)
            if is_error and run.code == 0:
                run.code = 1
        else:
            run.final = run.log.read_text(encoding="utf-8", errors="replace")
        run.transcript = self.export_transcript(session_dir, stamp) or (run.log if stream else None)
        self.event("agent_end", ticket=ticket.name, step=step, code=run.code,
                   timed_out=run.timed_out, seconds=round(run.seconds))
        return run

    def export_transcript(self, session_dir: Path, stamp: str) -> Optional[Path]:
        cmd = self.agent.get("export_command") or []
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

    # -- checks ------------------------------------------------------------

    def common_checks(self, ticket: Ticket, entry: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
        """(reason, retryable) of the first failed check shared by both steps, or None."""
        if not ticket.path.exists():
            return "the ticket file is gone", False
        if self.git.stash() != entry.get("stash", ""):
            return ("the git stash list changed - check 'git stash list' and "
                    "'git reflog show refs/stash'; the stash holds the user's own work"), False
        if self.git.branch() != entry.get("branch"):
            return f"the branch changed from {entry.get('branch')} to {self.git.branch()}", False
        if not self.git.is_ancestor(entry["base"]):
            return f"history was rewritten: the base {entry['base'][:10]} is no longer an ancestor of HEAD", False
        return None

    @staticmethod
    def agent_failed(run: Optional[StepRun], max_time: str) -> Optional[Tuple[str, bool]]:
        if run is not None and run.timed_out:
            return f"the agent ran out of max_time ({max_time})", True
        if run is not None and run.code != 0:
            return f"the agent exited with {run.code}", True
        return None

    def check_implement(self, ticket: Ticket, entry: Dict[str, Any], run: Optional[StepRun]) -> Optional[Tuple[str, bool]]:
        failed = self.common_checks(ticket, entry) or self.agent_failed(run, self.cfg["max_time"])
        if failed:
            return failed
        status = ticket.status()
        if status != RUNNABLE:
            return f"the ticket status is now '{status or 'missing'}'", False
        if self.git.head() == entry["base"]:
            return "no commit was made", True
        if self.git.tracked_changes(self.feature.dir):
            return "uncommitted changes were left", True
        return None

    def check_review(self, ticket: Ticket, entry: Dict[str, Any], run: Optional[StepRun]) -> Optional[Tuple[str, bool]]:
        failed = self.common_checks(ticket, entry) or self.agent_failed(run, self.cfg["max_time"])
        if failed:
            return failed
        status = ticket.status()
        if status != DONE:
            # ready-for-agent: the agent did not finish; anything else: it asked for a human.
            return f"the ticket status is '{status or 'missing'}', not done", status == RUNNABLE
        left = ticket.unchecked()
        if left:
            return f"{left} acceptance criteria are not ticked", True
        if self.git.tracked_changes(self.feature.dir):
            return "uncommitted changes were left", True
        return None

    # -- the loop ----------------------------------------------------------

    def preflight(self, when: str) -> Optional[str]:
        for cmd in self.cfg.get("preflight") or []:
            r = subprocess.run(cmd, shell=True, cwd=str(self.git.repo), capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                tail = (r.stdout + r.stderr).strip()[-1500:]
                return f"preflight ({when}) failed: `{cmd}` exited with {r.returncode}\n{tail}"
        return None

    def stop(self, ticket: Optional[Ticket], step: str, reason: str, run: Optional[StepRun] = None,
             base: str = "") -> int:
        if ticket is not None:
            self.state.update(ticket.name, running=False,
                              last_stop={"step": step, "reason": reason, "time": iso()})
            self.record(ticket, step, f"STOPPED - {reason}", run, base)
        else:
            append_durable(self.report, f"## {now():%Y-%m-%d %H:%M} STOPPED - {reason}\n\n")
        self.event("stopped", ticket=ticket.name if ticket else None, step=step, reason=reason)
        where = f"{ticket.name} ({step})" if ticket else step
        self.notify(f"Stopped at {where}: {reason.splitlines()[0]}")
        if run is not None:
            say(f"log: {run.log}")
            say(f"transcript: {run.transcript or 'none'}")
        say(f"report: {self.report}")
        return EXIT_STOPPED

    def run(self) -> int:
        say(f"run-tickets {__version__}: {self.feature.dir} with agent '{self.cfg['agent']}'"
            + (f" ({self.cfg['config_path']})" if self.cfg["config_path"] else ""))
        self.event("run_start", agent=self.cfg["agent"], pid=os.getpid())
        recovered = self.recover_finished_step()
        if recovered is not None:
            return recovered

        dirty = self.git.tracked_changes(self.feature.dir)
        if dirty:
            pending = [n for n, e in self.state.data["tickets"].items()
                       if e.get("phase") in ("implement", "review")
                       and (e.get("running") or e.get("last_stop"))]
            if not (pending and self.opts.adopt_dirty):
                warn("the working tree has uncommitted changes; commit or stash them first:")
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
            if self.opts.once:
                self.summary()
                return EXIT_OK

    def recover_finished_step(self) -> Optional[int]:
        """A step that was running when the runner died but whose ticket is already done
        (the agent finished, the runner did not live to check it): check it now, so it is
        neither skipped unchecked nor rerun."""
        for name, entry in list(self.state.data["tickets"].items()):
            if not entry.get("running"):
                continue
            path = self.feature.issues / f"{name}.md"
            if not path.exists():
                continue
            ticket = Ticket(path)
            if ticket.status() == RUNNABLE:
                continue  # resumed by the loop
            say(f"==> {name}: the runner stopped during its {entry.get('phase')} step; checking it now")
            check = self.check_review if entry.get("phase") == "review" else self.check_implement
            failed = check(ticket, entry, None)
            if failed:
                return self.stop(ticket, entry.get("phase", "?"),
                                 f"{failed[0]} (checked after an interrupted run)", None, entry.get("base", ""))
            if entry.get("phase") == "review":
                self.state.update(name, phase="done", running=False, last_stop=None)
                self.record(ticket, "review", "ok - ticket done (checked after an interrupted run)",
                            None, entry["base"])
            else:
                self.state.update(name, phase="review", running=False)
        return None

    def run_ticket(self, ticket: Ticket) -> int:
        entry = self.state.ticket(ticket.name)
        resuming = (entry.get("phase") in ("implement", "review") and entry.get("base")
                    and self.git.is_ancestor(entry["base"]))
        if not resuming:
            if entry.get("phase") in ("implement", "review"):
                say(f"==> {ticket.name}: earlier base {str(entry.get('base'))[:10]} is not in "
                    f"HEAD's history any more; starting the ticket over")
            entry.clear()
            self.state.update(ticket.name, phase="implement", base=self.git.head(),
                              branch=self.git.branch(), running=False, started=iso())
            entry = self.state.ticket(ticket.name)
        elif entry.get("branch") != self.git.branch():
            return self.stop(ticket, entry["phase"],
                             f"the ticket was started on branch {entry.get('branch')}, "
                             f"now on {self.git.branch()}; switch back or delete its entry in "
                             f"{self.state.path}")
        else:
            say(f"==> {ticket.name}: resuming at {entry['phase']} (base {entry['base'][:10]})")

        pre = self.preflight(ticket.name)
        if pre:
            return self.stop(ticket, entry["phase"], pre, None, entry["base"])

        started = time.monotonic()
        for step in ("implement", "review"):
            if step == "implement" and entry["phase"] != "implement":
                continue
            code = self.run_step(ticket, step)
            if code != EXIT_OK:
                return code
        commits = len(self.git.commits(entry["base"]))
        self.done_this_run.append((ticket.name, time.monotonic() - started, commits))
        say(f"==> {ticket.name} done: {commits} commit(s)")
        return EXIT_OK

    def run_step(self, ticket: Ticket, step: str) -> int:
        entry = self.state.ticket(ticket.name)
        check = self.check_implement if step == "implement" else self.check_review
        note = self.resume_note(ticket, step, entry)
        attempts = self.cfg["retries"] + 1
        for attempt in range(1, attempts + 1):
            prompt = self.build_prompt(step, ticket, entry["base"], note)
            self.state.update(ticket.name, phase=step, running=True, stash=self.git.stash(),
                              branch=self.git.branch(), attempt=attempt)
            run = self.run_agent(ticket, step, prompt)
            failed = check(ticket, entry, run)
            if failed is None:
                if step == "implement":
                    self.state.update(ticket.name, phase="review", running=False, last_stop=None,
                                      implemented=self.git.head())
                    self.record(ticket, step, "ok", run, entry["base"])
                else:
                    self.state.update(ticket.name, phase="done", running=False, last_stop=None,
                                      finished=iso())
                    self.record(ticket, step, "ok - ticket done", run, entry["base"])
                return EXIT_OK
            reason, retryable = failed
            if not retryable or attempt == attempts:
                return self.stop(ticket, step, reason, run, entry["base"])
            self.state.update(ticket.name, running=False)
            self.record(ticket, step, f"FAILED, retrying ({attempt}/{attempts - 1}) - {reason}", run,
                        entry["base"])
            say(f"==> {ticket.name}: {step} failed ({reason}); retrying with a fresh process "
                f"in {fmt_duration(self.cfg['retry_delay_seconds'])}")
            time.sleep(self.cfg["retry_delay_seconds"])
            note = self.retry_note(step, reason, run)
        return EXIT_STOPPED  # not reached

    def resume_note(self, ticket: Ticket, step: str, entry: Dict[str, Any]) -> str:
        stop = entry.get("last_stop") or {}
        if entry.get("running"):
            lead = f"A previous {step} run of this ticket was interrupted before it finished."
        elif stop.get("step") == step:
            lead = f"A previous {step} run of this ticket was stopped by the runner: {stop.get('reason')}."
        else:
            return ""
        if step == "implement" and self.git.head() != entry["base"]:
            lead += (f" Commits since {entry['base'][:10]} may hold its partial work: read them "
                     f"and continue from there instead of starting over.")
        if self.git.tracked_changes(self.feature.dir):
            lead += " The uncommitted changes in the working tree are its unfinished edits."
        return lead

    def retry_note(self, step: str, reason: str, run: StepRun) -> str:
        excerpt = self.extract_report(run.final)[-1500:] if run.final else ""
        note = (f"A previous {step} attempt of this ticket failed the runner's check: {reason}. "
                f"Find out why and fix it; commits since the base may hold that attempt's work.")
        if excerpt:
            note += f"\nIts final report was:\n{excerpt}"
        return note

    def finish(self) -> int:
        left = self.feature.open_tickets()
        self.summary()
        if not left:
            self.event("all_done")
            self.notify(f"All tickets of {self.feature.dir.name} are done.")
            return EXIT_OK
        self.event("nothing_runnable", open=[t.name for t in left])
        self.notify(f"No runnable ticket left in {self.feature.dir.name}; {len(left)} open.")
        print_open(self.feature, left)
        return EXIT_BLOCKED

    def summary(self) -> None:
        if not self.done_this_run:
            return
        say("\nDone in this run:")
        for name, seconds, commits in self.done_this_run:
            say(f"  {name}: {fmt_duration(seconds)}, {commits} commit(s)")

    def interrupt(self) -> None:
        if self.proc is not None:
            kill_tree(self.proc)


def print_open(feature: Feature, tickets: List[Ticket]) -> None:
    index = feature.by_number()
    say("open:")
    for t in tickets:
        why = feature.waiting_on(t, index)
        extra = f", waiting on {', '.join(why)}" if why and t.status() == RUNNABLE else ""
        say(f"  {t.name} - {t.status() or 'no status'}{extra}")


# ---------------------------------------------------------------------------
# Commands


def cmd_status(feature: Feature) -> int:
    state_path = feature.runs / "state.json"
    state = State(state_path) if state_path.exists() else None
    index = feature.by_number()
    tickets = feature.tickets()
    say(f"{feature.dir}")
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
        entry = (state.data["tickets"].get(t.name) if state else None) or {}
        if entry.get("phase") in ("implement", "review"):
            line += f"\n        runner: at {entry['phase']}" + (" (interrupted)" if entry.get("running") else "")
            if entry.get("last_stop"):
                line += f", stopped {entry['last_stop'].get('time')}: {entry['last_stop'].get('reason', '').splitlines()[0]}"
        say(line)
        if len(index.get(t.number, [])) > 1 and t is index[t.number][0]:
            say(f"      warning: {len(index[t.number])} tickets share number {t.number:02d}")
    nxt = feature.next_ticket()
    say(f"next: {nxt.name if nxt else 'none runnable'}")
    done = sum(1 for t in tickets if t.status() == DONE)
    say(f"done: {done}/{len(tickets)}")
    report = feature.runs / "REPORT.md"
    if report.exists():
        say(f"report: {report}")
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
    """The effective config: where each value comes from, the agent's command line and,
    with --prompts, the exact prompts of the next ticket."""
    git = Git(feature.dir)
    path = find_config(feature.dir, git.repo, args.config)
    cfg = build_config(path, args)
    say("config lookup: --config, then " + ", then ".join(
        str(d / CONFIG_NAMES[0]) for d in (feature.dir, git.repo)))
    say(f"config file:   {path or 'none found, using defaults'}")
    say("")
    shown = ("agent", "model", "max_time", "retries", "retry_delay", "report_heading",
             "report_language", "desktop_notify", "notify_command", "preflight",
             "extra_instructions", "review_extra_instructions")
    for key in shown:
        value = cfg.get(key)
        say(f"  {key:<26} = {json.dumps(value, ensure_ascii=False):<40} # {cfg['sources'].get(key, 'default')}")
    say(f"  {'skills.implement':<26} = {json.dumps(cfg['skills']['implement'])}")
    say(f"  {'skills.review':<26} = {json.dumps(cfg['skills']['review'])}")
    agent = cfg["agent_spec"]
    argv = fill_command(cfg, "<prompt>", str(git.repo), str(feature.runs / "sessions" / "<step>"),
                        str(feature.runs / "sessions" / "<step>" / "final.md"))
    try:
        exe = resolve_executable(argv[0])
        found = " ".join(exe)
    except UsageError:
        found = "NOT FOUND on PATH"
    say("")
    say(f"agent command ({'prompt on stdin' if agent['prompt_via'] == 'stdin' else 'prompt as the last argument'}):")
    say("  " + (subprocess.list2cmdline(argv) if IS_WINDOWS else shlex.join(argv)))
    say(f"  executable: {found}")
    say(f"  skill call: {fill(agent['skill_format'], {'skill': cfg['skills']['implement'], 'args': '<ticket>'})}")
    if getattr(args, "prompts", False):
        ticket = feature.next_ticket() or (feature.tickets() or [None])[0]
        if ticket is None:
            say("\nno tickets to build prompts for")
        else:
            runner = Runner(feature, git, cfg, args)
            base = git.head()
            for step in ("implement", "review"):
                say(f"\n----- {step} prompt for {ticket.name} -----")
                say(runner.build_prompt(step, ticket, base, ""))
    return EXIT_OK if found != "NOT FOUND on PATH" else EXIT_USAGE


HELP_EPILOG = """examples:
  run-tickets init --agent claude            # .ticket-runner.toml in the repo root
  run-tickets config .scratch/my-feature      # what will run, with which settings
  run-tickets status .scratch/my-feature      # done / next / stuck
  run-tickets run    .scratch/my-feature      # run until done, blocked or stopped
  run-tickets run    .scratch/my-feature --once --agent omp --model opus

The feature dir can be given from anywhere; the runner works in the git repo that
contains it. Config: --config, else .ticket-runner.toml in the feature dir, else in
the repo root; command-line flags win over the file.
exit codes: 0 all done, 1 stopped, 2 usage/setup error, 3 open tickets left, 130 interrupted
"""


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run-tickets", description=__doc__.split("\n\n")[0],
                                epilog=HELP_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("feature", help="feature directory with issues/NN-*.md")
        sp.add_argument("--config", help="config file (default: .ticket-runner.toml in the feature dir or repo root)")

    def overrides(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--agent", help="omp | pi | claude | codex | a name from the config")
        sp.add_argument("--model", help="model name passed to the agent")
        sp.add_argument("--max-time", dest="max_time", help="per step, e.g. 3h, 90m (default 3h)")
        sp.add_argument("--retries", type=int, help="fresh-process retries of a failed step (default 1)")

    r = sub.add_parser("run", help="implement and review tickets until done, blocked or stopped")
    common(r)
    overrides(r)
    r.add_argument("--once", action="store_true", help="stop after one ticket")
    r.add_argument("--dry-run", action="store_true", help="same as the status command")
    r.add_argument("--adopt-dirty", action="store_true",
                   help="after a crash mid-step, let the agent continue with the uncommitted changes")

    s = sub.add_parser("status", help="show tickets, the next one and where the runner stopped")
    common(s)

    c = sub.add_parser("config", help="show the effective config and the agent command line")
    common(c)
    overrides(c)
    c.add_argument("--prompts", action="store_true", help="also print the prompts of the next ticket")

    i = sub.add_parser("init", help="write an example .ticket-runner.toml")
    i.add_argument("dir", nargs="?", help="where to write it (default: the repo root)")
    i.add_argument("--agent", choices=sorted(AGENT_PRESETS), help="agent to put in the file")

    # `run-tickets <feature> [--opts]` works as `run-tickets run <feature>`.
    if argv and argv[0] not in ("run", "status", "config", "init", "-h", "--help", "--version"):
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


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "init":
            return cmd_init(args.dir, args.agent)
        feature = Feature(Path(args.feature))
        if args.command == "status" or getattr(args, "dry_run", False):
            return cmd_status(feature)
        if args.command == "config":
            return cmd_config(feature, args)

        git = Git(feature.dir)
        git.head()
        cfg = build_config(find_config(feature.dir, git.repo, args.config), args)
        resolve_executable(fill(cfg["agent_spec"]["command"][0], {}))
        feature.runs.mkdir(parents=True, exist_ok=True)
        lock = Lock(git.git_dir / "run-tickets.lock", feature.dir)
        lock.acquire()
    except UsageError as e:
        warn(f"run-tickets: {e}")
        return EXIT_USAGE
    try:
        runner = Runner(feature, git, cfg, args)
    except UsageError as e:
        lock.release()
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
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
