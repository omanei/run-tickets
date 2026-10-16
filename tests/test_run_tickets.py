import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_tickets as rt  # noqa: E402

STUB = Path(__file__).resolve().parent / "stub_agent.py"


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def ticket(num, title, blocked="None", status="ready-for-agent", criteria=2):
    boxes = "\n".join(f"- [ ] criterion {i}" for i in range(1, criteria + 1))
    return (f"# {num}: {title}\n\n**What to build:** {title}.\n\n**Blocked by:** {blocked}\n\n"
            f"**Status:** {status}\n\n{boxes}\n")


class Env:
    def __init__(self, tmp: Path, tracked: bool, prompt_via: str):
        self.repo = tmp / "repo"
        self.feature = self.repo / "feat" / "F-1"
        (self.feature / "issues").mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "code.txt").write_text("start\n")
        (self.repo / "user.txt").write_text("user\n")
        if not tracked:
            (self.repo / ".gitignore").write_text("feat/\n")
        self.calls = tmp / "calls.jsonl"
        self.plan = tmp / "plan.json"
        self.pidfile = tmp / "stub.pid"
        cmd = [sys.executable, str(STUB), "{{model_args}}", "{{budget_args}}",
               "--session-dir", "{{session_dir}}", "--cwd", "{{repo}}"]
        if prompt_via == "arg":
            cmd.append("{{prompt}}")
        self.config = tmp / "runner.toml"
        self.agent_table = (
            "[agents.stub]\n"
            f"command = [{', '.join(json.dumps(c) for c in cmd)}]\n"
            f'prompt_via = "{prompt_via}"\n'
            'skill_format = "/skill:{{skill}} {{args}}"\n'
            f"export_command = [{json.dumps(sys.executable)}, {json.dumps(str(STUB))}, "
            "'--export', '{{session_file}}']\n")
        self.write_config()
        self.tracked = tracked

    def write_config(self, tables="", **extra):
        """The test config: the stub agent, no retries, no final review, plus `extra`
        top-level keys (JSON values are valid TOML for strings, numbers, bools, lists)."""
        keys = {"agent": "stub", "retries": 0, "retry_delay": "0", "desktop_notify": False,
                "final_review": False, "log_retention": "0", "log_max_mb": 0, **extra}
        top = "".join(f"{k} = {json.dumps(v)}\n" for k, v in keys.items())
        self.config.write_text(top + self.agent_table + tables, encoding="utf-8")

    def add(self, name, text):
        (self.feature / "issues" / f"{name}.md").write_text(text, encoding="utf-8")

    def commit_all(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def set_plan(self, plan, monkeypatch=None):
        self.plan.write_text(json.dumps(plan))

    def env(self):
        return {"STUB_CALLS": str(self.calls), "STUB_PLAN": str(self.plan),
                "STUB_PIDFILE": str(self.pidfile)}

    def run(self, *extra, config=None):
        return rt.main(["run", str(self.feature), "--config", str(config or self.config), *extra])

    def calls_list(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def keys(self):
        return [c["key"] for c in self.calls_list()]

    def status(self, name):
        return rt.Ticket(self.feature / "issues" / f"{name}.md").status()

    def state(self):
        return json.loads((self.feature / "runs" / "state.json").read_text())["tickets"]

    def report(self):
        return (self.feature / "runs" / "REPORT.md").read_text(encoding="utf-8")


@pytest.fixture(params=[(False, "arg"), (True, "stdin")], ids=["ignored-arg", "tracked-stdin"])
def env(request, tmp_path, monkeypatch):
    e = Env(tmp_path, *request.param)
    e.set_plan({})
    for k, v in e.env().items():
        monkeypatch.setenv(k, v)
    return e


@pytest.fixture
def basic(env):
    # Numbers out of blocker order on purpose: 01 -> 03 -> 02 -> 04.
    env.add("01-first", ticket("01", "first"))
    env.add("02-second", ticket("02", "second", blocked="03"))
    env.add("03-third", ticket("03", "third", blocked="#01"))
    env.add("04-fourth", ticket("04", "fourth", blocked="02, 03 (see PROJ-1234)"))
    env.commit_all()
    return env


# --- parsing -------------------------------------------------------------


@pytest.mark.parametrize("line,status", [
    ("**Status:** ready-for-agent", "ready-for-agent"),
    ("**Status**: done", "done"),
    ("Status: needs-info", "needs-info"),
    ("- **Status:** Done", "done"),
    ("Status of the export is unknown", ""),
])
def test_status_line(tmp_path, line, status):
    p = tmp_path / "01-x.md"
    p.write_text(f"# 01\n\n{line}\n")
    assert rt.Ticket(p).status() == status


@pytest.mark.parametrize("line,blockers", [
    ("**Blocked by:** None", []),
    ("**Blocked by:** 04", [4]),
    ("Blocked by: #2, 03-add-api", [2, 3]),
    ("**Blocked by**: 05 (needs PROJ-1234)", [5]),
])
def test_blocked_by(tmp_path, line, blockers):
    p = tmp_path / "07-x.md"
    p.write_text(f"# 07\n\n{line}\n")
    assert rt.Ticket(p).blockers() == blockers


def test_blocked_by_heading(tmp_path):
    p = tmp_path / "07-x.md"
    p.write_text("# 07\n\n## Blocked by\n\n- Blocked by #3\n- Blocked by #05\n\n## Notes\n\nsee 09\n")
    assert rt.Ticket(p).blockers() == [3, 5]
    p.write_text("# 07\n\n## Blocked by\n\nNone - can start immediately\n")
    assert rt.Ticket(p).blockers() == []


def test_blocked_by_titles(tmp_path):
    feature = tmp_path / "f"
    (feature / "issues").mkdir(parents=True)
    (feature / "issues" / "01-create.md").write_text(ticket("01", "User signs up"))
    (feature / "issues" / "02-delete.md").write_text(
        ticket("02", "User signs in", blocked="User signs up"))
    (feature / "issues" / "03-other.md").write_text(
        ticket("03", "Other", blocked="None (can start immediately)"))
    (feature / "issues" / "04-odd.md").write_text(ticket("04", "Odd", blocked="the login work"))
    f = rt.Feature(feature)
    t = {x.name: x for x in f.tickets()}
    assert f.blockers_of(t["02-delete"]) == ([1], "")
    assert f.blockers_of(t["03-other"]) == ([], "")
    assert f.blockers_of(t["04-odd"]) == ([], "the login work")
    assert f.waiting_on(t["04-odd"], f.by_number()) == ["unrecognised 'Blocked by: the login work'"]
    # 02 waits for 01 even when 01 is stuck and skipped.
    p = feature / "issues" / "01-create.md"
    p.write_text(p.read_text().replace("ready-for-agent", "needs-info"))
    assert f.next_ticket().name == "03-other"


def test_duration():
    assert rt.parse_duration("3h") == 10800
    assert rt.parse_duration("1h30m") == 5400
    assert rt.parse_duration("90") == 90
    with pytest.raises(rt.UsageError):
        rt.parse_duration("3 hours")


# --- full run ------------------------------------------------------------


def test_full_run_in_blocker_order(basic):
    assert basic.run() == rt.EXIT_OK
    assert basic.keys() == [
        "01-first:implement", "01-first:review", "03-third:implement", "03-third:review",
        "02-second:implement", "02-second:review", "04-fourth:implement", "04-fourth:review"]
    for name in ("01-first", "02-second", "03-third", "04-fourth"):
        assert basic.status(name) == "done"
        assert basic.state()[name]["phase"] == "done"
    report = basic.report()
    assert report.count(": ok - ticket done") == 4
    assert "> - step: review 04-fourth (ok)" in report
    assert list((basic.feature / "runs").glob("01-first-implement-*.html"))
    review = basic.calls_list()[1]["prompt"]
    base = basic.state()["01-first"]["base"]
    assert review.startswith(f"/skill:code-review since {base} for the ticket ")
    assert not (Path(git(basic.repo, "rev-parse", "--absolute-git-dir")) / "run-tickets.lock").exists()


def test_once_and_status(basic, capsys):
    assert basic.run("--once") == rt.EXIT_OK
    assert basic.keys() == ["01-first:implement", "01-first:review"]
    capsys.readouterr()
    assert rt.main(["status", str(basic.feature)]) == 0
    out = capsys.readouterr().out
    assert "[x] 01-first - done" in out
    assert "next: 03-third" in out
    assert "02-second - ready-for-agent, 0/2 criteria, waiting on 03-third (ready-for-agent)" in out


def test_model_is_passed(basic):
    assert basic.run("--once", "--model", "big-one") == 0
    assert basic.calls_list()[0]["model"] == "big-one"


# --- stops ---------------------------------------------------------------


@pytest.mark.parametrize("key,action,reason", [
    ("01-first:implement", "dirty", "uncommitted changes were left"),
    ("01-first:implement", "exit1", "the agent exited with 1"),
    ("01-first:review", "unticked", "2 acceptance criteria are not ticked"),
    ("01-first:review", "noop", "the ticket status is 'ready-for-agent', not done"),
])
def test_failed_check_stops(basic, key, action, reason):
    basic.set_plan({key: [action]})
    assert basic.run() == rt.EXIT_STOPPED
    assert f"STOPPED - {reason}" in basic.report()
    assert basic.state()["01-first"]["last_stop"]["reason"] == reason
    assert "03-third:implement" not in basic.keys()


def test_retry_then_success(basic):
    basic.set_plan({"01-first:implement": ["exit1", "ok"]})
    assert basic.run("--once", "--retries", "1") == 0
    assert basic.keys() == ["01-first:implement", "01-first:implement", "01-first:review"]
    second = basic.calls_list()[1]["prompt"]
    assert "failed the runner's check: the agent exited with 1" in second
    assert "FAILED, retrying (1/1) - the agent exited with 1" in basic.report()


def test_stash_change_is_never_retried(basic):
    (basic.repo / "user.txt").write_text("user's own work\n")
    git(basic.repo, "stash", "push", "-m", "user work")
    basic.set_plan({"01-first:implement": ["stash-pop"]})
    assert basic.run("--retries", "3") == rt.EXIT_STOPPED
    assert basic.keys() == ["01-first:implement"]
    assert "the git stash list changed" in basic.report()


def test_needs_info_stops_and_dependents_wait(basic):
    basic.set_plan({"01-first:implement": ["needs-info"]})
    assert basic.run("--retries", "2") == rt.EXIT_STOPPED
    assert basic.keys() == ["01-first:implement"]
    assert "the ticket status is now 'needs-info'" in basic.report()
    # Nothing else is runnable: everything depends on 01.
    assert basic.run() == rt.EXIT_BLOCKED


def test_timeout_kills_the_agent(basic):
    basic.set_plan({"01-first:implement": ["hang"]})
    started = time.monotonic()
    assert basic.run("--max-time", "3s") == rt.EXIT_STOPPED
    assert time.monotonic() - started < 60
    assert "ran out of max_time (3s)" in basic.report()
    pid = int(basic.pidfile.read_text())
    time.sleep(1)
    assert not rt.pid_alive(pid)


# --- resume --------------------------------------------------------------


def test_resume_at_review_after_stop(basic):
    basic.set_plan({"01-first:review": ["unticked", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    base = basic.state()["01-first"]["base"]
    # The agent set done but left boxes; a human re-opens the ticket.
    p = basic.feature / "issues" / "01-first.md"
    p.write_text(p.read_text().replace("**Status:** done", "**Status:** ready-for-agent"))
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:review", "01-first:review"]
    resumed = basic.calls_list()[2]["prompt"]
    assert f"since {base} " in resumed
    assert "stopped by the runner: 2 acceptance criteria are not ticked" in resumed


def test_resume_implement_keeps_base(basic):
    basic.set_plan({"01-first:implement": ["exit1", "ok"]})
    base = git(basic.repo, "rev-parse", "HEAD")
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.run("--once") == 0
    assert basic.state()["01-first"]["base"] == base


def test_rewritten_history_starts_over(basic):
    basic.set_plan({"01-first:review": ["noop", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    old_base = basic.state()["01-first"]["base"]
    git(basic.repo, "reset", "-q", "--hard", old_base)
    git(basic.repo, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    assert not rt.Git(basic.repo).is_ancestor(old_base)
    assert basic.run("--once") == 0
    assert basic.keys()[-2:] == ["01-first:implement", "01-first:review"]


def test_dirty_tree_refuses_to_start(basic):
    (basic.repo / "user.txt").write_text("edited\n")
    assert basic.run() == rt.EXIT_USAGE
    assert basic.keys() == []


def test_adopt_dirty_after_stop(basic):
    basic.set_plan({"01-first:implement": ["dirty", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.run("--once") == rt.EXIT_USAGE
    assert basic.run("--once", "--adopt-dirty") == 0
    assert "uncommitted changes in the working tree are its unfinished edits" in basic.calls_list()[1]["prompt"]


def test_recovers_step_finished_while_runner_was_dead(basic):
    assert basic.run("--once") == 0
    # Simulate: the runner died after the agent finished 03's review but before checking it.
    runner_state = basic.feature / "runs" / "state.json"
    data = json.loads(runner_state.read_text())
    head = git(basic.repo, "rev-parse", "HEAD")
    (basic.repo / "code.txt").write_text("03 work\n")
    git(basic.repo, "commit", "-qam", "03 work")
    p = basic.feature / "issues" / "03-third.md"
    p.write_text(p.read_text().replace("- [ ]", "- [x]").replace("ready-for-agent", "done"))
    data["tickets"]["03-third"] = {"phase": "review", "running": True, "base": head,
                                    "branch": "main", "stash": ""}
    runner_state.write_text(json.dumps(data))
    assert basic.run("--once") == 0
    assert "03-third:review" not in basic.keys()
    assert basic.state()["03-third"]["phase"] == "done"
    assert "checked after an interrupted run" in basic.report()
    assert basic.keys()[-2:] == ["02-second:implement", "02-second:review"]


# --- lock ----------------------------------------------------------------


def lock_path(env):
    return Path(git(env.repo, "rev-parse", "--absolute-git-dir")) / "run-tickets.lock"


def test_live_lock_blocks(basic):
    lock_path(basic).write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
    assert basic.run() == rt.EXIT_USAGE
    assert basic.keys() == []


def test_stale_lock_is_taken_over(basic):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    lock_path(basic).write_text(json.dumps({"pid": p.pid, "host": socket.gethostname()}))
    assert basic.run("--once") == 0


# --- a real crash: the runner process is killed mid-step ------------------


def test_killed_runner_resumes(basic):
    basic.set_plan({"01-first:implement": ["commit-then-hang", "ok"]})
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run_tickets.py"), "run", str(basic.feature),
         "--config", str(basic.config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ))
    deadline = time.monotonic() + 60
    while not basic.pidfile.exists():
        assert time.monotonic() < deadline, "the stub never started"
        time.sleep(0.2)
    proc.kill()  # SIGKILL / TerminateProcess: no cleanup at all
    proc.wait()
    stub_pid = int(basic.pidfile.read_text())
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(stub_pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(stub_pid, signal.SIGKILL)
    time.sleep(0.5)

    assert lock_path(basic).exists()
    entry = basic.state()["01-first"]
    assert entry["running"] is True and entry["phase"] == "implement"

    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:implement", "01-first:review"]
    resumed = basic.calls_list()[1]["prompt"]
    assert "was interrupted before it finished" in resumed
    assert "may hold its partial work" in resumed
    assert basic.state()["01-first"]["base"] == entry["base"]


# --- agents --------------------------------------------------------------


def test_claude_stream_output(basic, monkeypatch, capsys):
    monkeypatch.setenv("STUB_STREAM", "1")
    text = basic.config.read_text()
    basic.config.write_text(text.replace('skill_format', 'output = "claude-stream-json"\nskill_format'))
    assert basic.run("--once") == 0
    out = capsys.readouterr().out
    assert "  · Bash: git commit" in out
    assert "> - step: review 01-first (ok)" in basic.report()
    assert '"type": "result"' not in basic.report()


def _argv(tmp_path, monkeypatch, agent, model=""):
    monkeypatch.setattr(rt.shutil, "which", lambda name: f"/bin/{name}")
    repo = tmp_path / "r"
    (repo / "f" / "issues").mkdir(parents=True)
    git(repo, "init", "-q")
    args = rt.parse_args(["run", str(repo / "f"), "--agent", agent] + (["--model", model] if model else []))
    cfg = rt.build_config(None, args)
    runner = rt.Runner(rt.Feature(repo / "f"), rt.Git(repo), cfg, args)
    return runner.agent_argv("implement", "PROMPT", Path("/s"), Path("/s/final.md")), str(runner.git.repo)


def test_omp_argv(tmp_path, monkeypatch):
    argv, repo = _argv(tmp_path, monkeypatch, "omp", "opus")
    assert argv == ["/bin/omp", "--model", "opus", "-p", "--auto-approve", "--session-dir",
                    str(Path("/s")), "--cwd", repo, "PROMPT"]


def test_claude_argv_reads_prompt_from_stdin(tmp_path, monkeypatch):
    argv, _ = _argv(tmp_path, monkeypatch, "claude")
    assert "PROMPT" not in argv
    assert argv[:2] == ["/bin/claude", "-p"]


def test_unknown_agent(tmp_path):
    args = rt.parse_args(["run", str(tmp_path), "--agent", "nope"])
    with pytest.raises(rt.UsageError):
        rt.build_config(None, args)


def test_init_writes_a_valid_config(tmp_path):
    assert rt.main(["init", str(tmp_path), "--agent", "claude"]) == 0
    path = tmp_path / ".ticket-runner.toml"
    assert rt.load_toml(path) == {"agent": "claude"}
    # The commented top-level defaults are valid TOML and match the real defaults.
    top = path.read_text(encoding="utf-8").split("# [skills]")[0]
    names = ("model", "max_time", "retries", "retry_delay", "desktop_notify",
             "report_heading", "report_language", "notify_command")
    keys = [l[2:] for l in top.splitlines() if l.startswith(tuple(f"# {n} = " for n in names))]
    uncommented = tmp_path / "uncommented.toml"
    uncommented.write_text("\n".join(keys), encoding="utf-8")
    values = rt.load_toml(uncommented)
    for key in names[:-1]:
        assert values[key] == rt.DEFAULT_CONFIG[key], key
    assert "$RUN_TICKETS_MESSAGE" in values["notify_command"]
    assert rt.main(["init", str(tmp_path)]) == rt.EXIT_USAGE


def test_config_command_shows_sources_and_command(basic, capsys):
    assert rt.main(["config", str(basic.feature), "--config", str(basic.config),
                    "--model", "m1", "--prompts"]) == 0
    out = capsys.readouterr().out
    assert '"stub"' in out and "# config file" in out
    assert '"m1"' in out and "# command line" in out
    assert "--model m1" in out
    assert "----- implement prompt for 01-first -----" in out
    assert "/skill:implement " in out


# --- the runner's own test gate --------------------------------------------

PY = sys.executable if " " not in sys.executable else f'"{sys.executable}"'
TESTS = PY + ' -c "import os,sys; sys.exit(1 if os.path.exists(\'BROKEN\') else 0)"'


def test_failing_tests_fail_the_step_and_feed_the_retry(basic):
    basic.write_config(test_command=TESTS, retries=1)
    basic.set_plan({"01-first:implement": ["break-tests", "fix-tests"]})
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:implement", "01-first:review"]
    first, second = basic.calls_list()[0]["prompt"], basic.calls_list()[1]["prompt"]
    assert "the runner itself runs" in first
    assert "failed the runner's check: the tests failed" in second
    report = basic.report()
    assert "- tests: FAILED" in report and "- tests: passed" in report


def test_failing_baseline_stops_before_any_agent(basic):
    (basic.repo / "BROKEN").write_text("x\n")
    git(basic.repo, "add", "BROKEN")
    git(basic.repo, "commit", "-qm", "broken base")
    basic.write_config(test_command=TESTS)
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.keys() == []
    assert "before the agent started on this ticket" in basic.report()
    git(basic.repo, "rm", "-q", "BROKEN")
    git(basic.repo, "commit", "-qm", "fixed base")
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:review"]


# --- provider limits -------------------------------------------------------


def test_provider_limit_waits_without_spending_a_retry(basic):
    basic.write_config(limit_default_wait="1s")
    basic.set_plan({"01-first:implement": ["limit", "ok"]})
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:implement", "01-first:review"]
    assert "PROVIDER LIMIT - waiting" in basic.report()
    assert "cut off by the provider's usage limit" in basic.calls_list()[1]["prompt"]


def test_provider_limit_past_max_wait_stops(basic):
    basic.write_config(limit_max_wait="0")
    basic.set_plan({"01-first:implement": ["limit"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert "did not lift within limit_max_wait" in basic.report()


def test_parse_limit_wait():
    import datetime as dt
    at = dt.datetime(2026, 10, 16, 1, 0, tzinfo=dt.timezone.utc)
    ts = int(at.timestamp())
    assert rt.parse_limit_wait(f"Claude AI usage limit reached|{ts + 3600}", 5, at) == 3660
    assert rt.parse_limit_wait("You've hit your limit · resets 3am (UTC)", 5, at) == 2 * 3600 + 120
    assert rt.parse_limit_wait("resets at 12:30am", 5, at) == 23 * 3600 + 30 * 60 + 120
    assert rt.parse_limit_wait("429: try again in 15 minutes", 5, at) == 900
    assert rt.parse_limit_wait("rate limit exceeded", 1800, at) == 1800


# --- git flow: branch, worktree, push, pull request ------------------------


def test_worktree_leaves_only_a_local_branch(basic, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(basic.repo, "remote", "add", "origin", str(remote))
    basic.write_config(worktree=True, branch="feat/{{feature}}", final_review=True)
    main_head = git(basic.repo, "rev-parse", "HEAD")
    work = basic.repo.parent / "repo-worktrees" / "F-1"

    assert basic.run("--once") == 0
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "feat/F-1"
    assert {c["cwd"] for c in basic.calls_list()} == {str(work)}
    assert basic.run() == rt.EXIT_OK

    # The user's checkout is untouched.
    assert git(basic.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(basic.repo, "rev-parse", "HEAD") == main_head
    assert (basic.repo / "code.txt").read_text() == "start\n"
    # The worktree folder is gone; the branch with all the work stays in the repository.
    assert not work.exists()
    assert str(work) not in git(basic.repo, "worktree", "list")
    log = git(basic.repo, "log", "--format=%s", "main..feat/F-1")
    assert "implement 01-first" in log and "final review fixes" in log
    assert "final polish" in git(basic.repo, "show", "feat/F-1:code.txt")
    assert all(basic.status(n) == "done" for n in ("01-first", "02-second", "03-third", "04-fourth"))
    # Nothing was pushed.
    assert git(tmp_path, "--git-dir", str(remote), "for-each-ref") == ""
    assert "feat/F-1" in (basic.feature / "runs" / "SUMMARY.md").read_text()
    # A later run on the finished feature changes nothing.
    calls = len(basic.keys())
    assert basic.run() == rt.EXIT_OK
    assert len(basic.keys()) == calls and not work.exists()


def test_worktree_with_uncommitted_files_is_kept(basic):
    basic.write_config(worktree=True, worktree_setup=[PY + ' -c "open(\'notes.txt\', \'w\').write(\'x\')"'])
    assert basic.run() == rt.EXIT_OK
    work = basic.repo.parent / "repo-worktrees" / "F-1"
    assert (work / "notes.txt").exists()
    assert "WORKTREE KEPT" in basic.report()


def test_worktree_setup_runs_once_and_repeats_until_it_passes(basic, tmp_path):
    (basic.repo / ".env").write_text("SECRET=1\n")  # untracked: not in a new worktree
    flag = tmp_path / "setup-ok"
    setup = PY + ' -c "import os,shutil,sys; sys.exit(0 if os.path.exists(sys.argv[1]) else 3)" ' + json.dumps(str(flag))
    copy = PY + ' -c "import os,shutil; shutil.copy(os.path.join(os.environ[\'RUN_TICKETS_MAIN_REPO\'], \'.env\'), \'.env\')"'
    basic.write_config(worktree=True, worktree_setup=[copy, setup])
    assert basic.run("--once") == rt.EXIT_USAGE
    assert basic.keys() == []
    flag.write_text("")
    assert basic.run("--once") == 0
    work = basic.repo.parent / "repo-worktrees" / "F-1"
    assert (work / ".env").read_text() == "SECRET=1\n"
    flag.unlink()  # done once: not run again for the existing worktree
    assert basic.run("--once") == 0


def test_retry_after_review_left_criteria_unticked(basic):
    basic.set_plan({"01-first:review": ["unticked", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.status("01-first") == "done"
    assert rt.main(["retry", str(basic.feature), "01"]) == 0
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:review", "01-first:review"]


def test_branch_without_worktree(basic):
    basic.write_config(branch="feat/{{feature}}")
    assert basic.run("--once") == 0
    assert git(basic.repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/F-1"
    data = json.loads((basic.feature / "runs" / "state.json").read_text())
    assert data["git"]["base_branch"] == "main"


# --- the final review ------------------------------------------------------


def test_final_review_runs_once_on_the_whole_feature(basic):
    base = git(basic.repo, "rev-parse", "HEAD")
    basic.write_config(final_review=True)
    assert basic.run() == 0
    assert basic.keys()[-1] == "final:final" and basic.keys().count("final:final") == 1
    final = basic.calls_list()[-1]["prompt"]
    assert f"since {base} for the whole feature" in final
    assert "01-first, 02-second, 03-third, 04-fourth" in final
    assert basic.run() == 0
    assert basic.keys().count("final:final") == 1


def test_final_review_stop_and_resume(basic, capsys):
    basic.write_config(final_review=True)
    basic.set_plan({"final:final": ["exit1", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    capsys.readouterr()
    rt.main(["status", str(basic.feature)])
    assert "final review: final, stopped" in capsys.readouterr().out
    assert basic.run() == 0
    assert "stopped by the runner: the agent exited with 1" in basic.calls_list()[-1]["prompt"]


# --- retry / reset / skip --------------------------------------------------


def test_retry_reset_skip(basic):
    basic.set_plan({"01-first:implement": ["needs-info", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.status("01-first") == "needs-info"
    assert rt.main(["retry", str(basic.feature), "1"]) == 0
    assert basic.status("01-first") == "ready-for-agent"
    assert "last_stop" not in basic.state()["01-first"]
    assert basic.run("--once") == 0
    assert basic.status("01-first") == "done"

    assert rt.main(["reset", str(basic.feature), "01-first"]) == 0
    t = rt.Ticket(basic.feature / "issues" / "01-first.md")
    assert t.status() == "ready-for-agent" and t.unchecked() == 2
    assert "01-first" not in basic.state()

    assert rt.main(["skip", str(basic.feature), "01", "--reason", "covered elsewhere"]) == 0
    assert t.status() == "wontfix"
    assert "skipped with run-tickets: covered elsewhere" in t.text()
    assert basic.run() == rt.EXIT_BLOCKED  # everything else waits for 01
    assert rt.main(["retry", str(basic.feature), "99"]) == rt.EXIT_USAGE


# --- per-step agents, guards, budget ----------------------------------------


def test_another_agent_and_model_for_the_review(basic):
    stub2 = basic.agent_table.replace("[agents.stub]", "[agents.stub2]").replace(
        "{{skill}} {{args}}", "{{skill}} {{args}} [stub2]")
    basic.write_config(tables=stub2 + '[steps.review]\nagent = "stub2"\nmodel = "reviewer"\n')
    assert basic.run("--once") == 0
    implement, review = basic.calls_list()
    assert implement["model"] is None and review["model"] == "reviewer"
    assert review["prompt"].splitlines()[0].endswith("[stub2]")
    assert not implement["prompt"].splitlines()[0].endswith("[stub2]")


def test_protected_paths_stop_without_retry(basic):
    basic.write_config(protected=["migrations/"], retries=2)
    basic.set_plan({"01-first:implement": ["protected"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.keys() == ["01-first:implement"]
    assert "protected paths were changed: migrations/001.sql" in basic.report()


def test_changed_test_constant_is_flagged(basic):
    (basic.repo / "tests").mkdir()
    (basic.repo / "tests" / "test_budget.py").write_text("BUDGET = 100\n")
    git(basic.repo, "add", "tests")
    git(basic.repo, "commit", "-qm", "budget test")
    basic.set_plan({"01-first:implement": ["threshold"]})
    assert basic.run("--once") == 0
    assert "`tests/test_budget.py`: `BUDGET = 100  ->  BUDGET = 200`" in basic.report()


def test_budget_per_run_and_per_step(basic, monkeypatch):
    monkeypatch.setenv("STUB_STREAM", "1")
    basic.agent_table = basic.agent_table.replace(
        "skill_format", 'output = "claude-stream-json"\nbudget_args = ["--max-budget-usd", "{{budget}}"]\nskill_format')
    basic.write_config(max_cost=0.9, max_step_cost=2.5)
    assert basic.run() == rt.EXIT_STOPPED
    assert basic.keys() == ["01-first:implement", "01-first:review"]
    assert basic.calls_list()[0]["budget"] == "2.5"
    assert "the run's budget is spent: $1.00 of max_cost $0.90" in basic.report()
    assert basic.state()["01-first"]["cost"] == 1.0
    assert "$1.00" in (basic.feature / "runs" / "SUMMARY.md").read_text()


# --- pause, summary, housekeeping, queue, status, config ---------------------


def test_pause_after_and_summary(basic):
    basic.write_config(pause_after=["03"])
    assert basic.run() == rt.EXIT_PAUSED
    assert basic.keys()[-1] == "03-third:review"
    summary = (basic.feature / "runs" / "SUMMARY.md").read_text()
    assert "**paused**" in summary and "tickets done: 2/4" in summary
    assert "| 01-first | done | done | 2 |" in summary
    assert basic.run() == 0
    assert basic.keys()[-1] == "04-fourth:review"


def test_old_logs_are_rotated(basic):
    runs = basic.feature / "runs"
    (runs / "sessions" / "old").mkdir(parents=True)
    old_log, keep = runs / "old-implement.log", runs / "events.jsonl"
    old_log.write_text("x")
    keep.write_text("")
    past = time.time() - 40 * 86400
    for p in (old_log, runs / "sessions" / "old", keep):
        os.utime(p, (past, past))
    basic.write_config(log_retention="30d")
    assert basic.run("--once") == 0
    assert not old_log.exists() and not (runs / "sessions" / "old").exists()
    assert keep.exists() and (runs / "REPORT.md").exists()


def test_queue_of_features(env):
    env.add("01-a", ticket("01", "a"))
    second = env.repo / "feat" / "F-2"
    (second / "issues").mkdir(parents=True)
    (second / "issues" / "01-b.md").write_text(ticket("01", "b"), encoding="utf-8")
    env.commit_all()
    env.set_plan({"01-a:implement": ["exit1"]})
    both = ["run", str(env.feature), str(second), "--config", str(env.config)]
    assert rt.main(both) == rt.EXIT_STOPPED
    assert "01-b:implement" not in env.keys()
    assert rt.main(both + ["--keep-going"]) == rt.EXIT_STOPPED
    assert env.keys()[-2:] == ["01-b:implement", "01-b:review"]


def test_status_shows_the_running_step(basic, capsys):
    runs = basic.feature / "runs"
    runs.mkdir()
    log = runs / "01-first-review-x.log"
    log.write_text("reading the diff\nrunning the tests\n")
    (runs / "state.json").write_text(json.dumps({"tickets": {}, "current": {
        "ticket": "01-first", "step": "review", "attempt": 1, "started": rt.iso(),
        "log": str(log), "stream": False, "agent": "stub"}}))
    (runs / ".lock").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
    assert rt.main(["status", str(basic.feature)]) == 0
    out = capsys.readouterr().out
    assert "running now: 01-first review (attempt 1, stub)" in out
    assert "last: running the tests" in out


def test_unknown_config_key_is_an_error(basic):
    basic.write_config(retrys=3)
    assert basic.run() == rt.EXIT_USAGE


# --- tickets without commits: the verify step --------------------------------


def test_implement_without_commits_is_verified_not_reviewed(basic):
    basic.set_plan({"01-first:implement": ["noop"]})
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:verify"]
    verify = basic.calls_list()[1]["prompt"]
    assert verify.startswith(f"Verify the ticket {basic.feature / 'issues' / '01-first.md'}. "
                             f"Its implement step made no commits")
    assert "The implement step's word is not evidence." in verify
    assert basic.status("01-first") == "done"
    report = basic.report()
    assert "01-first - implement: ok - no commits, the verify step checks the result" in report
    assert "01-first - verify: ok - ticket done" in report


def test_verify_is_checked_like_a_review_and_resumes(basic):
    basic.set_plan({"01-first:implement": ["noop"], "01-first:verify": ["unticked", "ok"]})
    assert basic.run() == rt.EXIT_STOPPED
    assert "01-first - verify: STOPPED - 2 acceptance criteria are not ticked" in basic.report()
    assert basic.state()["01-first"]["phase"] == "verify"
    assert rt.main(["retry", str(basic.feature), "01"]) == 0
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:verify", "01-first:verify"]


def test_report_ticket_is_verified_even_with_commits(basic):
    p = basic.feature / "issues" / "01-first.md"
    p.write_text(p.read_text().replace("**Status:**", "**Kind:** report\n\n**Status:**"))
    assert rt.Ticket(p).kind() == "report"
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:implement", "01-first:verify"]
    assert "This is a report ticket (Kind: report)" in basic.calls_list()[0]["prompt"]
    assert "(it is a report ticket)" in basic.calls_list()[1]["prompt"]


def test_verify_step_takes_the_review_agent_settings(basic):
    basic.write_config(tables='[steps.review]\nmodel = "reviewer"\n')
    basic.set_plan({"01-first:implement": ["noop"]})
    assert basic.run("--once") == 0
    assert [c["model"] for c in basic.calls_list()] == [None, "reviewer"]


# --- tickets the agent creates ------------------------------------------------


def test_tickets_created_by_the_agent_wait_for_triage(basic):
    basic.set_plan({"01-first:implement": ["new-ticket"]})
    assert basic.run() == rt.EXIT_BLOCKED  # the feature waits for the new ticket's triage
    assert "05-follow-up:implement" not in basic.keys()
    assert basic.status("05-follow-up") == "needs-triage"
    assert all(basic.status(n) == "done" for n in ("01-first", "02-second", "03-third", "04-fourth"))
    assert "01-first - new tickets: 05-follow-up" in basic.report()
    assert "Set to needs-triage by the runner: 05-follow-up (ready-for-agent)" in basic.report()
    prompt = basic.calls_list()[0]["prompt"]
    assert "give each one the status line `**Status:** needs-triage`" in prompt


def test_new_tickets_of_an_interrupted_step_are_triaged_on_restart(basic):
    runs = basic.feature / "runs"
    runs.mkdir()
    head = git(basic.repo, "rev-parse", "HEAD")
    (runs / "state.json").write_text(json.dumps({"tickets": {"01-first": {
        "phase": "implement", "running": True, "base": head, "branch": "main", "stash": "",
        "tickets_before": ["01-first", "02-second", "03-third", "04-fourth"]}}}))
    basic.add("05-follow-up", ticket("05", "follow-up"))
    assert basic.run("--once") == 0
    assert basic.status("05-follow-up") == "needs-triage"
    assert basic.keys() == ["01-first:implement", "01-first:review"]


# --- adopt: taking over a feature another runner started ----------------------


def adopt(env, *args):
    return rt.main(["adopt", str(env.feature), *args])


def test_adopt_resumes_a_ticket_at_review_with_its_base(basic):
    base = git(basic.repo, "rev-parse", "HEAD")
    (basic.repo / "code.txt").write_text("done by another runner\n")
    git(basic.repo, "commit", "-qam", "implement 01 elsewhere")
    assert adopt(basic, "--feature-base", base[:8], "--resume", f"01:review:{base[:8]}") == 0
    data = json.loads((basic.feature / "runs" / "state.json").read_text())
    assert data["feature_base"] == base
    assert data["tickets"]["01-first"]["phase"] == "review"
    assert data["tickets"]["01-first"]["base"] == base
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:review"]
    assert f"since {base} for the ticket" in basic.calls_list()[0]["prompt"]


def test_adopted_review_without_commits_becomes_verify(basic):
    p = basic.feature / "issues" / "01-first.md"
    p.write_text(p.read_text().replace("ready-for-agent", "needs-info"))
    head = git(basic.repo, "rev-parse", "HEAD")
    assert adopt(basic, "--resume", f"1:review:{head}") == 0
    assert basic.status("01-first") == "ready-for-agent"
    assert basic.run("--once") == 0
    assert basic.keys() == ["01-first:verify"]


def test_adopted_feature_base_is_the_final_reviews_base(basic):
    feature_base = git(basic.repo, "rev-parse", "HEAD")
    (basic.repo / "code.txt").write_text("earlier tickets\n")
    git(basic.repo, "commit", "-qam", "earlier work of another runner")
    assert adopt(basic, "--feature-base", feature_base) == 0
    basic.write_config(final_review=True)
    assert basic.run() == 0
    assert f"since {feature_base} for the whole feature" in basic.calls_list()[-1]["prompt"]


@pytest.mark.parametrize("args", [
    [],
    ["--feature-base", "no-such-commit"],
    ["--resume", "01:review"],
    ["--resume", "01:deploy:HEAD"],
    ["--resume", "99:review:HEAD"],
])
def test_adopt_rejects_bad_input(basic, args):
    assert adopt(basic, *args) == rt.EXIT_USAGE
    assert not (basic.feature / "runs" / "state.json").exists()


def test_adopt_refuses_a_done_ticket(basic):
    assert basic.run("--once") == 0
    assert adopt(basic, "--resume", "01:review:HEAD") == rt.EXIT_USAGE
