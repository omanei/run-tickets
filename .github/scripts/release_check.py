"""Checks a release tag against the code and prints the release notes.

    python .github/scripts/release_check.py 0.5.0 > notes.md

Fails unless the tag is X.Y.Z, equals the version in run_tickets.py and in
pyproject.toml, and CHANGELOG.md has a dated section for it ("## 0.5.0 - 2026-10-27").
Prints that section and how to install, for the GitHub Release.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

INSTALL = """### Install

```bash
uv tool install run-tickets     # or: pipx install run-tickets
uv tool upgrade run-tickets     # or: pipx upgrade run-tickets
```"""


def check(tag: str, root: Path = ROOT) -> str:
    """The release notes for `tag`; ValueError naming every mismatch."""
    errors = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", tag):
        errors.append(f"the tag {tag!r} is not X.Y.Z (no 'v')")
    sources = (("__version__ in run_tickets.py", "run_tickets.py", r'^__version__ = "([^"]+)"'),
               ("version in pyproject.toml", "pyproject.toml", r'^version = "([^"]+)"'))
    for what, name, pattern in sources:
        m = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.MULTILINE)
        found = m.group(1) if m else None
        if found != tag:
            errors.append(f"{what} is {found}, not {tag}")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(tag)} - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*\n(.*?)(?=^## |\Z)",
                  changelog, re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        errors.append(f"CHANGELOG.md has no '## {tag} - YYYY-MM-DD' section with the changes")
    if errors:
        raise ValueError("\n".join(errors))
    return m.group(1).strip() + "\n\n" + INSTALL + "\n"  # type: ignore[union-attr]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        print(check(sys.argv[1]), end="")
    except ValueError as e:
        print(f"release {sys.argv[1]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
