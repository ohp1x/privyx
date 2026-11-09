"""`privyx scan` and `privyx run --harvest`: what a workspace tells, masked."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli import workspace
from privyx.cli.main import cli
from privyx.config.loader import is_trusted, load_config
from privyx.core.context import Context
from privyx.privacy.detector.yaml import build_detector

DSN = "https://abc123@o1.ingest.example.test/42"
STRIPE = "not-a-real-" + "secret-42"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Ana Example", "-c", "user.email=a@example.com",
         *args],
        check=True,
        capture_output=True,
    )  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a history, a remote holding a credential, a manifest, and a .env."""
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    _git(
        root,
        "remote",
        "add",
        "origin",
        "https://bob:hunter22@git.example.test/acme-labs/billing.git",
    )
    _git(
        root,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init\n\nCo-authored-by: Budi Contoh <b@example.com>",
    )
    _git(root, "-c", "user.name=dependabot[bot]", "commit", "-q", "--allow-empty", "-m", "bump")
    (root / "package.json").write_text('{"name": "@acme-labs/portal"}', encoding="utf-8")
    (root / ".env").write_text(
        f"SENTRY_DSN={DSN}\nDEBUG=true\nPORT=3000\nexport STRIPE_SECRET_KEY='{STRIPE}'\n"
        "APP_NAME=acme-portal # shown in the title\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text("EXAMPLE_ONLY=some-example-value\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def test_workspace_finds_people_owners_and_projects(repo: Path) -> None:
    found = {(f.entity, f.value) for f in workspace.unique(
        workspace.git_terms(repo) + workspace.package_terms(repo)
    )}  # fmt: skip

    assert found == {
        ("PERSON", "Ana Example"),
        ("PERSON", "Budi Contoh"),
        ("ORGANIZATION", "acme-labs"),
        ("PROJECT", "billing"),
        ("PROJECT", "portal"),
    }


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("git@github.com:acme/app.git", "acme/app.git"),
        ("ssh://git@host.example:2222/group/sub/app.git", "/group/sub/app.git"),
        ("https://user:secret@host.example/acme/app", "/acme/app"),
        ("/srv/git/app.git", ""),
        ("file:///srv/git/app.git", ""),
        ("C:\\repos\\app", ""),
    ],
)
def test_a_remote_gives_its_path_never_its_credentials(url: str, path: str) -> None:
    assert workspace._url_path(url) == path


def test_env_values_skip_examples_numbers_and_plain_words(repo: Path) -> None:
    assert workspace.env_values(repo) == [
        ("SENTRY_DSN", DSN, ".env"),
        ("STRIPE_SECRET_KEY", STRIPE, ".env"),
        ("APP_NAME", "acme-portal", ".env"),
    ]


def test_scan_writes_terms_and_env_patterns_but_no_env_value(runner: CliRunner, repo: Path) -> None:
    result = runner.invoke(cli, ["scan", "--yes"])

    assert result.exit_code == 0, result.output
    written = (repo / "privyx.d" / "terms.yaml").read_text(encoding="utf-8")
    assert "Ana Example" in written and "acme-labs" in written
    assert "ENV_SENTRY_DSN" in written and "ENV_APP_NAME" in written
    # The built-in detector already masks a value assigned to a *_SECRET_KEY.
    assert "STRIPE_SECRET_KEY" not in written
    for secret in (DSN, STRIPE, "acme-portal", "hunter22", "dependabot"):
        assert secret not in written
    assert (repo / "privyx.yaml").read_text(encoding="utf-8") == "include: [privyx.d/terms.yaml]\n"
    # Written wholly here, so trusted: the terms apply right away.
    assert is_trusted(repo / "privyx.yaml")
    text = f"SENTRY_DSN={DSN} from Budi Contoh at ACME-LABS"
    detector = build_detector(load_config().detector.model_dump())  # type: ignore[union-attr]
    spans = asyncio.run(detector.detect(text, Context())).merged(text).spans
    assert {(s.entity_type, s.text) for s in spans} >= {
        ("ENV_SENTRY_DSN", DSN),
        ("PERSON", "Budi Contoh"),
        ("ORGANIZATION", "ACME-LABS"),
    }


def test_scan_twice_leaves_the_files_unchanged(runner: CliRunner, repo: Path) -> None:
    runner.invoke(cli, ["scan", "--yes"])
    files = [repo / "privyx.yaml", repo / "privyx.d" / "terms.yaml"]
    before = [f.read_bytes() for f in files]

    again = runner.invoke(cli, ["scan", "--yes"])

    assert again.exit_code == 0
    assert "Nothing new" in again.output
    assert [f.read_bytes() for f in files] == before
    assert is_trusted(repo / "privyx.yaml")


def test_scan_adds_only_what_is_kept(runner: CliRunner, repo: Path) -> None:
    listed = runner.invoke(cli, ["scan", "--dry-run"]).output
    lines = [line for line in listed.splitlines() if line.strip()[:1].isdigit()]
    skip = [line.split()[0] for line in lines if "PERSON" in line]

    result = runner.invoke(cli, ["scan"], input="9999\n" + ",".join(skip) + "\n")

    assert "is not a number from 1 to" in result.output
    written = (repo / "privyx.d" / "terms.yaml").read_text(encoding="utf-8")
    assert "PERSON" not in written and "acme-labs" in written
    # What was left out is offered again.
    assert "Budi Contoh" in runner.invoke(cli, ["scan", "--dry-run"]).output


def test_scan_dry_run_writes_nothing(runner: CliRunner, repo: Path) -> None:
    result = runner.invoke(cli, ["scan", "--dry-run"])

    assert "Ana Example" in result.output and "value of SENTRY_DSN" in result.output
    assert DSN not in result.output
    assert not (repo / "privyx.d").exists() and not (repo / "privyx.yaml").exists()


