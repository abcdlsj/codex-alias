from __future__ import annotations

import json

from click.testing import CliRunner

from codex_alias.cli import cli
from conftest import write_session


SID = "019d1df0-8f1e-7393-b54a-0f0b511c5a33"


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


def test_profile_shortcut_forwards_codex_options(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("SHELL", raising=False)

    def fake_execvpe(file: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(file=file, argv=argv, env=env)

    monkeypatch.setattr("codex_alias.cli.os.execvpe", fake_execvpe)
    result = CliRunner().invoke(
        cli,
        [
            "luna-high",
            "--yolo",
            "--model",
            "gpt-5.6-sol",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["codex", "--yolo", "--model", "gpt-5.6-sol"]


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


def _env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "CODEXALIAS_SOURCE_HOME": str(tmp_path / ".codex"),
        "CODEXALIAS_PROFILE_ROOT": str(tmp_path / "profiles"),
        "CODEXALIAS_BIN_DIR": str(tmp_path / "bin"),
    }


def test_remove_deletes_profile_with_yes(tmp_path) -> None:
    env = _env(tmp_path)
    CliRunner().invoke(cli, ["add", "work", "--no-bootstrap"], env=env)

    result = CliRunner().invoke(cli, ["remove", "work", "--yes"], env=env)

    assert result.exit_code == 0, result.output
    assert "Removed wrapper" in result.output
    assert "Removed profile home" in result.output
    assert not (tmp_path / "profiles" / "work").exists()
    assert not (tmp_path / "bin" / "codex-work").exists()


def test_remove_keep_data_keeps_profile_home(tmp_path) -> None:
    env = _env(tmp_path)
    CliRunner().invoke(cli, ["add", "work", "--no-bootstrap"], env=env)

    result = CliRunner().invoke(cli, ["remove", "work", "--keep-data"], env=env)

    assert result.exit_code == 0, result.output
    assert "Removed wrapper" in result.output
    assert "Profile data kept" in result.output
    assert (tmp_path / "profiles" / "work").is_dir()
    assert not (tmp_path / "bin" / "codex-work").exists()


def test_remove_without_yes_requires_confirmation_non_interactive(tmp_path) -> None:
    env = _env(tmp_path)
    CliRunner().invoke(cli, ["add", "work", "--no-bootstrap"], env=env)

    result = CliRunner().invoke(cli, ["remove", "work"], env=env)

    assert result.exit_code != 0
    assert "--yes" in result.output
    assert (tmp_path / "profiles" / "work").is_dir()


def test_resume_can_fix_provider_and_model_before_launch(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    source = tmp_path / ".codex"
    write_session(
        source,
        SID,
        content=(
            json.dumps(
                {"type": "session_meta", "payload": {"id": SID, "model_provider": "old"}}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings",
                        "thread_settings": {
                            "model": "gpt-5.6-sol",
                            "model_provider_id": "old",
                        },
                    },
                }
            )
            + "\n"
        ),
    )
    target = tmp_path / "profiles" / "deepseek"
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        'model = "deepseek-v4-pro"\nmodel_provider = "deepseek"\n',
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_execvpe(file: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(file=file, argv=argv, env=env)

    monkeypatch.setattr("codex_alias.cli.os.execvpe", fake_execvpe)
    result = CliRunner().invoke(
        cli,
        ["resume", SID, "--profile", "deepseek", "--yolo"],
        input="y\n",
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(tmp_path / "profiles"),
        },
    )

    assert result.exit_code == 0, result.output
    assert captured["argv"][0:2] == ["codex", "resume"]
    assert captured["argv"][-1] == "--yolo"
    copied = next(target.glob("sessions/**/*.jsonl"))
    records = [json.loads(line) for line in copied.read_text().splitlines()]
    assert records[0]["payload"]["model_provider"] == "deepseek"
    assert records[1]["payload"]["thread_settings"] == {
        "model": "deepseek-v4-pro",
        "model_provider_id": "deepseek",
    }


def test_resume_uses_source_provider_for_builtin_auth_profile(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    source = tmp_path / ".codex"
    write_session(
        source,
        SID,
        content=json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SID, "model_provider": "openai"},
            }
        )
        + "\n",
    )
    target = tmp_path / "profiles" / "luna-high"
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n', encoding="utf-8"
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "codex_alias.cli.os.execvpe",
        lambda file, argv, env: captured.update(file=file, argv=argv, env=env),
    )
    result = CliRunner().invoke(
        cli,
        ["resume", SID, "--profile", "luna-high"],
        input="y\n",
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(tmp_path / "profiles"),
        },
    )

    assert result.exit_code == 0, result.output
    copied = next(target.glob("sessions/**/*.jsonl"))
    record = json.loads(copied.read_text(encoding="utf-8"))
    assert record["payload"]["model_provider"] == "openai"


def test_resume_skips_fix_when_declined(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    source = tmp_path / ".codex"
    write_session(
        source,
        SID,
        content=(
            json.dumps(
                {"type": "session_meta", "payload": {"id": SID, "model_provider": "old"}}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings",
                        "thread_settings": {
                            "model": "gpt-5.6-sol",
                            "model_provider_id": "old",
                        },
                    },
                }
            )
            + "\n"
        ),
    )
    target = tmp_path / "profiles" / "deepseek"
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        'model = "deepseek-v4-pro"\nmodel_provider = "deepseek"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr("codex_alias.cli.os.execvpe", lambda *args: None)
    result = CliRunner().invoke(
        cli,
        ["resume", SID, "--profile", "deepseek"],
        input="n\n",
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(tmp_path / "profiles"),
        },
    )

    assert result.exit_code == 0, result.output
    copied = next(target.glob("sessions/**/*.jsonl"))
    records = [json.loads(line) for line in copied.read_text().splitlines()]
    assert records[1]["payload"]["thread_settings"]["model"] == "gpt-5.6-sol"


def test_resume_confirms_before_lossy_history_mapping(tmp_path) -> None:
    source = tmp_path / ".codex"
    write_session(
        source,
        SID,
        content=(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": SID, "model_provider": "source_api"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "content": [],
                        "encrypted_content": "foreign-ciphertext",
                    },
                }
            )
            + "\n"
        ),
    )
    (source / "config.toml").write_text(
        "model_provider = \"source_api\"\n"
        "[model_providers.source_api]\n"
        "base_url = \"https://source.example/v1\"\n"
        "wire_api = \"responses\"\n",
        encoding="utf-8",
    )
    target = tmp_path / "profiles" / "target"
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        "model = \"gpt-5.6-sol\"\n"
        "model_provider = \"target_api\"\n"
        "[model_providers.target_api]\n"
        "base_url = \"https://target.example/v1\"\n"
        "wire_api = \"responses\"\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["resume", SID, "--profile", "target", "--no-launch"],
        input="n\ny\n",
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(tmp_path / "profiles"),
        },
    )

    assert result.exit_code == 0, result.output
    assert "Apply this lossy mapping?" in result.output
    copied = next(target.glob("sessions/**/*.jsonl"))
    records = [json.loads(line) for line in copied.read_text().splitlines()]
    assert records[1]["payload"]["encrypted_content"] is None
