"""Runtime source of truth for the omnigent version.

``VERSION`` is the version string the runtime imports directly — the CLI
(``--version``), the server's ``/api/version`` endpoint, and the
host/runner ``hello`` frames all read this same value. Importing the
constant (rather than reading ``importlib.metadata``) means the version is
correct regardless of how the package was installed.

This constant mirrors the canonical ``[project].version`` in
``pyproject.toml``; a pre-commit hook (``scripts/sync_version_py.py``) keeps
the two in sync, so releases are cut by bumping pyproject alone (via
``scripts/update_versions.py``).
"""

VERSION = "0.20.0.dev0"

_DISPLAY_VERSION: str | None = None


def display_version() -> str:
    """Return ``VERSION`` with the build commit appended as local metadata.

    Release wheels stamp ``omnigent/_build_info.py`` at build time; when it
    is absent (source checkouts that were never built) the commit is read
    from the containing git worktree instead. The result looks like
    ``0.20.0.dev0+9e779bef4`` — PEP 440 local metadata, so
    ``Version(...).release`` comparisons ignore the suffix.

    ``VERSION`` itself stays commit-free: update checks and version gates
    compare it directly. Everything user-visible (``/api/version``,
    ``server_version`` fields, ``--version``) uses this form instead.

    :returns: ``VERSION+<9-char sha>`` when a commit is known, else
        ``VERSION``.
    """
    global _DISPLAY_VERSION
    if _DISPLAY_VERSION is None:
        sha = _build_commit_sha()
        _DISPLAY_VERSION = f"{VERSION}+{sha[:9]}" if sha else VERSION
    return _DISPLAY_VERSION


def _build_commit_sha() -> str:
    """Resolve the commit this build was cut from, or ``""``.

    Prefers the ``_build_info.py`` stamp written by ``setup.py`` at wheel
    build time; falls back to ``git rev-parse HEAD`` against the worktree
    that contains this package.
    """
    try:
        from omnigent import _build_info

        sha = str(_build_info.COMMIT_SHA)
        if _is_hex_sha(sha):
            return sha
    except (ImportError, AttributeError, TypeError):
        pass
    return _worktree_head_sha()


def _worktree_head_sha() -> str:
    """Return the HEAD commit of the git worktree holding this package."""
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and _is_hex_sha(sha) else ""


def _is_hex_sha(value: str) -> bool:
    """``True`` iff *value* looks like a full git commit hash."""
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)