def test_scan_leaves_a_config_that_came_with_the_project_untrusted(
    runner: CliRunner, repo: Path
) -> None:
    config = repo / "privyx.yaml"
    config.write_text("port: 9001\n", encoding="utf-8")

    result = runner.invoke(cli, ["scan", "--yes"])

    assert config.read_text(encoding="utf-8") == "port: 9001\n"
    assert "Add privyx.d/terms.yaml to the include list" in result.output
    assert "Run `privyx trust`" in result.output
    assert not is_trusted(config)


def test_scan_keeps_a_trusted_config_trusted(runner: CliRunner, repo: Path) -> None:
    config = repo / "privyx.yaml"
    config.write_text("include: [privyx.d/*.yaml]\nport: 9001\n", encoding="utf-8")
    assert runner.invoke(cli, ["trust"]).exit_code == 0

    result = runner.invoke(cli, ["scan", "--yes"])

    assert "include list" not in result.output and "privyx trust" not in result.output
    assert is_trusted(config)
    assert load_config().port == 9001


def test_harvest_masks_the_workspace_for_one_run(runner: CliRunner, repo: Path) -> None:
    from privyx.cli.commands.run import _harvest

    settings = load_config()
    summary = _harvest(settings)

    assert summary == "1 ORGANIZATION, 2 PERSON, 1 PROJECT, 3 .env value(s)"
    assert isinstance(settings.detector, list)
    detector = build_detector([d.model_dump() for d in settings.detector])
    text = f"Budi Contoh pasted {DSN} and acme-portal"
    spans = asyncio.run(detector.detect(text, Context())).merged(text).spans
    assert {s.text for s in spans} >= {"Budi Contoh", DSN, "acme-portal"}
    assert not (repo / "privyx.d").exists()


def test_harvest_is_opt_in_and_shows_no_value(runner: CliRunner, repo: Path) -> None:
    plain = runner.invoke(cli, ["run", "--dry-run", "claude"])
    assert "Harvest" not in plain.output

    result = runner.invoke(cli, ["run", "--harvest", "--dry-run", "claude"])

    assert result.exit_code == 0, result.output
    assert "Harvest: 1 ORGANIZATION, 2 PERSON, 1 PROJECT, 3 .env value(s)" in result.output
    for value in (DSN, STRIPE, "acme-portal", "Budi Contoh"):
        assert value not in result.output


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_a_quoted_env_value_with_a_comment_loses_its_quotes(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "CUSTOM_VALUE=\"abc-123456\" # the id\nOTHER='x # y-123'\n", encoding="utf-8"
    )

    assert workspace.env_values(tmp_path) == [
        ("CUSTOM_VALUE", "abc-123456", ".env"),
        ("OTHER", "x # y-123", ".env"),
    ]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('CUSTOM_VALUE="alpha beta, gamma-123"', "alpha beta, gamma-123"),
        ("CUSTOM_VALUE: 'alpha beta;123'", "alpha beta;123"),
        ('"CUSTOM_VALUE": "alpha \\"beta\\" 123"', 'alpha \\"beta\\" 123'),
        ("export CUSTOM_VALUE=alpha-123 # note", "alpha-123"),
    ],
)
def test_the_scan_pattern_masks_the_whole_value(text: str, value: str) -> None:
    import re

    from privyx.cli.commands.scan import env_pattern
    from privyx.privacy.detector.yaml import backtracks

    pattern = env_pattern("CUSTOM_VALUE")
    match = re.search(pattern, text)

    assert match is not None and match["value"] == value
    assert not backtracks(pattern)


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ('CUSTOM_VALUE="abc-123\\"tail-456" # comment', 'abc-123"tail-456'),
        ('CUSTOM_VALUE="back\\\\slash-1"', "back\\slash-1"),
        ('CUSTOM_VALUE="line-1\\nline-2"', "line-1\nline-2"),
        ("CUSTOM_VALUE='it\\'s-123' # comment", "it's-123"),
        ("CUSTOM_VALUE='raw\\n-123'", "raw\\n-123"),
        ('CUSTOM_VALUE="abc\\q-123456"', "abc\\q-123456"),
        ("CUSTOM_VALUE='a\\\\b\\q-123'", "a\\b\\q-123"),
    ],
)
def test_env_values_decode_escapes_in_quotes(tmp_path: Path, line: str, value: str) -> None:
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")

    assert workspace.env_values(tmp_path) == [("CUSTOM_VALUE", value, ".env")]


def test_harvest_masks_a_value_with_an_escaped_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from privyx.cli.commands.run import _harvest

    (tmp_path / ".env").write_text('CUSTOM_VALUE="abc-123\\"tail-456" # x\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = load_config()
    _harvest(settings)

    detector = build_detector([d.model_dump() for d in settings.detector])  # type: ignore[union-attr]
    text = 'it is abc-123"tail-456 here'
    spans = asyncio.run(detector.detect(text, Context())).merged(text).spans
    assert [s.text for s in spans] == ['abc-123"tail-456']


def test_harvest_masks_a_value_with_an_unknown_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from privyx.cli.commands.run import _harvest

    (tmp_path / ".env").write_text('CUSTOM_VALUE="abc\\q-123456"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = load_config()
    _harvest(settings)

    detector = build_detector([d.model_dump() for d in settings.detector])  # type: ignore[union-attr]
    text = "it is abc\\q-123456 here"
    spans = asyncio.run(detector.detect(text, Context())).merged(text).spans
    assert [s.text for s in spans] == ["abc\\q-123456"]
