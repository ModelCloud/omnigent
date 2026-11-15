"""Check added text and paths against the repository reference policy."""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from bisect import bisect_right
from pathlib import Path
from urllib.parse import urlsplit

POLICY_FILES = frozenset({"dev/lint/lint_reference_policy.py"})
PROHIBITED_NAMES = frozenset(
    {
        "arca",
        "arcas",
        "arclet",
        "arclets",
        "isaac",
        "dbcert",
        "dbcerts",
        "lakebox",
        "ucode",
        "dbexec",
        "spog",
    }
)
PHRASES = re.compile(
    r"databricks[\s_-]*(?:internal|corp(?:orate)?|employees?|network|only)"
    r"|(?:internal|corp(?:orate)?)[\s_-]*databricks"
    r"|databricks-eng(?:/|\b)"
    r"|\binternal[-_]beta\b"
    r"|\be2[-_]dogfood\b",
    re.IGNORECASE,
)
HOST_SUFFIXES = ("databricks.com", "databricksapps.com", "azuredatabricks.net")
PUBLIC_HOSTS = frozenset(
    {
        "www.databricks.com",
        "docs.databricks.com",
        "accounts.cloud.databricks.com",
        "login.databricks.com",
    }
)


def host_pattern(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(suffix) for suffix in suffixes)
    return re.compile(
        r"(?<![\w.-])(?:[a-z0-9-]+\.)+(?:" + alternatives + r")(?![\w-]|\.[\w-])",
        re.IGNORECASE,
    )


HOST = host_pattern(HOST_SUFFIXES)
SYNTHETIC_SECURITY_HOSTS = frozenset(
    {
        "synthetic-security-fixture-0000000000000000.cloud.databricks.com",
        "synthetic-security-fixture-0000000000000000.ai-gateway.cloud.databricks.com",
    }
)
_URL = re.compile(r"https?://[^\s\"'<>\\]+")

