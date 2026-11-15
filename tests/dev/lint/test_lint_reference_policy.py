"""Reference-policy matching and staged/branch diff coverage."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from dev.lint import lint_reference_policy as policy
from dev.lint.lint_reference_policy import check, git, violates


@pytest.fixture
def sample_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        policy, "PROHIBITED_NAMES", frozenset({"sampletool", "sampletools", "demotool"})
    )
    monkeypatch.setattr(policy, "PHRASES", re.compile(r"example[\s_-]*restricted", re.IGNORECASE))
    monkeypatch.setattr(
        policy, "HOST", policy.host_pattern(("service.example.test", "apps.example.test"))
    )
    monkeypatch.setattr(policy, "PUBLIC_HOSTS", frozenset({"docs.service.example.test"}))


@pytest.mark.parametrize(
    "text",
    [
        "Sampletool",
        "sampletools",
        "DEMOTOOL",
        "refreshSampletool",
        "SAMPLETOOL_HOST",
        "useSampletoolHost",
        "exampleRestricted",
        "Example-restricted",
        "example restricted",
        "https://tenant.service.example.test/path",
        "https://tenant.apps.example.test/path",
        "EXAMPLE.SERVICE.EXAMPLE.TEST",
        "123.service.example.test",
        "docs.extra.service.example.test",
        "See https://tenant.service.example.test.",
        "See tenant.service.example.test. More text.",
    ],
)
@pytest.mark.usefixtures("sample_policy")
def test_prohibited(text: str) -> None:
    assert violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "varchar",
        "sampletoolbox",
        "xsampletool",
        "internal server error",
        "https://docs.service.example.test/en/",
        "https://DOCS.SERVICE.EXAMPLE.TEST/en/",
        "https://tenant.example.test/path",
        "https://service.example.test/",
        "https://tenant.service.example.test.other.example/",
        "See docs.service.example.test. More text.",
    ],
)
@pytest.mark.usefixtures("sample_policy")
def test_allowed(text: str) -> None:
    assert not violates(text)


def test_configured_names_are_prohibited() -> None:
    assert policy.PROHIBITED_NAMES
    for name in policy.PROHIBITED_NAMES:
        assert violates(name)
        assert violates(name.upper())
        assert violates(f"before_{name}_after")
        assert violates(f"use{name.title()}Value")


@pytest.mark.parametrize("label", ["example", "test", "workspace", "123", "12345678"])
def test_sample_labels_do_not_exempt_policy_hosts(label: str) -> None:
    for suffix in policy.HOST_SUFFIXES:
        assert violates(f"https://{label}.{suffix}/path")


def test_public_hosts_and_reserved_examples_are_allowed() -> None:
    for host in policy.PUBLIC_HOSTS:
        assert not violates(f"https://{host}/path")
        assert violates(f"extra.{host}")
    for text in ["Databricks OAuth", "https://workspace.example.test/", "https://example.com/"]:
        assert not violates(text)


def test_fixture_source_follows_policy() -> None:
    assert not violates(Path(__file__).read_text(encoding="utf-8"))


@pytest.mark.parametrize("host", sorted(policy.SYNTHETIC_SECURITY_HOSTS))
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("multiline", [False, True])
def test_synthetic_security_fixture_literals(host: str, scheme: str, multiline: bool) -> None:
    url = f"{scheme}://{host}/codex/v1"
    source = f"endpoint = {url!r}\n"
    if multiline:
        source = f"config = '''\nbase_url = \"{url}\"\n'''\n"
    assert not policy._violation_lines(source, "tests/harnesses/test_gateway.py")
    assert violates(source)


@pytest.mark.parametrize("host", sorted(policy.SYNTHETIC_SECURITY_HOSTS))
@pytest.mark.parametrize(
    "path,template",
    [
        ("omnigent/test_gateway.py", "endpoint = {url!r}"),
        ("docs/test_gateway.py", "endpoint = {url!r}"),
        ("tests/fixtures/gateway.py", "endpoint = {url!r}"),
        ("tests/test_gateway.md", "endpoint = {url!r}"),
        ("tests/test_gateway.py", "# {url}"),
        ("tests/test_gateway.py", '"""{url}"""'),
        ("tests/test_gateway.py", 'def test_example():\n    """{url}"""'),
        ("tests/test_gateway.py", 'class Example:\n    """{url}"""'),
        ("tests/test_gateway.py", 'async def test_example():\n    """{url}"""'),
        ("tests/test_gateway.py", "endpoint = f'{url}'"),
        ("tests/test_gateway.py", 'endpoint = ("{url}" "extra")'),
        ("tests/test_gateway.py", 'endpoint = ("first"\n# {url}\n"last")'),
        ("tests/test_gateway.py", 'endpoint = "{url}" + suffix'),
        ("tests/test_gateway.py", 'endpoint = "{url}" % values'),
        ("tests/test_gateway.py", 'endpoint = "prefix"\nendpoint += "{url}"'),
        ("tests/test_gateway.py", 'endpoint = "prefix %s"\nendpoint %= "{url}"'),
        ("tests/test_gateway.py", 'endpoint = "{url}".format(path="x")'),
        ("tests/test_gateway.py", 'endpoint = str.format("{url}", path="x")'),
        ("tests/test_gateway.py", 'endpoint = "{url}".format_map(values)'),
        ("tests/test_gateway.py", 'endpoint = str.format_map("{url}", values)'),
        ("tests/test_gateway.py", 'endpoint = "{url}".replace("x", "y")'),
        ("tests/test_gateway.py", 'endpoint = "{url}".lower()'),
        ("tests/test_gateway.py", 'endpoint = "{url}"[1:]'),
        ("tests/test_gateway.py", 'endpoint = format("{url}")'),
        ("tests/test_gateway.py", 'endpoint = str("{url}")'),
        ("tests/test_gateway.py", 'endpoint = bytes("{url}", "utf-8")'),
        ("tests/test_gateway.py", 'endpoint = bytearray("{url}", "utf-8")'),
        ("tests/test_gateway.py", 'endpoint = repr("{url}")'),
        ("tests/test_gateway.py", 'endpoint = ascii("{url}")'),
        ("tests/test_gateway.py", 'endpoint = builtins.str("{url}")'),
        ("tests/test_gateway.py", 'endpoint = r"{url}"'),
        ("tests/test_gateway.py", 'endpoint = u"{url}"'),
        ("tests/test_gateway.py", 'endpoint = "/".join(["{url}"])'),
        ("tests/test_gateway.py", 'endpoint = "{url}\\x2fextra"'),
        ("tests/test_gateway.py", "endpoint = {url!r}\ninvalid("),
    ],
)
def test_synthetic_security_rule_rejects_non_fixture_contexts(
    host: str, path: str, template: str
) -> None:
    source = template.format(url=f"https://{host}/codex/v1")
    assert policy._violation_lines(source, path)


@pytest.mark.parametrize("host", sorted(policy.SYNTHETIC_SECURITY_HOSTS))
def test_synthetic_security_rule_preserves_other_findings(host: str) -> None:
    url = f"https://{host}/codex/v1"
    source = f"endpoint = {url!r}; name = {min(policy.PROHIBITED_NAMES)!r}\n"
    assert policy._violation_lines(source, "tests/test_gateway.py") == {1}
    for suffix in policy.HOST_SUFFIXES:
        for label in ["real-workspace", "example", "test", "12345678"]:
            source = f"endpoints = [{url!r}, 'https://{label}.{suffix}/']\n"
            assert policy._violation_lines(source, "tests/test_gateway.py") == {1}
    for invalid_url in [
        f"https://user@{host}/codex/v1",
        f"https://{host}:8443/codex/v1",
        f"https://extra.{host}/codex/v1",
        f"ftp://{host}/codex/v1",
        host,
    ]:
        assert policy._violation_lines(f"endpoint = {invalid_url!r}", "tests/test_gateway.py")


@pytest.mark.parametrize("host", sorted(policy.SYNTHETIC_SECURITY_HOSTS))
def test_plain_fixture_helper_argument_and_unicode_prefix(host: str) -> None:
    source = f'fixture("https://{host}/codex/v1")\nlabel = "東京"; fixture("https://{host}/")\n'
    assert not policy._violation_lines(source, "tests/test_gateway.py")


@pytest.mark.parametrize(
    "path,declaration,preamble",
    [
        (
            "web/electron/test/url.test.js",
            'const SYNTHETIC_WORKSPACE_URL = "{url}";',
            '// fixture\n"use strict";\n',
        ),
        (
            "web/android/app/src/test/example/OriginsTest.kt",
            'private const val SYNTHETIC_WORKSPACE_URL = "{url}"',
            "package example\n\nimport example.URI\n",
        ),
    ],
)
def test_script_workspace_fixture_declarations(path: str, declaration: str, preamble: str) -> None:
    host = next(host for host in policy.SYNTHETIC_SECURITY_HOSTS if ".ai-gateway." not in host)
    url = f"https://{host}/"
    literal = declaration.format(url=url)
    source = preamble + literal + "\n"
    assert not policy._violation_lines(source, path)
    assert not policy._violation_lines(preamble + literal.replace(" = ", " =\n  ") + "\n", path)
    for invalid in [
        "// " + literal,
        "/*\n" + literal + "\n*/",
        "`\n" + literal + "\n`",
        '"""\n' + literal + '\n"""',
        literal.replace('"' + url + '"', "`" + url + "`"),
        literal.replace('"' + url + '"', '"' + url + '".trim()'),
        literal.replace('"' + url + '"', '"' + url + '" + suffix'),
        literal.replace(url, url.replace("https:", "http:")),
        literal.replace(url, url[:-1]),
        literal.replace(url, f"https://user@{host}/"),
        literal.replace(url, f"https://{host}:8443/"),
        literal.replace(url, f"https://extra.{host}/"),
        literal.replace(url, f"https://private.{policy.HOST_SUFFIXES[0]}/"),
        literal.replace("SYNTHETIC_WORKSPACE_URL", "OTHER_URL"),
        "const before = 1;\n" + literal,
    ]:
        assert policy._violation_lines(preamble + invalid + "\n", path), invalid
    assert policy._violation_lines(
        source, path.replace("/test/", "/src/").replace("/src/test/", "/src/main/")
    )
    assert policy._violation_lines(source + min(policy.PROHIBITED_NAMES) + "\n", path)
    assert policy._violation_lines(
        source + f'"https://private.{policy.HOST_SUFFIXES[0]}/"\n', path
    )


@pytest.mark.parametrize("owner,pattern", policy.COMPATIBILITY_CONTEXTS)
def test_compatibility_contracts_are_context_bound(owner: str, pattern: re.Pattern[str]) -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / owner).read_text()
    matches = list(pattern.finditer(source))
    assert matches, owner
    for match in matches:
        snippet = match.group()
        assert not policy._violation_lines(snippet, owner), snippet
        assert policy._violation_lines(snippet, "other/" + owner)
        for group in pattern.groupindex:
            token = match[group]
            assert policy._violation_lines(token, owner)
            assert policy._violation_lines(snippet.replace(token, token + "Private"), owner)
            assert policy._violation_lines(snippet.replace(token, "private_" + token), owner)
        commented = (
            "<!--\n" + snippet + "\n-->" if owner.endswith(".md") else "/*\n" + snippet + "\n*/"
        )
        assert policy._violation_lines(commented, owner)
        if not owner.endswith(".md"):
            assert policy._violation_lines(repr(snippet), owner)
        assert policy._violation_lines(snippet + " " + min(policy.PROHIBITED_NAMES), owner)
        assert policy._violation_lines(
            snippet + f" https://private.{policy.HOST_SUFFIXES[0]}/", owner
        )


def test_synthetic_security_rule_checks_staged_and_branch_diffs(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    path = repository / "tests/test_gateway.py"
    path.parent.mkdir()
    host = min(policy.SYNTHETIC_SECURITY_HOSTS)
    path.write_text(f"endpoint = 'https://{host}/codex/v1'\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == []
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "synthetic fixture")
    assert check(repository, base, "HEAD") == []
    path.write_text(f"endpoint = 'https://private.{policy.HOST_SUFFIXES[0]}/'\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == ["tests/test_gateway.py:1: prohibited reference in added text"]


@pytest.fixture
def prohibited_name() -> str:
    return min(policy.PROHIBITED_NAMES)


@pytest.fixture
def repository(tmp_path: Path, prohibited_name: str) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "existing.txt").write_text(f"{prohibited_name}\nkeep\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    return tmp_path


@pytest.mark.parametrize(
    "separator", ["", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_staged_only_and_existing_lines(
    repository: Path, prohibited_name: str, separator: str
) -> None:
    path = repository / "existing.txt"
    path.write_text(f"{prohibited_name}\nkeep\nsafe\n", encoding="utf-8")
    git(repository, "add", ".")
    path.write_text(
        f"{prohibited_name}\nkeep\nsafe {separator}{prohibited_name}\n{prohibited_name}\n",
        encoding="utf-8",
    )
    assert check(repository) == []
    git(repository, "add", ".")
    expected = [
        "existing.txt:3: prohibited reference in added text",
        "existing.txt:4: prohibited reference in added text",
    ]
    assert check(repository) == expected
    if not separator:
        for setting in ["color.diff", "color.ui"]:
            git(repository, "config", setting, "always")
            try:
                assert check(repository) == expected
            finally:
                git(repository, "config", "--unset", setting)


@pytest.mark.usefixtures("sample_policy")
def test_wrapped_phrases_only_report_added_text(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repository / "existing.txt"
    path.write_text("example\nrestricted\nkeep\nexample\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "historical")
    path.write_text("example\n\nrestricted\nkeep\nexample\nsafe\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == []
    path.write_text(
        "example\n\nrestricted\nkeep\nexample\nrestricted\nkeep\nexample\nrestricted\n",
        encoding="utf-8",
    )
    git(repository, "add", ".")
    assert check(repository) == [
        "existing.txt:6: prohibited reference in added text",
        "existing.txt:8: prohibited reference in added text",
        "existing.txt:9: prohibited reference in added text",
    ]
    monkeypatch.setattr(
        policy,
        "PHRASES",
        re.compile(r"example[\s_-]*restricted|restricted[\s_-]*example", re.IGNORECASE),
    )
    definition = repository / "dev/lint/lint_reference_policy.py"
    definition.parent.mkdir(parents=True)
    definition.write_text("example\nrestricted\nexample\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "definition")
    git(repository, "mv", "dev/lint/lint_reference_policy.py", "moved.txt")
    assert check(repository) == [
        "moved.txt:1: prohibited reference in added text",
        "moved.txt:2: prohibited reference in added text",
        "moved.txt:3: prohibited reference in added text",
    ]


@pytest.mark.parametrize("edit_renamed", [False, True], ids=["pure-rename", "edited-rename"])
def test_branch_range_checks_committed_changes(
    repository: Path, prohibited_name: str, edit_renamed: bool
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    git(repository, "mv", "existing.txt", "renamed.txt")
    (repository / "existing.txt").mkdir()
    (repository / "existing.txt" / "safe.txt").write_text("safe\n", encoding="utf-8")
    if edit_renamed:
        (repository / "renamed.txt").write_text(
            f"{prohibited_name}\nkeep\n{prohibited_name}\n", encoding="utf-8"
        )
    (repository / "notes.md").write_text(f"# Note\n{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    expected = ["notes.md:2: prohibited reference in added text"]
    if edit_renamed:
        expected.append("renamed.txt:3: prohibited reference in added text")
    assert check(repository) == expected
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    (repository / "notes.md").write_bytes(b"\x00")
    assert check(repository) == []
    assert check(repository, base, "HEAD") == expected


@pytest.mark.parametrize(
    "literal_name",
    [
        "bracket[1].txt",
        pytest.param(
            ":(exclude)*",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Unsupported filename on Windows"
            ),
        ),
    ],
)
def test_paths_renames_and_line_numbers(
    repository: Path, prohibited_name: str, literal_name: str
) -> None:
    name = f"{prohibited_name} notes.txt"
    (repository / name).write_text("safe\n", encoding="utf-8")
    (repository / literal_name).write_text(f"safe\n{prohibited_name}\n", encoding="utf-8")
    (repository / "bracket1.txt").write_text(f"{prohibited_name}\n", encoding="utf-8")
    git(repository, "mv", "existing.txt", "renamed.txt")
    git(repository, "add", ".")
    assert sorted(check(repository)) == sorted(
        [
            f"{name}:1: prohibited reference in new path",
            f"{literal_name}:2: prohibited reference in added text",
            "bracket1.txt:1: prohibited reference in added text",
        ]
    )
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "changes")
    destination = f"{prohibited_name}-renamed.txt"
    git(repository, "mv", "renamed.txt", destination)
    assert check(repository) == [f"{destination}:1: prohibited reference in new path"]


def test_deletion_and_binary_content(repository: Path, prohibited_name: str) -> None:
    git(repository, "rm", "existing.txt")
    (repository / "image.bin").write_bytes(b"\x00\xff" + prohibited_name.encode())
    (repository / ".gitattributes").write_text(
        "notes.txt -diff\nimage.bin diff\n", encoding="utf-8"
    )
    (repository / "notes.txt").write_text(
        f"Unrelated sample content\n{prohibited_name}\n", encoding="utf-8"
    )
    git(repository, "add", ".")
    commit = git(repository, "rev-parse", "HEAD").decode().strip()
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},{prohibited_name}-module",
    )
    (repository / "notes.txt").write_bytes(b"\x00")
    assert sorted(check(repository)) == sorted(
        [
            f"{prohibited_name}-module:1: prohibited reference in new path",
            "notes.txt:2: prohibited reference in added text",
        ]
    )
    blob = git(repository, "rev-parse", ":notes.txt").decode().strip()
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "module")
    git(repository, "update-index", "--cacheinfo", f"100644,{blob},{prohibited_name}-module")
    assert check(repository) == [f"{prohibited_name}-module:2: prohibited reference in added text"]


def test_only_policy_definition_is_exempt(repository: Path, prohibited_name: str) -> None:
    assert {"dev/lint/lint_reference_policy.py"} == policy.POLICY_FILES
    for name in [
        *policy.POLICY_FILES,
        "docs/policy.md",
        "tests/dev/lint/test_lint_reference_policy.py",
        "tests/other.py",
    ]:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == [
        "docs/policy.md:1: prohibited reference in added text",
        "tests/dev/lint/test_lint_reference_policy.py:1: prohibited reference in added text",
        "tests/other.py:1: prohibited reference in added text",
    ]
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "changes")
    git(repository, "mv", "dev/lint/lint_reference_policy.py", "docs/moved.py")
    assert check(repository) == ["docs/moved.py:1: prohibited reference in added text"]


def test_missing_base_fails_closed(repository: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        check(repository, "missing-ref", "HEAD")


@pytest.mark.parametrize("push_context", ["flag", "hook"])
def test_cli_uses_push_range_and_does_not_echo_values(
    repository: Path, prohibited_name: str, push_context: str
) -> None:
    script = Path(__file__).resolve().parents[3] / "dev/lint/lint_reference_policy.py"
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "notes.txt").write_text(f"{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    arguments = ["--pre-push"] if push_context == "flag" else []
    environment = {**os.environ, "PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": "HEAD"}
    if push_context == "hook":
        environment["PRE_COMMIT_REMOTE_NAME"] = "origin"
    result = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "notes.txt:1: prohibited reference" in result.stdout
    assert prohibited_name not in result.stdout
    for missing in [
        ("PRE_COMMIT_FROM_REF",),
        ("PRE_COMMIT_TO_REF",),
        ("PRE_COMMIT_FROM_REF", "PRE_COMMIT_TO_REF"),
    ]:
        incomplete_environment = dict(environment)
        for variable in missing:
            del incomplete_environment[variable]
        incomplete = subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=repository,
            env=incomplete_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert incomplete.returncode == 2
        assert "reference-policy: cannot determine changes" in incomplete.stderr
    failed = subprocess.run(
        [sys.executable, str(script), "--base", "missing-ref", "--head", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 2


@pytest.mark.parametrize("base", ["", "0" * 40, "missing-ref", "pull-request"])
@pytest.mark.skipif(sys.platform == "win32", reason="The CI step runs in Bash on Linux")
def test_hook_and_ci_enforce_policy(repository: Path, prohibited_name: str, base: str) -> None:
    root = Path(__file__).resolve().parents[3]
    hook = (root / ".pre-commit-config.yaml").read_text()
    workflow = (root / ".github/workflows/lint.yml").read_text()
    assert "entry: python3 dev/lint/lint_reference_policy.py" in hook
    hooks = {
        item["id"]: item
        for repo in yaml.safe_load(hook)["repos"]
        for item in repo["hooks"]
        if item["id"].startswith("reference-policy")
    }
    assert set(hooks) == {"reference-policy"}
    assert "stages" not in hooks["reference-policy"]
    assert 'lint_reference_policy.py --base "$POLICY_BASE" --head "$POLICY_HEAD"' in workflow
    assert "fetch-depth: 0" in workflow
    step = next(
        step
        for step in yaml.safe_load(workflow)["jobs"]["pre-commit"]["steps"]
        if step.get("name") == "Check reference policy"
    )
    assert step["env"]["POLICY_HEAD"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    event_base = git(repository, "rev-parse", "HEAD").decode().strip()
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / "notes.txt").write_text(f"{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "first")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "second")
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    if base == "pull-request":
        git(repository, "switch", "-c", "updated-base", event_base)
        (repository / "upstream.txt").write_text(f"{prohibited_name}\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "upstream")
        git(repository, "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "-m", "merge", head)
    script = repository / "dev/lint/lint_reference_policy.py"
    script.parent.mkdir(parents=True)
    script.write_text((root / "dev/lint/lint_reference_policy.py").read_text(), encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=repository,
        env={
            **os.environ,
            "POLICY_BASE": event_base if base == "pull-request" else base,
            "POLICY_HEAD": head,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    if base != "missing-ref":
        assert result.returncode == 1
        assert "notes.txt:1: prohibited reference" in result.stdout
        assert "upstream.txt" not in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="The CI step runs in Bash on Linux")
def test_ci_fetches_an_available_missing_base(repository: Path, prohibited_name: str) -> None:
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/lint.yml").read_text())
    step = next(
        item
        for item in workflow["jobs"]["pre-commit"]["steps"]
        if item.get("name") == "Check reference policy"
    )
    remote = repository.parent / "upstream.git"
    git(repository, "clone", "--bare", str(repository), str(remote))
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    fetched_base = (
        git(
            remote,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            f"{base}^{{tree}}",
            "-p",
            base,
            "-m",
            "upstream base",
        )
        .decode()
        .strip()
    )
    git(remote, "update-ref", "refs/heads/main", fetched_base)
    git(repository, "remote", "add", "origin", str(remote))
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{fetched_base}^{{commit}}"],
            cwd=repository,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    (repository / "notes.txt").write_text(prohibited_name + "\n")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    script = repository / "dev/lint/lint_reference_policy.py"
    script.parent.mkdir(parents=True)
    script.write_text((root / "dev/lint/lint_reference_policy.py").read_text())
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=repository,
        env={**os.environ, "POLICY_BASE": fetched_base, "POLICY_HEAD": "HEAD"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "notes.txt:1: prohibited reference" in result.stdout
    git(repository, "cat-file", "-e", f"{fetched_base}^{{commit}}")
