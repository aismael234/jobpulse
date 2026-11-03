"""The public export: `git archive` of a commit (export-ignore honored), scanned for secrets and
personal data before anything is copied into the public clone."""

import subprocess
from pathlib import Path

import pytest

from jobpulse.devtools import export_public

TOPIC = "jobpulse-" + "Ab3_" * 8  # what the wizard generates: 32 characters after the prefix
# Split so this file passes the scan it tests.
GMAIL = "someone@" + "gmail.com"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    return root


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"NTFY_TOPIC={TOPIC}", "ntfy topic"),
        ("https://discord.com/api/webhooks/123456789012345678/" + "x" * 68, "Discord webhook"),
        ("key = 'sk-ant-api03-" + "a" * 40 + "'", "Anthropic key"),
        (f"mail me at {GMAIL}", "email address"),
        ("see /" + "Users/someone/notes.txt", "home directory path"),
        ("-----BEGIN OPENSSH PRIVATE " + "KEY-----", "private key"),
    ],
)
def test_the_scan_finds_secrets_and_personal_data(tmp_path: Path, text: str, kind: str) -> None:
    (tmp_path / "a.txt").write_text(f"line one\n{text}\n", encoding="utf-8")
    assert export_public.scan(tmp_path, literals=()) == [
        export_public.Finding(Path("a.txt"), 2, kind)
    ]


def test_the_scan_lets_test_values_and_example_addresses_through(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        'KEY = "sk-ant-test-0123456789"\n'
        'HOOK = "https://discord.com/api/webhooks/1/abcdefghijklmnop"\n'
        "contact = 'dev@example.com', 'someone@example.org', noreply@anthropic.com\n"
        "topic = 'jobpulse-short-topic-for-tests'\n",
        encoding="utf-8",
    )
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe " + GMAIL.encode())
    assert export_public.scan(tmp_path, literals=()) == []


def test_literals_match_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("Written by Jane Q. Private\n", encoding="utf-8")
    found = export_public.scan(tmp_path, literals=("jane q. private",))
    assert found == [export_public.Finding(Path("notes.md"), 1, "private pattern 1")]


def test_literals_come_from_the_patterns_file(tmp_path: Path) -> None:
    patterns = tmp_path / "patterns"
    patterns.write_text(f"# a comment\n\nJane Q. Private\n{TOPIC}\n", encoding="utf-8")
    assert export_public.literals(patterns) == ("Jane Q. Private", TOPIC)
    assert export_public.literals(tmp_path / "missing") == ()


def test_export_copies_the_commit_without_export_ignored_files(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path / "private",
        {
            "README.md": "hello\n",
            "src/app.py": "print('hi')\n",
            "PROGRESS.md": "personal log\n",
            ".gitattributes": "PROGRESS.md export-ignore\n.gitattributes export-ignore\n",
        },
    )
    (repo / "uncommitted.txt").write_text("not exported\n", encoding="utf-8")
    dest = tmp_path / "public"
    dest.mkdir()
    git(dest, "init", "-q")
    (dest / "stale.txt").write_text("removed by the export\n", encoding="utf-8")

    code = export_public.export(repo, "HEAD", dest, literals=())

    assert code == 0
    files = sorted(
        str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    assert files == ["README.md", "src/app.py"]
    assert (dest / ".git").is_dir()


def test_export_with_findings_leaves_the_public_clone_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "private", {"config.txt": f"topic: {TOPIC}\n"})
    dest = tmp_path / "public"
    dest.mkdir()
    git(dest, "init", "-q")
    (dest / "kept.txt").write_text("still here\n", encoding="utf-8")

    assert export_public.export(repo, "HEAD", dest, literals=()) == 1

    assert (dest / "kept.txt").exists()
    assert not (dest / "config.txt").exists()
    out = capsys.readouterr().out
    assert "config.txt:1: ntfy topic" in out
    assert TOPIC not in out  # findings never echo the value


def test_export_refuses_a_destination_that_isnt_a_git_clone(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "private", {"README.md": "hello\n"})
    (tmp_path / "plain").mkdir()
    with pytest.raises(SystemExit, match="git"):
        export_public.export(repo, "HEAD", tmp_path / "plain", literals=())
