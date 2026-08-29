from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_alias import ProfileNotFoundError
from codex_alias.doctor_service import DoctorService
from codex_alias.home_service import HomeResolver
from codex_alias.launcher import ProfileLauncher
from codex_alias.models import HomeKind, HomeRef
from codex_alias.profile_service import ProfileStore
from codex_alias.session_service import SessionService
from codex_alias.sharing_service import SessionSharingService
from codex_alias.sync_service import SyncService


def test_profile_store_owns_home_and_wrapper_lifecycle(config) -> None:
    store = ProfileStore(config)

    wrapper = store.add_profile("work")

    assert wrapper == config.bin_dir / "codex-work"
    assert config.profile_path("work").is_dir()
    assert store.list_profiles()[0].name == "work"

    result = store.remove_profile(
        "work",
        source_home=config.source_home,
        current_home=config.source_home,
        keep_data=True,
    )

    assert result.wrapper_removed is True
    assert result.home_removed is False
    assert config.profile_path("work").is_dir()


def test_profile_launcher_is_independent_from_profile_store(config, monkeypatch) -> None:
    launcher = ProfileLauncher(config)
    monkeypatch.delenv("SHELL", raising=False)
    config.profile_path("work").mkdir(parents=True)

    argv, env = launcher.run_argv("work", ["--version"])

    assert argv == ["codex", "--version"]
    assert env["CODEX_HOME"] == str(config.profile_path("work"))


def test_profile_launcher_rejects_missing_profile(config) -> None:
    with pytest.raises(ProfileNotFoundError):
        ProfileLauncher(config).run_argv("work", ["--version"])

    assert not config.profile_path("work").exists()


def test_home_resolver_keeps_reference_rules_out_of_profile_store(
    config, monkeypatch
) -> None:
    store = ProfileStore(config)
    store.add_profile("work")
    resolver = HomeResolver(config, store.list_profiles)
    config.source_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    assert resolver.resolve_home_ref("@source").path == config.source_home.resolve()
    assert resolver.resolve_home_ref("work").profile == "work"
    assert resolver.candidate_source_homes(config.profile_path("work"))[0].path == (
        config.source_home.resolve()
    )


def test_session_service_can_be_used_without_manager_facade(config) -> None:
    store = ProfileStore(config)
    store.add_profile("work")
    source = config.source_home
    session_dir = source / "sessions" / "2026" / "08" / "26"
    session_dir.mkdir(parents=True)
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    (session_dir / f"rollout-{session_id}.jsonl").write_text(
        "line\n", encoding="utf-8"
    )

    service = SessionService(
        config,
        default_source_home=lambda: source,
        profiles=store.list_profiles,
    )

    assert [item.session_id for item in service.list_sessions(source)] == [session_id]


def test_sync_service_owns_plan_execution(config) -> None:
    store = ProfileStore(config)
    store.add_profile("work")
    (config.source_home / "rules").mkdir(parents=True)
    (config.source_home / "rules" / "default.rules").write_text(
        "rule\n", encoding="utf-8"
    )
    events: list[tuple[str, str]] = []

    facade = SimpleNamespace(
        config=config,
        profile_home=store.profile_home,
        profile_sync_types=lambda _profile: (),
        profile_skill_sync_options=lambda _profile: None,
    )
    service = SyncService(facade, emit=lambda level, text: events.append((level, text)))
    service.sync_profile("work", sync_types=("rules",), yes=True)

    assert (config.profile_path("work") / "rules" / "default.rules").is_file()
    assert events[0] == ("info", "Syncing rules for profile 'work' ...")


def test_session_sharing_and_doctor_are_standalone_services(config) -> None:
    store = ProfileStore(config)
    store.add_profile("work")
    source = config.source_home
    (source / "sessions").mkdir(parents=True)
    sharing = SessionSharingService(
        config,
        profile_home=store.profile_home,
        resolve_home_ref=lambda _ref: HomeRef(source, HomeKind.SOURCE),
    )

    sharing.share_sessions("work")

    assert (config.profile_path("work") / "sessions").is_symlink()
    assert DoctorService(config).report().profile_root == config.profile_root
