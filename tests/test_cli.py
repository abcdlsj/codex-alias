from __future__ import annotations

from click.testing import CliRunner

from codex_alias.cli import cli


def test_run_forwards_unknown_codex_options(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("SHELL", raising=False)

    def fake_execvpe(file: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(file=file, argv=argv, env=env)

    monkeypatch.setattr("codex_alias.cli.os.execvpe", fake_execvpe)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "cpa",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            "gpt-5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5",
    ]


def test_run_forwards_help_after_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("SHELL", raising=False)

    def fake_execvpe(file: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(file=file, argv=argv, env=env)

    monkeypatch.setattr("codex_alias.cli.os.execvpe", fake_execvpe)
    result = CliRunner().invoke(cli, ["run", "cpa", "--help"])

    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["codex", "--help"]


def test_run_help_before_profile_still_shows_manager_help() -> None:
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "Run codex once under PROFILE" in result.output
