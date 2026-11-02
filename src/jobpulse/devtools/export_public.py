"""Export a commit of this checkout into a clone of the public repository (maintainers only).

The tree comes from ``git archive`` (so paths marked ``export-ignore`` in .gitattributes, such
as PROGRESS.md, stay private) and is scanned before anything is copied: secret-shaped values,
email addresses outside example domains, home-directory paths, plus literal strings from a
gitignored patterns file and the secrets in ``.env``. A finding names the file and line, never
the value, and leaves the public clone untouched. Nothing is committed or pushed.

    uv run python -m jobpulse.devtools.export_public ../jobpulse-public [--ref HEAD]
"""

import argparse
import io
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PATTERNS_FILE = Path(".export-private-patterns")
ENV_SECRETS = (
    "NTFY_TOPIC",
    "NTFY_TOKEN",
    "DISCORD_WEBHOOK_URL",
    "LLM_API_KEY",
    "DASHBOARD_PASSWORD",
)
MIN_LITERAL = 6

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # What the wizard and the README generate: token_urlsafe(24) after the prefix.
    ("ntfy topic", re.compile(r"\bjobpulse-[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])")),
    (
        "Discord webhook",
        re.compile(r"discord(?:app)?\.com/api/webhooks/\d{17,20}/[A-Za-z0-9_-]{50,}"),
    ),
    ("Anthropic key", re.compile(r"\bsk-ant-(?:api|admin)\d\d-[A-Za-z0-9_-]{20,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("home directory path", re.compile(r"/Users/[A-Za-z0-9._-]+/|/home/[a-z_][a-z0-9_-]*/")),
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
ALLOWED_EMAIL = re.compile(
    r"@(?:[a-z0-9-]+\.)*example\.(?:com|org|net)$|^noreply@|@users\.noreply\.github\.com$|^git@",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    kind: str


def literals(patterns_file: Path, env_file: Path) -> tuple[str, ...]:
    """The patterns file's lines (no blanks or # comments), then the secrets set in ``env_file``."""
    found: list[str] = []
    if patterns_file.is_file():
        for line in patterns_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(line)
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            value = value.strip().strip("'\"")
            if sep and name.strip() in ENV_SECRETS and len(value) >= MIN_LITERAL:
                found.append(value)
    return tuple(found)


def scan(root: Path, *, literals: Sequence[str]) -> list[Finding]:
    """Every finding under ``root``, by path and line; binary files are skipped."""
    lowered = [(index, text.casefold()) for index, text in enumerate(literals, start=1)]
    findings: list[Finding] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root)
        for number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in RULES:
                if pattern.search(line):
                    findings.append(Finding(relative, number, kind))
            if any(not ALLOWED_EMAIL.search(m.group(0)) for m in EMAIL.finditer(line)):
                findings.append(Finding(relative, number, "email address"))
            folded = line.casefold()
            for index, literal in lowered:
                if literal in folded:
                    findings.append(Finding(relative, number, f"private pattern {index}"))
    return findings


def _archive(repo: Path, ref: str, into: Path) -> None:
    tar = subprocess.run(
        ["git", "archive", "--format=tar", ref], cwd=repo, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        archive.extractall(into, filter="data")


def _replace_tree(source: Path, dest: Path) -> None:
    for item in dest.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)


def export(repo: Path, ref: str, dest: Path, *, literals: Sequence[str]) -> int:
    """Archive ``ref``, scan it, and mirror it into ``dest`` (a git clone) when clean."""
    if not (dest / ".git").exists():
        raise SystemExit(f"{dest} isn't a git clone of the public repository (no .git)")
    with tempfile.TemporaryDirectory() as scratch:
        tree = Path(scratch)
        _archive(repo, ref, tree)
        findings = scan(tree, literals=literals)
        if findings:
            for finding in findings:
                print(f"{finding.path}:{finding.line}: {finding.kind}")
            print(f"{len(findings)} findings; nothing was copied to {dest}")
            return 1
        _replace_tree(tree, dest)
    print(f"exported {ref} into {dest}; review `git -C {dest} status`, then commit")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_public", description=__doc__.split("\n")[0])
    parser.add_argument("dest", type=Path, help="a clone of the public repository")
    parser.add_argument("--ref", default="HEAD", help="the commit to export (default: HEAD)")
    parser.add_argument("--patterns", type=Path, default=PATTERNS_FILE)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    return export(Path.cwd(), args.ref, args.dest, literals=literals(args.patterns, args.env_file))


if __name__ == "__main__":
    raise SystemExit(main())
