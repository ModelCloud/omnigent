---
name: privacy-no-telemetry
description: Enforce Omnigent's no-telemetry, no-tracking, no-phone-home rule for every agent CLI, terminal pane, MCP server, and integration a session launches. Load when adding or changing a harness, an executor environment, a terminal pane, an MCP server launch, or anything that spawns a subprocess inside a session.
---

# No telemetry, tracking, or phone-home

An Omnigent session never emits telemetry, usage analytics, crash reports,
update pings, or session replay. That covers Omnigent's own processes and every
vendor CLI or integration it launches, including their own children.

## The mechanism

`omnigent/privacy_env.py` owns the canonical list:

- `PRIVACY_ENV` — `DO_NOT_TRACK=1`, `DISABLE_TELEMETRY=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, the Claude Code
  error/auto-update switches, the Node toolchain analytics flags, and the
  update/analytics notices of common CLIs.
- `apply_privacy_env(env)` — forces those values into a child environment,
  overwriting anything the caller passed.
- `install_privacy_env()` — forces them into the current process environment so
  descendants inherit them without a call site.

Enforcement points already wired:

| Boundary | Where |
| --- | --- |
| Any harness child | `omnigent/inner/agent_env.py` `clean_agent_env` |
| Claude Code terminal and probes | `build_native_claude_terminal_env` |
| Claude Agent SDK CLI | `claude_sdk_executor` options env |
| Terminal panes | `omnigent/inner/terminal.py` `terminal_pane_environment` |
| Runner subprocess | `omnigent/host/connect.py` `_build_runner_env` |
| Host daemon subprocess | `omnigent/cli.py` `_build_host_daemon_env` |
| Process entry points | `omnigent.cli`, runner `_entry`, host daemon and service entries |
| Omnigent's own analytics | `omnigent/telemetry/client.py` — `TELEMETRY_PERMANENTLY_DISABLED` makes `is_disabled()` always `True` |

## Rules

- Never delete, weaken, or bypass a call to `apply_privacy_env` or
  `install_privacy_env`. A caller that sets `DO_NOT_TRACK=0` must still launch a
  private child.
- A new spawn path must either build its environment with `clean_agent_env` or
  call `apply_privacy_env` itself. Do not copy the variable list into a call
  site; extend `PRIVACY_ENV`.
- Never add a feature that sends usage data, crash data, transcripts, prompts,
  repository paths, or hostnames to a third party. That includes "anonymous"
  aggregate counters and vendor SDKs that buffer events locally.
- Never clear `TELEMETRY_PERMANENTLY_DISABLED` or add a config/consent path
  that re-enables Omnigent's own telemetry. The emitter and its remote config
  fetch stay wired only for upstream compatibility; they must remain
  unreachable.
- Never add a telemetry opt-in that is on by default, and never make quiet
  behavior depend on a variable being absent.
- Do not remove the opt-outs for a probe or a "faster startup" path. Probes and
  terminals share the same privacy contract.

## Verify

Run the module's tests and check a real child environment:

```sh
uv run --no-sync pytest tests/test_privacy_env.py tests/test_telemetry.py --reruns 0
```

```sh
uv run --no-sync python - <<'PY'
from omnigent.inner.agent_env import clean_agent_env
env = clean_agent_env(source={"DO_NOT_TRACK": "0", "PATH": "/usr/bin"})
assert env["DO_NOT_TRACK"] == "1"
assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
print("privacy env forced")
PY
```
