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

import pathlib

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

    A git worktree wins over ``_build_info.py``: the stamp can go stale
    when a checkout is rebuilt rarely, while HEAD always names the code on
    disk. The stamp is still the answer for installed wheels, which ship
    no ``.git``.
    """
    sha = _worktree_head_sha(allow_dirty=True)
    if sha:
        return sha
    try:
        from omnigent import _build_info

        stamped = str(_build_info.COMMIT_SHA)
        if _is_hex_sha(stamped):
            return stamped
    except (ImportError, AttributeError, TypeError):
        pass
    return ""


def _worktree_head_sha(*, allow_dirty: bool = False) -> str:
    """Return the HEAD commit of the git worktree holding this package.

    :param allow_dirty: Skip the clean-worktree check. A dirty worktree's
        HEAD can still mislabel uncommitted edits, but the alternative —
        showing a stale ``_build_info.py`` commit — is strictly worse: a
        leftover stamp is guaranteed wrong, HEAD is usually right.
    """
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return ""

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    sha = _git("rev-parse", "HEAD")
    if not _is_hex_sha(sha):
        return ""
    if not allow_dirty and _worktree_has_changes(root):
        return ""
    return sha


def _worktree_has_changes(root: pathlib.Path) -> bool:
    """``True`` iff the worktree has staged or unstaged tracked changes."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.stdout.strip())


def _is_hex_sha(value: str) -> bool:
    """``True`` iff *value* looks like a full git commit hash."""
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)