# Only these existing contracts, at their owning decode/read boundaries. Each
# pattern captures the identifier, not its surrounding source or documentation.
COMPATIBILITY_CONTEXTS = (
    (
        "web/src/lib/nativeBridge.ts",
        re.compile(
            r"export function personalHostCapabilities\(\s*"
            r"features: DesktopFeatures \| null,?\s*\): PersonalHostCapabilities \{\s*"
            r"return \{\s*enabled: features\?\.(?P<contract1>databricksInternalFeatures)"
            r" === true,\s*previewRecovery: features\?\.(?P<contract2>arcaPreviewRecovery)"
            r" === true,?\s*\};\s*\}"
        ),
    ),
    (
        "web/src/shell/AppShellOverlays.tsx",
        re.compile(r'isFeatureEnabled\(serverInfo, "(?P<contract1>arca_shutdown_warnings)"\)'),
    ),
    (
        "web/electron/docs/managed-preferences.md",
        re.compile(
            r"(?m)^\|\s*`(?P<contract1>databricksInternalFeaturesEnabled)`\s*\|\s*"
            r"Boolean\s*\|\s*No\s*\|\s*`false`\s*\|"
        ),
    ),
    (
        "docs/open-in-editor.md",
        re.compile(
            r"`(?P<contract1>databricks-arca|arca\.ssh|arclet|arclet_<host\.name>\.ssh|"
            r"arclet_|arclet-|arca_type arca|arca_type arclet|arclet_<name>\.ssh|isaac-arca)`"
        ),
    ),
    (
        "web/src/lib/personalHost.ts",
        re.compile(
            r'const PERSONAL_HOST_ID_STORAGE_KEY = "(?P<contract1>omnigent:arca-host-id)";'
        ),
    ),
    (
        "web/src/lib/personalHost.ts",
        re.compile(r"""host\.name\.endsWith\("'s (?P<contract1>arca)"\)"""),
    ),
    (
        "web/src/lib/arcaShutdownWarning.test.ts",
        re.compile(
            r"""isPersonalHost\(\{ host_id: "a", name: "user's """
            r"""(?P<contract1>arca|arclet)" \}, null\)"""
        ),
    ),
    (
        "web/src/hooks/useBrowserAgentRelay.ts",
        re.compile(
            r"rememberedPersonalHostId \? \{ (?P<contract1>rememberedArcaHostId): "
            r"rememberedPersonalHostId \}"
        ),
    ),
    (
        "web/src/hooks/useBrowserAgentRelay.ts",
        re.compile(r"sourceIsPersonalHost \? \{ (?P<contract1>sourceIsArcaHost): true \}"),
    ),
    (
        "web/src/shell/NewChatDialog.tsx",
        re.compile(r'effectiveSandboxProvider === "(?P<contract1>arclet)"'),
    ),
    (
        "web/src/shell/NewChatDialog.test.tsx",
        re.compile(r'sandboxSessionCatalogKey\("(?P<contract1>arclet)",'),
    ),
    (
        "web/src/shell/NewChatDialog.test.tsx",
        re.compile(r'sandbox_provider: "(?P<contract1>arclet)"'),
    ),
    (
        "web/src/shell/NewChatDialog.test.tsx",
        re.compile(r"\{ (?P<contract1>databricksInternalFeatures): (?:true|false) \}"),
    ),
    (
        "web/src/shell/ForkSessionDialog.test.tsx",
        re.compile(
            r'\["(?:Managed sandbox host|Databricks Sandbox host)", '
            r'"(?P<contract1>arclet|lakebox)", (?:true|false)\]'
        ),
    ),
    (
        "web/src/shell/ForkSessionDialog.test.tsx",
        re.compile(r'sandbox_provider: "(?P<contract1>arclet)"'),
    ),
    (
        "web/src/shell/ForkSessionDialog.test.tsx",
        re.compile(r'sandbox_providers: \["(?P<contract1>lakebox)", "(?P<contract2>arclet)"\]'),
    ),
    (
        "web/src/hooks/useBrowserAgentRelay.test.tsx",
        re.compile(r'(?P<contract1>rememberedArcaHostId): "remote-host"'),
    ),
    (
        "web/src/hooks/useSessionReconnect.test.ts",
        re.compile(r'''hostName: "e2e's (?P<contract1>arca)"'''),
    ),
    (
        "web/src/lib/nativeBridge.test.ts",
        re.compile(r"(?P<contract1>databricksInternalFeatures|arcaPreviewRecovery):"),
    ),
)
_NON_CODE = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
)


def _line_spans(text: str, start: int, end: int) -> set[tuple[int, int, int]]:
    prefix = text[:start]
    line = prefix.count("\n") + 1
    column = len(prefix.rsplit("\n", 1)[-1])
    return {(line, column, column + end - start)}


def _compatibility_spans(text: str, path: str | None) -> set[tuple[int, int, int]]:
    if not path:
        return set()
    non_code = (
        re.finditer(r"<!--[\s\S]*?-->", text) if path.endswith(".md") else _NON_CODE.finditer(text)
    )
    excluded = [(match.start(), match.end()) for match in non_code]
    spans = set()
    for owner, pattern in COMPATIBILITY_CONTEXTS:
        if owner != path:
            continue
        for match in pattern.finditer(text):
            if match.start() and (
                text[match.start() - 1].isalnum() or text[match.start() - 1] in "_$.\\"
            ):
                continue
            # A matching example inside a comment or string is not a read boundary.
            anchor = match.start() + len(match.group()) - len(match.group().lstrip())
            if any(start <= anchor < end for start, end in excluded):
                continue
            for group in pattern.groupindex:
                spans.update(_line_spans(text, *match.span(group)))
    return spans


