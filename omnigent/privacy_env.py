"""Privacy-by-default environment for every process a session launches.

Agent CLIs and the integrations they start emit usage telemetry, crash
reports, update pings, and survey prompts unless they see a well-known
opt-out variable. A session must not depend on the operator exporting those
variables, so every child environment passes through :func:`apply_privacy_env`.

The values are forced rather than defaulted: an inherited ``DO_NOT_TRACK=0``
from a shell profile must not switch telemetry back on for a session. Add a
new tool to :data:`PRIVACY_ENV` instead of at its call site, so every launch
path picks it up.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

#: Opt-out variables forced into every session and integration environment.
#:
#: ``DO_NOT_TRACK`` and ``DISABLE_TELEMETRY`` are the cross-vendor convention.
#: The remaining entries are the per-tool spellings that ignore that convention:
#: Claude Code's nonessential-traffic switches, the Node toolchain's analytics
#: flags, and the update/analytics notices of CLIs agents commonly invoke.
PRIVACY_ENV: tuple[tuple[str, str], ...] = (
    ("DO_NOT_TRACK", "1"),
    ("DISABLE_TELEMETRY", "1"),
    ("DISABLE_ERROR_REPORTING", "1"),
    ("DISABLE_AUTOUPDATER", "1"),
    ("DISABLE_BUG_COMMAND", "1"),
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
    ("CLAUDE_CODE_DISABLE_ERROR_REPORTING", "1"),
    ("NEXT_TELEMETRY_DISABLED", "1"),
    ("TURBO_TELEMETRY_DISABLED", "1"),
    ("GATSBY_TELEMETRY_DISABLED", "1"),
    ("ASTRO_TELEMETRY_DISABLED", "1"),
    ("NG_CLI_ANALYTICS", "false"),
    ("HF_HUB_DISABLE_TELEMETRY", "1"),
    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
    ("POWERSHELL_TELEMETRY_OPTOUT", "1"),
    ("SAM_CLI_TELEMETRY", "0"),
    ("HOMEBREW_NO_ANALYTICS", "1"),
    ("GH_NO_UPDATE_NOTIFIER", "1"),
)


def privacy_env() -> dict[str, str]:
    """Return the forced privacy opt-outs as a new dict."""
    return dict(PRIVACY_ENV)


def apply_privacy_env[EnvMappingT: MutableMapping[str, str]](env: EnvMappingT) -> EnvMappingT:
    """
    Force every privacy opt-out into *env* and return it.

    Existing values are overwritten so a caller cannot re-enable telemetry by
    forwarding its own ``DO_NOT_TRACK=0``.

    :param env: Child environment to update in place.
    :returns: The same mapping, for use in a ``return`` expression.
    """
    env.update(PRIVACY_ENV)
    return env


def install_privacy_env(env: MutableMapping[str, str] | None = None) -> None:
    """
    Force the opt-outs into a process environment so children inherit them.

    :param env: Environment to update. ``None`` means this process's
        ``os.environ``.
    :returns: None.
    """
    apply_privacy_env(os.environ if env is None else env)
