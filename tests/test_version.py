"""Tests for the version source of truth (``omnigent.version``)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import omnigent.version as version_module
from omnigent.version import VERSION


def test_version_is_a_nonempty_string() -> None:
    """``VERSION`` is the exported source-of-truth constant."""
    assert isinstance(VERSION, str)
    assert VERSION


def test_version_is_pep440() -> None:
    """The literal must be a valid PEP 440 version — the build ships it as-is."""
    from packaging.version import Version

    # Raises InvalidVersion if the literal is malformed.
    Version(VERSION)


def test_version_matches_pyproject() -> None:
    """``VERSION`` must equal ``pyproject.toml``'s canonical ``[project].version``.

    ``scripts/sync_version_py.py`` (a pre-commit fixer) keeps them equal; this
    is the CI backstop that catches a commit made without the hook.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        # Running from an installed wheel with no source tree — nothing to check.
        return
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    pyproject_version = data["project"]["version"]
    assert pyproject_version == VERSION, (
        f"pyproject.toml version {pyproject_version!r} != omnigent.version.VERSION "
        f"{VERSION!r}; run `python scripts/sync_version_py.py`"
    )


@pytest.fixture(autouse=True)
def _reset_display_version_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test composes ``display_version()`` from a fresh commit probe."""
    monkeypatch.setattr(version_module, "_DISPLAY_VERSION", None)


def test_display_version_appends_short_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamped/known commit renders as PEP 440 local metadata."""
    monkeypatch.setattr(
        version_module, "_build_commit_sha", lambda: "9e779bef422b18cce633a9c9ad3e3b8f61d0c066"
    )
    assert version_module.display_version() == f"{VERSION}+9e779bef4"


def test_display_version_falls_back_to_bare_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """No commit info (unbuilt sdist, no git) must not break rendering."""
    monkeypatch.setattr(version_module, "_build_commit_sha", lambda: "")
    assert version_module.display_version() == VERSION


def test_display_version_is_pep440(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suffixed form still parses, and version gates see the same release."""
    from packaging.version import Version

    monkeypatch.setattr(
        version_module, "_build_commit_sha", lambda: "0123456789abcdef0123456789abcdef01234567"
    )
    rendered = version_module.display_version()
    assert Version(rendered).release == Version(VERSION).release