def _script_fixture_spans(text: str, path: str) -> set[tuple[int, int, int]]:
    """A first, top-level URL fixture declaration in desktop/Android unit tests."""
    if path.startswith("web/electron/test/") and path.endswith(".test.js"):
        declaration = r"const SYNTHETIC_WORKSPACE_URL =\s*([\"'])([^\n\\$]+?)\1;"
        preamble = r"\s*(?:(?:[\"']use strict[\"'];)\s*)?"
    elif path.startswith("web/android/app/src/test/") and path.endswith("Test.kt"):
        declaration = r'private const val SYNTHETIC_WORKSPACE_URL =\s*(")([^\n\\$]+?)\1'
        preamble = r"\s*(?:(?:package|import) [\w.*]+\s*)*"
    else:
        return set()
    match = re.search(r"(?m)^" + declaration + r"[ \t]*$", text)
    if match is None:
        return set()
    prefix = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", text[: match.start()])
    if re.fullmatch(preamble, prefix) is None:
        return set()
    url = match[2]
    # This fixture is a canonical workspace root, not a way to encode or format a host.
    roots = {f"https://{host}/" for host in SYNTHETIC_SECURITY_HOSTS if ".ai-gateway." not in host}
    if url not in roots:
        return set()
    start = match.start(2) + len("https://")
    return _line_spans(text, start, match.end(2) - 1)


def _synthetic_fixture_spans(text: str, path: str | None) -> set[tuple[int, int, int]]:
    """Recognize exact, unescaped synthetic URLs in standalone Python test literals."""
    if not path:
        return set()
    if not any(host in text.lower() for host in SYNTHETIC_SECURITY_HOSTS):
        return set()
    if (
        not path.startswith("tests/")
        or not Path(path).name.startswith("test_")
        or Path(path).suffix != ".py"
    ):
        return _script_fixture_spans(text, path)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    excluded = set()
    for node in ast.walk(tree):
        transformed = isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Subscript, ast.Attribute))
        if isinstance(node, ast.Call):
            transformed = (
                isinstance(node.func, ast.Name)
                and node.func.id in {"str", "bytes", "bytearray", "repr", "ascii", "format"}
            ) or (
                isinstance(node.func, ast.Attribute)
                and (
                    node.func.attr
                    in {
                        "str",
                        "bytes",
                        "bytearray",
                        "repr",
                        "ascii",
                        "format",
                        "format_map",
                        "replace",
                        "join",
                        "substitute",
                        "safe_substitute",
                    }
                    or isinstance(node.func.value, ast.Constant)
                    or (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id in {"str", "bytes", "bytearray"}
                    )
                )
            )
        if transformed:
            excluded.update(id(child) for child in ast.walk(node))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                excluded.add(id(node.body[0].value))
    literals = {
        ((node.lineno, node.col_offset), (node.end_lineno, node.end_col_offset))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in excluded
        and node.end_lineno is not None
        and node.end_col_offset is not None
    }
    source_lines = text.split("\n")
    literal_tokens = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if (
                token.type != tokenize.STRING
                or "\\" in token.string
                or not token.string.startswith(("'", '"'))
            ):
                continue
            first = (
                token.start[0],
                len(source_lines[token.start[0] - 1][: token.start[1]].encode("utf-8")),
            )
            last = (
                token.end[0],
                len(source_lines[token.end[0] - 1][: token.end[1]].encode("utf-8")),
            )
            if (first, last) in literals:
                literal_tokens.add((first, last))
    except (tokenize.TokenError, IndentationError):
        return set()
    spans = set()
    for number, line in enumerate(source_lines, 1):
        for url in _URL.finditer(line):
            try:
                parsed = urlsplit(url.group())
                if (
                    parsed.hostname not in SYNTHETIC_SECURITY_HOSTS
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port is not None
                ):
                    continue
            except ValueError:
                continue
            start = (number, len(line[: url.start()].encode("utf-8")))
            end = (number, len(line[: url.end()].encode("utf-8")))
            if not any(first <= start and end <= last for first, last in literal_tokens):
                continue
            spans.update(
                (number, url.start() + host.start(), url.start() + host.end())
                for host in HOST.finditer(url.group())
                if host.group().lower() == parsed.hostname
            )
    return spans


