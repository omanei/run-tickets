"""Try run-tickets without a model: python examples/demo.py

Creates a throwaway git repository in a temporary directory, copies the example feature
(examples/demo: a spec and three tickets) into it, and runs the runner there with
examples/fake_agent.py standing in for the coding agent. Your own repositories are not
touched. Pass --keep to keep the temporary repository and look at its runs/ folder.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def runner(repo: Path, *args: str) -> int:
    print(f"\n$ run-tickets {' '.join(args)}", flush=True)
    return subprocess.run([sys.executable, str(ROOT / "run_tickets.py"), *args], cwd=str(repo)).returncode


def main() -> int:
    keep = "--keep" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="run-tickets-demo-"))
    repo = tmp / "my-project"
    try:
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "demo@example.com")
        git(repo, "config", "user.name", "Demo")
        git(repo, "config", "commit.gpgsign", "false")
        (repo / "README.md").write_text("# my-project\n")
        (repo / ".gitignore").write_text(".scratch/\n")
        shutil.copytree(HERE / "demo", repo / ".scratch" / "export-csv")
        agent = [sys.executable, str(HERE / "fake_agent.py"), "--cwd", "{{repo}}", "{{prompt}}"]
        (repo / ".ticket-runner.toml").write_text(
            'agent = "fake"\n'
            "desktop_notify = false\n"
            'retry_delay = "0"\n'
            "[agents.fake]\n"
            f"command = {json.dumps(agent)}\n"
            'skill_format = "/skill:{{skill}} {{args}}"\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")

        feature = ".scratch/export-csv"
        codes = [runner(repo, "status", feature), runner(repo, "run", feature),
                 runner(repo, "status", feature)]
        if keep:
            print(f"\nThe demo repository is kept at {repo}")
        return max(codes)
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
