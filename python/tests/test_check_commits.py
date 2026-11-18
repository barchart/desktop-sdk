"""The repository's scripts/check_commits.py: commitlint's conventional rules."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_commits", Path(__file__).parents[2] / "scripts/check_commits.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_commits"] = module
    spec.loader.exec_module(module)
    return module


get_problems = _load().get_problems


@pytest.mark.parametrize(
    "message",
    [
        "fix: keep the book's depth after a snapshot",
        "feat(registry): name instruments by their subscription",
        "feat!: drop the definition symbol from getters\n\nBREAKING CHANGE: look root instruments up by market id",
        "docs: use aliases\n\nThe examples stop expiring.\n# Please enter the commit message for your changes.",
        'Merge branch "main" into tsdoc',
        'Revert "fix: something"',
        "fixup! fix: something",
    ],
)
def test_accepts_conventional_commits_and_what_commitlint_skips(message: str) -> None:
    assert get_problems(message) == []


@pytest.mark.parametrize(
    ("message", "problem"),
    [
        ("Fixed the book", "the header should be"),
        ("fixes: the book", "the type should be one of"),
        ("fix: Keep the book", "the subject shouldn't start with a capital letter"),
        ("fix: keep the book.", "the subject shouldn't end with a period"),
        ("fix: keep the book\nwith no blank line", "a blank line should come between the header and the body"),
        ("fix: " + "x" * 100, "line 1 is over 100 characters"),
        ("# only a comment\n", "the message is empty"),
    ],
)
def test_names_what_isnt_conventional(message: str, problem: str) -> None:
    assert any(found.startswith(problem) for found in get_problems(message))