def _violation_lines(text: str, path: str | None = None) -> set[int]:
    """Return LF-based locations, including phrases that wrap across lines."""
    accepted = _synthetic_fixture_spans(text, path) | _compatibility_spans(text, path)
    source_lines = text.split("\n")
    for line_number, start, end in accepted:
        line = source_lines[line_number - 1]
        source_lines[line_number - 1] = line[:start] + " " * (end - start) + line[end:]
    text = "\n".join(source_lines)
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
    lines = set()
    original_lines = text.split("\n")
    for line_number, normalized in enumerate(words.split("\n"), 1):
        if PROHIBITED_NAMES.intersection(re.findall(r"[a-z]+", normalized.lower())) or any(
            match.group().lower() not in PUBLIC_HOSTS
            for match in HOST.finditer(original_lines[line_number - 1])
        ):
            lines.add(line_number)
    line_starts = [0, *(match.end() for match in re.finditer("\n", words))]
    position = 0
    while match := PHRASES.search(words, position):
        first_line = bisect_right(line_starts, match.start())
        for offset, segment in enumerate(match.group().split("\n")):
            if segment.strip():
                lines.add(first_line + offset)
        position = match.start() + 1
    return lines


def violates(text: str) -> bool:
    """Match names in prose, snake_case, camelCase, and filenames."""
    return bool(_violation_lines(text))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def check(root: Path, base: str | None = None, head: str | None = None) -> list[str]:
    """Check staged changes, or the branch diff from a merge base."""
    if head and not base:
        raise ValueError("--head requires --base")
    revisions = ["--cached"]
    if base:
        target = head or "HEAD"
        ancestor = git(root, "merge-base", base, target).decode().strip()
        revisions = [ancestor, target]
    options = ["--no-ext-diff", "--no-textconv", "--no-color"]
    entries = iter(
        git(
            root,
            "diff",
            *options,
            "--find-renames",
            *revisions,
            "--raw",
            "--no-abbrev",
            "-z",
            "--diff-filter=AMRT",
            "--",
        ).split(b"\0")
    )
    findings = []
    for metadata in entries:
        if not metadata:
            break
        old_mode, mode, old_object_id, object_id, status = metadata[1:].split()
        old_path = os.fsdecode(next(entries))
        renamed = status.startswith(b"R")
        path = os.fsdecode(next(entries)) if renamed else old_path
        if path in POLICY_FILES:
            continue
        if (status == b"A" or renamed) and violates(path):
            findings.append(f"{path}:1: prohibited reference in new path")
        if mode == b"160000":
            continue
        contents = git(root, "cat-file", "blob", object_id.decode())
        if b"\0" in contents[:8000]:
            continue
        violation_lines = _violation_lines(contents.decode("utf-8", errors="replace"), path)
        if not violation_lines:
            continue
        if status == b"A" or old_mode == b"160000" or old_path in POLICY_FILES:
            added_lines = violation_lines
        else:
            patch = git(
                root,
                "diff",
                *options,
                "--text",
                "--unified=0",
                "--inter-hunk-context=0",
                old_object_id.decode(),
                object_id.decode(),
                "--",
            ).decode("utf-8", errors="replace")
            added_lines = set()
            line_number = None
            for line in patch.split("\n"):
                hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk:
                    line_number = int(hunk[1])
                elif line_number is not None and line.startswith("+"):
                    added_lines.add(line_number)
                    line_number += 1
                elif line_number is not None and line.startswith(" "):
                    line_number += 1
        findings.extend(
            f"{path}:{line_number}: prohibited reference in added text"
            for line_number in sorted(violation_lines & added_lines)
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("PRE_COMMIT_FROM_REF"))
    parser.add_argument("--head", default=os.environ.get("PRE_COMMIT_TO_REF"))
    parser.add_argument(
        "--pre-push",
        action="store_true",
        default="PRE_COMMIT_REMOTE_NAME" in os.environ,
        help="Require both push revisions.",
    )
    args = parser.parse_args()
    try:
        if args.pre_push and not (args.base and args.head):
            raise ValueError("--pre-push requires both --base and --head")
        findings = check(Path.cwd(), args.base, args.head)
    except (subprocess.CalledProcessError, ValueError):
        print(
            "reference-policy: cannot determine changes; check the base/head revisions",
            file=sys.stderr,
        )
        return 2
    for finding in findings:
        print(finding)
    if findings:
        print("Use generic names and synthetic examples. Do not add inline suppressions.")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
