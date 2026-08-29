from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_alias import (
    Config,
    RelayConfig,
    RelayConfigError,
    RelayLaunch,
    RelayService,
    RelayState,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        profile_root=tmp_path / "profiles",
        bin_dir=tmp_path / "bin",
        codex_cmd="codex",
        source_home=tmp_path / "source",
        manager_bin_name="codexalias",
    )


def _write_relay(home: Path, *, port: int | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "test-key"}),
        encoding="utf-8",
    )
    lines = ['upstream = "https://provider.example/v1"']
    if port is not None:
        lines.append(f"port = {port}")
    (home / "relay.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_relay_config_reads_profile_file(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    _write_relay(home, port=4446)

    config = RelayConfig.from_home(home)

    assert config is not None
    assert config.upstream == "https://provider.example/v1"
    assert config.port == 4446
    assert config.api_key_file == Path("auth.json")
    assert config.provider is None


def test_relay_config_can_be_disabled(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / "relay.toml").write_text("enabled = false\n", encoding="utf-8")

    config = RelayConfig.from_home(home)

    assert config is not None
    assert config.enabled is False


def test_relay_config_rejects_missing_upstream(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / "relay.toml").write_text("port = 4446\n", encoding="utf-8")

    with pytest.raises(RelayConfigError, match="upstream"):
        RelayConfig.from_home(home)


def test_prepare_injects_local_responses_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profile"
    _write_relay(home)
    service = RelayService(_config(tmp_path))
    state = RelayState(
        pid=1234,
        port=4567,
        host="127.0.0.1",
        fingerprint="fingerprint",
        started_at=1.0,
    )
    monkeypatch.setattr(service, "_ensure_started", lambda *_args: state)

    launch = service.prepare(home)

    assert launch.status is not None
    assert launch.status.running
    assert launch.environment == {"OPENAI_API_KEY": "test-key"}
    assert launch.codex_args == (
        "-c",
        'model_provider="codexalias_relay"',
        "-c",
        'model_providers.codexalias_relay.name="codex-relay"',
        "-c",
        'model_providers.codexalias_relay.base_url="http://127.0.0.1:4567/v1"',
        "-c",
        'model_providers.codexalias_relay.wire_api="responses"',
        "-c",
        'model_providers.codexalias_relay.env_key="OPENAI_API_KEY"',
    )


def test_prepare_reuses_active_custom_provider_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profile"
    _write_relay(home)
    (home / "config.toml").write_text(
        'model_provider = "custom"\n\n[model_providers.custom]\n'
        'base_url = "https://provider.example/v1"\n',
        encoding="utf-8",
    )
    service = RelayService(_config(tmp_path))
    state = RelayState(1234, 4567, "127.0.0.1", "fingerprint", 1.0)
    monkeypatch.setattr(service, "_ensure_started", lambda *_args: state)

    launch = service.prepare(home)

    assert 'model_provider="custom"' in launch.codex_args
    assert any(
        value.startswith("model_providers.custom.base_url=")
        for value in launch.codex_args
    )


def test_prepare_without_relay_is_a_noop(tmp_path: Path) -> None:
    service = RelayService(_config(tmp_path))

    launch = service.prepare(tmp_path / "profile")

    assert launch == RelayLaunch((), {}, None)


def test_start_persists_state_without_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profile"
    _write_relay(home, port=4446)
    config = _config(tmp_path)
    service = RelayService(config)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return FakeProcess()

    monkeypatch.setattr("codex_alias.relay.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "codex_alias.relay.shutil.which",
        lambda _name: "/usr/local/bin/codex-relay",
    )
    monkeypatch.setattr("codex_alias.relay._pid_alive", lambda _pid: True)
    monkeypatch.setattr(service, "_ready", lambda *_args, **_kwargs: True)

    status = service.start(home)

    assert status.running
    assert captured["args"] == ["/usr/local/bin/codex-relay"]
    env = captured["kwargs"]["env"]
    assert env["CODEX_RELAY_API_KEY"] == "test-key"
    state = json.loads(
        (home / ".codexalias-relay" / "state.json").read_text(encoding="utf-8")
    )
    assert state["pid"] == 4321
    assert "test-key" not in json.dumps(state)


def test_prepare_reports_missing_key(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / "relay.toml").write_text(
        'upstream = "https://provider.example/v1"\n',
        encoding="utf-8",
    )
    service = RelayService(_config(tmp_path))

    with pytest.raises(RelayConfigError, match="API key"):
        service.prepare(home)


def test_session_shares_matching_relay_and_stops_after_last_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "profiles" / "first"
    second = tmp_path / "profiles" / "second"
    _write_relay(first, port=4446)
    _write_relay(second, port=4446)
    service = RelayService(_config(tmp_path))
    spawned: list[dict[str, object]] = []
    terminated: list[int] = []

    def fake_spawn(**kwargs):
        spawned.append(kwargs)
        relay_config = kwargs["relay_config"]
        return RelayState(
            4321,
            kwargs["port"],
            relay_config.host,
            kwargs["fingerprint"],
            1.0,
        )

    monkeypatch.setattr(service, "_spawn_relay", fake_spawn)
    monkeypatch.setattr(service, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("codex_alias.relay._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "codex_alias.relay._terminate_pid", lambda pid: terminated.append(pid)
    )

    with service.session(first) as first_launch:
        with service.session(second) as second_launch:
            assert len(spawned) == 1
            assert first_launch.codex_args == second_launch.codex_args
            registry = spawned[0]["state_dir"]
            leases = list((registry / "leases").glob("*.json"))
            assert len(leases) == 2
        assert terminated == []
        assert (registry / "state.json").exists()

    assert terminated == [4321]
    assert not (registry / "state.json").exists()
    assert not list((registry / "leases").glob("*.json"))


def test_session_keeps_different_upstreams_or_keys_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "profiles" / "first"
    second = tmp_path / "profiles" / "second"
    _write_relay(first)
    _write_relay(second)
    (second / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "different-key"}), encoding="utf-8"
    )
    (second / "relay.toml").write_text(
        'upstream = "https://another.example/v1"\n', encoding="utf-8"
    )
    service = RelayService(_config(tmp_path))
    spawned: list[dict[str, object]] = []
    terminated: list[int] = []

    def fake_spawn(**kwargs):
        spawned.append(kwargs)
        relay_config = kwargs["relay_config"]
        return RelayState(
            5000 + len(spawned),
            kwargs["port"],
            relay_config.host,
            kwargs["fingerprint"],
            1.0,
        )

    monkeypatch.setattr(service, "_spawn_relay", fake_spawn)
    monkeypatch.setattr(service, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("codex_alias.relay._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "codex_alias.relay._terminate_pid", lambda pid: terminated.append(pid)
    )

    with service.session(first), service.session(second):
        assert len(spawned) == 2
        assert spawned[0]["state_dir"] != spawned[1]["state_dir"]

    assert sorted(terminated) == [5001, 5002]


def test_manual_shared_pin_survives_alias_release_until_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "profiles" / "first"
    second = tmp_path / "profiles" / "second"
    _write_relay(first)
    _write_relay(second)
    service = RelayService(_config(tmp_path))
    spawned: list[dict[str, object]] = []
    terminated: list[int] = []

    def fake_spawn(**kwargs):
        spawned.append(kwargs)
        relay_config = kwargs["relay_config"]
        return RelayState(
            6000,
            kwargs["port"],
            relay_config.host,
            kwargs["fingerprint"],
            1.0,
        )

    monkeypatch.setattr(service, "_spawn_relay", fake_spawn)
    monkeypatch.setattr(service, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("codex_alias.relay._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "codex_alias.relay._terminate_pid", lambda pid: terminated.append(pid)
    )

    assert service.start(first).running
    assert service.start(second).running
    with service.session(first):
        pass
    assert terminated == []
    assert service.stop(first).running
    assert terminated == []
    assert service.stop(second).state == "stopped"
    assert terminated == [6000]
    assert len(spawned) == 1


def test_stop_uses_pointer_when_profile_config_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "profiles" / "first"
    second = tmp_path / "profiles" / "second"
    _write_relay(first)
    _write_relay(second)
    service = RelayService(_config(tmp_path))
    terminated: list[int] = []

    def fake_spawn(**kwargs):
        relay_config = kwargs["relay_config"]
        return RelayState(
            7000,
            kwargs["port"],
            relay_config.host,
            kwargs["fingerprint"],
            1.0,
        )

    monkeypatch.setattr(service, "_spawn_relay", fake_spawn)
    monkeypatch.setattr(service, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("codex_alias.relay._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "codex_alias.relay._terminate_pid", lambda pid: terminated.append(pid)
    )

    assert service.start(first).running
    assert service.start(second).running
    (first / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "changed-key"}), encoding="utf-8"
    )

    result = service.stop(first)

    assert result.running
    assert terminated == []
    assert service.status(second).running
