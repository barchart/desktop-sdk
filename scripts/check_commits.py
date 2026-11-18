"""Checks that commit messages are conventional commits, which release-please reads for the next version and the changelog.

The rules are commitlint's `config-conventional`: a known type, an optional scope and `!`, a subject that doesn't start
with a capital or end with a period, and no line over 100 characters. Merges, reverts, fixups and squashes are skipped, as
commitlint skips them.

    python3 scripts/check_commits.py --file .git/COMMIT_EDITMSG     # one message (the commit-msg hook)
    python3 scripts/check_commits.py --range origin/main..HEAD      # every commit in a range (CI, on a pull request)

It needs only Python's standard library, so it runs with any Python 3.11 or later, without installing anything. The Python
SDK's checks lint and test it (python/scripts/check.sh, python/tests/test_check_commits.py).
"""

# ruff: noqa: T201, INP001 - it reports on stderr, and isn't a package

from __future__ import annotations

import argparse
import re
import subprocess
import sys

TYPES = ("build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test")
MAX_LINE = 100
_HEADER = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<breaking>!)?: (?P<subject>.+)$")
_IGNORED = re.compile(r"^(Merge |Revert \"|fixup! |squash! |amend! )")


def get_problems(message: str) -> list[str]:
    """What's wrong with one commit message; empty when it's a conventional commit."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[0].strip():
        return ["the message is empty"]
    header = lines[0]
    if _IGNORED.match(header):
        return []

    problems: list[str] = []
    match = _HEADER.match(header)
    if match is None:
        problems.append(f'the header should be "type(scope): subject", such as "fix: …", not "{header}"')
    else:
        if match["type"] not in TYPES:
            problems.append(f"the type should be one of {', '.join(TYPES)}, not {match['type']}")
        subject = match["subject"]
        if subject[0].isupper():
            problems.append("the subject shouldn't start with a capital letter")
        if subject.endswith("."):
            problems.append("the subject shouldn't end with a period")
    if len(lines) > 1 and lines[1].strip():
        problems.append("a blank line should come between the header and the body")
    problems.extend(f"line {number} is over {MAX_LINE} characters" for number, line in enumerate(lines, 1) if len(line) > MAX_LINE)
    return problems


def _messages_in(revision_range: str) -> list[tuple[str, str]]:
    """Each commit in the range, oldest first, as (short hash, message)."""
    output = subprocess.run(  # noqa: S603
        ["git", "log", "--reverse", "--format=%h%x00%B%x01", revision_range],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    commits = [entry.strip("\n").split("\x00", 1) for entry in output.split("\x01") if entry.strip()]
    return [(commit, message) for commit, message in commits]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="a file with one commit message, as git passes it to the commit-msg hook")
    source.add_argument("--range", help="a git revision range, such as origin/main..HEAD")
    arguments = parser.parse_args()

    if arguments.file:
        with open(arguments.file, encoding="utf-8") as file:  # noqa: PTH123
            commits = [("this commit", file.read())]
    else:
        commits = _messages_in(arguments.range)

    failed = False
    for commit, message in commits:
        for problem in get_problems(message):
            print(f"{commit}: {problem}", file=sys.stderr)
            failed = True
    if failed:
        print("Commit messages are conventional commits: https://www.conventionalcommits.org", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
