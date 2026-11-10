"""`privyx install-hook` and `privyx hook`: no secret or placeholder reaches git."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.commands.hook import _added_lines
from privyx.cli.main import cli

KEY = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"


def _git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Ana", "-c", "user.email=a@example.com", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)


def test_install_hook_writes_both_hooks_and_is_idempotent(runner: CliRunner, repo: Path) -> None:
    first = runner.invoke(cli, ["install-hook"])
    hooks = repo / ".git" / "hooks"
    scripts = [(hooks / name).read_bytes() for name in ("pre-commit", "commit-msg")]

    again = runner.invoke(cli, ["install-hook"])

    assert first.exit_code == again.exit_code == 0
    assert [(hooks / name).read_bytes() for name in ("pre-commit", "commit-msg")] == scripts
    assert b"privyx hook pre-commit" in scripts[0]
    assert b'privyx hook commit-msg "$1"' in scripts[1]
    assert os.access(hooks / "pre-commit", os.X_OK)


def test_install_hook_leaves_a_hook_of_another_tool_alone(runner: CliRunner, repo: Path) -> None:
    other = repo / ".git" / "hooks" / "pre-commit"
    other.write_text("#!/bin/sh\nmake lint\n", encoding="utf-8")

    result = runner.invoke(cli, ["install-hook"])

    assert result.exit_code == 1
    assert other.read_text(encoding="utf-8") == "#!/bin/sh\nmake lint\n"
    assert "add this line to it: privyx hook pre-commit" in result.output
    assert (repo / ".git" / "hooks" / "commit-msg").exists()


def test_install_hook_follows_core_hooks_path(runner: CliRunner, repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", ".githooks")

    assert runner.invoke(cli, ["install-hook"]).exit_code == 0
    assert (repo / ".githooks" / "pre-commit").exists()


def test_pre_commit_blocks_a_key_and_a_placeholder_by_line(runner: CliRunner, repo: Path) -> None:
    _stage(repo, "app.py", f'x = 1\nkey = "{KEY}"\n')
    _stage(repo, "notes.md", "Mail <PRIVYX_EMAIL_1> about it\n")

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert result.exit_code == 1
    assert "app.py:2  API_KEY" in result.output
    assert "notes.md:1  placeholder <PRIVYX_EMAIL_1>" in result.output
    assert KEY not in result.output


def test_pre_commit_checks_text_after_a_lone_carriage_return(runner: CliRunner, repo: Path) -> None:
    _stage(repo, "app.py", f'x = 1\rkey = "{KEY}"\n')

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert result.exit_code == 1
    assert "app.py:1  API_KEY" in result.output


def test_pre_commit_passes_personal_data_and_removed_lines(runner: CliRunner, repo: Path) -> None:
    _stage(repo, "old.py", f'key = "{KEY}"\n')
    _git(repo, "commit", "-q", "--no-verify", "-m", "old")
    _stage(repo, "old.py", "key = load()\n")
    _stage(repo, "users.py", 'OWNER = "ana@example.com"  # +1 555 0100\n')

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert result.exit_code == 0, result.output


def test_a_private_key_over_several_lines_is_found(runner: CliRunner, repo: Path) -> None:
    body = "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"] * 3)
    _stage(repo, "k.pem", f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n")

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert "k.pem:1  PRIVATE_KEY" in result.output


def test_added_lines_tell_content_from_headers() -> None:
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +4,2 @@\n"
        "+++ b/not-a-header\n+second\n"
    )
    assert _added_lines(diff) == {"a.txt": [(4, "++ b/not-a-header"), (5, "second")]}


def test_commit_msg_passes_a_plain_message(runner: CliRunner, tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(
        "Fix the parser\n\n# Please enter the commit message for your changes.\n",
        encoding="utf-8",
    )
    assert runner.invoke(cli, ["hook", "commit-msg", str(message)]).exit_code == 0

    message.write_text("Remove <PRIVYX_EMAIL_1> from the docs\n", encoding="utf-8")
    result = runner.invoke(cli, ["hook", "commit-msg", str(message)])

    assert result.exit_code == 1
    assert "commit message:1  placeholder <PRIVYX_EMAIL_1>" in result.output


def test_the_installed_hooks_guard_a_real_commit(runner: CliRunner, repo: Path) -> None:
    assert runner.invoke(cli, ["install-hook"]).exit_code == 0
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}

    _stage(repo, "ok.py", "print('hello')\n")
    assert _git(repo, "commit", "-q", "-m", "Add a greeting", env=env).returncode == 0

    _stage(repo, "bad.py", f'key = "{KEY}"\n')
    blocked = _git(repo, "commit", "-q", "-m", "Add a key", env=env)
    assert blocked.returncode != 0 and "bad.py:1  API_KEY" in blocked.stderr

    _git(repo, "reset", "-q", "bad.py")
    _stage(repo, "two.py", "print(2)\n")
    blocked = _git(repo, "commit", "-q", "-m", "Mail <PRIVYX_EMAIL_1>", env=env)
    assert blocked.returncode != 0 and "placeholder <PRIVYX_EMAIL_1>" in blocked.stderr


def test_commit_msg_checks_comment_lines_git_may_keep(runner: CliRunner, tmp_path: Path) -> None:
    # `git commit -m` keeps lines starting with #; the hook cannot tell the cleanup mode.
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("# <PRIVYX_EMAIL_1>\n", encoding="utf-8")

    result = runner.invoke(cli, ["hook", "commit-msg", str(message)])

    assert result.exit_code == 1
    assert "commit message:1  placeholder <PRIVYX_EMAIL_1>" in result.output


def test_a_real_commit_with_a_hash_line_is_blocked(runner: CliRunner, repo: Path) -> None:
    assert runner.invoke(cli, ["install-hook"]).exit_code == 0
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}

    blocked = _git(repo, "commit", "--allow-empty", "-q", "-m", "# <PRIVYX_EMAIL_1>", env=env)

    assert blocked.returncode != 0
    assert _git(repo, "log", "--oneline").returncode != 0  # nothing was committed


def test_pre_commit_reads_the_content_not_a_textconv_view(runner: CliRunner, repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.cfg diff=hide\n", encoding="utf-8")
    _git(repo, "config", "diff.hide.textconv", "sed s/./x/g")
    _stage(repo, "app.cfg", f"key = {KEY}\n")

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert result.exit_code == 1
    assert "app.cfg:1  API_KEY" in result.output


def test_pre_commit_checks_a_file_git_marks_as_binary(runner: CliRunner, repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.cfg -diff\n", encoding="utf-8")
    _stage(repo, "app.cfg", f"x = 1\nkey = {KEY}\n")

    result = runner.invoke(cli, ["hook", "pre-commit"])

    assert result.exit_code == 1
    assert "app.cfg:2  API_KEY" in result.output


def test_added_lines_keep_header_shaped_content_when_the_count_matches() -> None:
    # A removed `-- note` and an added `++ x` at the end of a hunk, then the
    # next hunk: content, not a header naming file `x`.
    diff = (
        "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
        "@@ -1 +1 @@\n--- note\n+++ x\n@@ -5,0 +6 @@\n+select 1;\n"
    )
    assert _added_lines(diff) == {"q.sql": [(1, "++ x"), (6, "select 1;")]}
