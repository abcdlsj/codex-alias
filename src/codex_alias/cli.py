"""codexalias — rich + click command line on top of the codex_alias library.

The library does the work and raises typed errors; this layer resolves refs,
renders results with rich, drives interactive prompts, and maps errors to a
clean exit. Command names and behaviour mirror the original shell tool.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import click

from . import __version__, ui
from . import sync_service
from .config import Config
from .errors import CodexAliasError, SessionLossyMappingError
from .manager import REF_CURRENT, REF_SOURCE, CodexAlias


def _mgr(ctx: click.Context) -> CodexAlias:
    return ctx.obj


def _confirm_lossy_mapping(exc: SessionLossyMappingError, subject: str) -> None:
    mappings = ", ".join(exc.mappings)
    ui.warn(
        f"{subject} requires a lossy session mapping ({mappings}); "
        f"{exc.mapped_records} record(s) are affected."
    )
    if not ui.confirm("Apply this lossy mapping?", default=False):
        raise click.ClickException("lossy session mapping declined")


def _interactive_migrate(mgr: CodexAlias, target_home: Path) -> None:
    """Prompt-driven session migration into ``target_home``."""
    target_ref = mgr.describe_home(target_home)
    ui.info(f"Session migration target: {target_ref.label}")

    candidates = mgr.candidate_source_homes(target_home)
    if not candidates:
        ui.warn("No other source homes available for migration.")
        return

    source_value = ui.choose(
        "Choose a source home",
        [(str(ref.path), ref.label) for ref in candidates],
    )
    source_home = Path(source_value)

    mode = ui.choose(
        "Choose a migration mode",
        [("copy", "copy all sessions"), ("one", "copy one session")],
    )

    if mode == "copy":
        ui.render_copy_results(mgr.copy_all_sessions(source_home, target_home))
        return

    sessions = mgr.list_sessions(source_home)
    if not sessions:
        ui.warn(f"No sessions found in {mgr.describe_home(source_home).label}.")
        return
    ui.render_sessions(sessions, mgr.describe_home(source_home).label)
    raw = ui.console.input("Choose a session number or enter a session id: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= min(len(sessions), 20):
        query = sessions[int(raw) - 1].session_id
    else:
        query = raw
    result = mgr.copy_session_by_query(source_home, query, target_home)
    ui.render_copy_results([result])


def _choose_profile(mgr: CodexAlias) -> str | None:
    profiles = mgr.list_profiles()
    if not profiles:
        ui.info("No profiles found.")
        return None
    return ui.choose(
        "Choose a profile",
        [(profile.name, f"{profile.name} ({profile.path})") for profile in profiles],
    )


def _profile_payload(mgr: CodexAlias, profile) -> dict[str, object]:
    """Build a stable, machine-readable profile summary."""
    skill_options = mgr.profile_skill_sync_options(profile.name)
    if skill_options is not None:
        skill_options = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in skill_options.items()
        }
    return {
        "name": profile.name,
        "path": str(profile.path),
        "sessions_shared": profile.sessions_shared,
        "sync_types": list(mgr.profile_sync_types(profile.name)),
        "skill_sync": skill_options,
    }


def _configure_profile_hooks(mgr: CodexAlias, profile: str) -> None:
    source_path = mgr.root_hooks_path()
    options = mgr.profile_hook_options(profile)
    if not options:
        if source_path.is_file():
            ui.warn(f"No selectable hooks found in the root profile: {source_path}")
        else:
            ui.info(f"No root or plugin hooks found, skipped: {source_path}")
        return
    try:
        selected = ui.select_hooks(profile, source_path, options)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if selected is None:
        return
    if not ui.confirm(
        f"Write {len(selected)} selected root hook(s) to profile '{profile}'?",
        default=False,
    ):
        ui.warn("No hook changes written.")
        return
    ui.render_hook_sync_result(mgr.configure_profile_hooks(profile, selected))


class ProfileShortcutGroup(click.Group):
    """Allow ``codexa PROFILE [codex args...]`` as shorthand for ``run``."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args.insert(0, "run")
        return super().parse_args(ctx, args)


def _bootstrap_profile(mgr: CodexAlias, profile_path: Path) -> None:
    """Interactive post-create setup, matching the shell tool's prompts."""
    if not sys.stdin.isatty():
        return

    source = mgr.config.source_home
    ui.info(f"Bootstrap from source home: {source}")

    if ui.confirm("Copy plugins/skills/rules from source home?"):
        _copy_plugin_dirs(source, profile_path)
        mgr.record_profile_sync_type(profile_path.name, "plugins")
    if ui.confirm("Copy global instructions (AGENTS.md + AGENTS.override.md)?"):
        _copy_instruction_files(source, profile_path)
        mgr.record_profile_sync_type(profile_path.name, "instructions")
    if ui.confirm("Copy current config (auth.json + config.toml)?"):
        _copy_core_config(source, profile_path)
        mgr.record_profile_sync_type(profile_path.name, "config")
    _configure_profile_hooks(mgr, profile_path.name)
    if ui.confirm("Share sessions with root home (symlink)?"):
        for action in mgr.link_shared(profile_path, source):
            ui.success(action.message)
        mgr.record_profile_sync_type(profile_path.name, "sessions_shared")
    elif ui.confirm("Migrate sessions into this profile?"):
        _interactive_migrate(mgr, profile_path)
        mgr.record_profile_sync_type(profile_path.name, "sessions_migrate")


def _render_sync_messages(messages: tuple[sync_service.SyncMessage, ...]) -> None:
    renderers = {"info": ui.info, "success": ui.success, "warn": ui.warn}
    for message in messages:
        renderers[message.level](message.text)


def _copy_resource_dirs(
    src: Path,
    dst: Path,
    names: tuple[str, ...],
    *,
    dry_run: bool = False,
    label: str = "resource",
) -> None:
    _render_sync_messages(
        sync_service.copy_resource_dirs(src, dst, names, dry_run=dry_run, label=label)
    )


def _copy_plugin_dirs(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    """Copy the historical all-in-one plugin resource bundle."""
    _render_sync_messages(sync_service.copy_plugin_dirs(src, dst, dry_run=dry_run))


def _copy_plugin_only_dirs(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    _render_sync_messages(
        sync_service.copy_plugin_only_dirs(src, dst, dry_run=dry_run)
    )


def _copy_agents_dirs(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    _render_sync_messages(sync_service.copy_agents_dirs(src, dst, dry_run=dry_run))


def _copy_mcp_dirs(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    _render_sync_messages(sync_service.copy_mcp_dirs(src, dst, dry_run=dry_run))


def _skill_selector_name(value: str, option: str) -> str:
    try:
        return sync_service.validate_skill_name(value, option)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _read_skills_file(path: Path) -> tuple[str, ...]:
    try:
        return sync_service.read_skills_file(path)
    except (OSError, ValueError) as exc:
        message = (
            f"failed to read skills file {path}: {exc}"
            if isinstance(exc, OSError)
            else str(exc)
        )
        raise click.ClickException(message) from exc


def _source_skill_names(src: Path, *, include_system: bool = False) -> tuple[str, ...]:
    return sync_service.source_skill_names(src, include_system=include_system)


def _selected_skill_names(
    src: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    include_system: bool,
) -> tuple[str, ...]:
    try:
        return sync_service.selected_skill_names(
            src,
            include=include,
            exclude=exclude,
            include_system=include_system,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _copy_skills(
    src: Path,
    dst: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    include_system: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> None:
    try:
        messages = sync_service.copy_skills(
            src,
            dst,
            include=include,
            exclude=exclude,
            include_system=include_system,
            prune=prune,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_sync_messages(messages)


def _interactive_skill_selection(
    mgr: CodexAlias,
    profiles: list[str],
) -> tuple[tuple[str, ...], bool] | None:
    """Open the skill table once and return an allowlist for all targets."""
    profile = profiles[0]
    saved = mgr.profile_skill_sync_options(profile)
    include_system = bool(saved and saved.get("include_system", False))
    names = list(
        _source_skill_names(mgr.config.source_home, include_system=include_system)
    )
    selected = set(names)
    if saved is not None and saved.get("include"):
        selected = set(value for value in saved["include"] if value in names)
    if saved is not None:
        selected.difference_update(saved.get("exclude", ()))
    try:
        result = ui.select_skills(
            profile if len(profiles) == 1 else f"{len(profiles)} profiles",
            mgr.config.source_home,
            names,
            selected,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if result is None:
        return None
    return tuple(sorted(result)), include_system


def _copy_instruction_files(
    src: Path, dst: Path, *, dry_run: bool = False
) -> None:
    """Mirror global instruction files, including removal of stale overrides."""
    _render_sync_messages(
        sync_service.copy_instruction_files(src, dst, dry_run=dry_run)
    )


def _copy_core_config(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    _render_sync_messages(sync_service.copy_core_config(src, dst, dry_run=dry_run))


def _render_sync_event(level: str, text: str) -> None:
    renderers = {"info": ui.info, "success": ui.success, "warn": ui.warn}
    renderers[level](text)


def _confirm_sync(question: str) -> bool:
    if not sys.stdin.isatty():
        sync_type = question.removeprefix("Sync ").split(" into profile", 1)[0].lower()
        raise click.ClickException(
            f"syncing {sync_type} may overwrite profile files; "
            "run it from a TTY or pass --yes"
        )
    return ui.confirm(question, default=False)


def _sync_service(mgr: CodexAlias) -> sync_service.SyncService:
    """Build the sync coordinator with this CLI's presentation callbacks."""
    return sync_service.SyncService(
        mgr,
        emit=_render_sync_event,
        confirm=_confirm_sync,
        render_hook=ui.render_hook_sync_result,
        migrate_sessions=_interactive_migrate,
        copy_skills_callback=_copy_skills,
    )


def _compat_sync_handler(name: str):
    """Keep the old private handler names as thin service adapters."""
    def run(
        mgr: CodexAlias, profile_path: Path, *, dry_run: bool = False
    ) -> None:
        handler = _sync_service(mgr).migrations()[name]
        if name in {"hooks", "sessions_shared", "sessions_migrate"}:
            handler(mgr, profile_path)
        else:
            handler(mgr, profile_path, dry_run=dry_run)

    return run


_sync_skills = _compat_sync_handler("skills")
_sync_plugins = _compat_sync_handler("plugins")
_sync_legacy_bundle = _compat_sync_handler("bundle")
_sync_plugin_only_dirs = _compat_sync_handler("plugins-only")
_sync_agents = _compat_sync_handler("agents")
_sync_mcp = _compat_sync_handler("mcp")
_sync_rules = _compat_sync_handler("rules")
_sync_prompts = _compat_sync_handler("prompts")
_sync_instructions = _compat_sync_handler("instructions")
_sync_config = _compat_sync_handler("config")
_sync_hooks = _compat_sync_handler("hooks")
_sync_shared_sessions = _compat_sync_handler("sessions_shared")
_sync_migrated_sessions = _compat_sync_handler("sessions_migrate")

_SYNC_MIGRATIONS = {
    "skills": _sync_skills,
    "plugins": _sync_plugins,
    "bundle": _sync_legacy_bundle,
    "plugins-only": _sync_plugin_only_dirs,
    "agents": _sync_agents,
    "mcp": _sync_mcp,
    "rules": _sync_rules,
    "prompts": _sync_prompts,
    "instructions": _sync_instructions,
    "config": _sync_config,
    "hooks": _sync_hooks,
    "sessions_shared": _sync_shared_sessions,
    "sessions_migrate": _sync_migrated_sessions,
}

_SYNC_TYPE_DESCRIPTIONS = sync_service.SYNC_TYPE_DESCRIPTIONS
_SYNC_CONFIRM_TYPES = sync_service.SYNC_CONFIRM_TYPES
_SYNC_TYPE_CHOICES = sync_service.SYNC_TYPE_CHOICES


def _sync_profile(
    mgr: CodexAlias,
    profile: str,
    *,
    yes: bool = False,
    sync_types: tuple[str, ...] | None = None,
    skill_include: tuple[str, ...] = (),
    skill_exclude: tuple[str, ...] = (),
    include_system_skills: bool = False,
    prune_skills: bool = False,
    dry_run: bool = False,
    extra_sync_types: tuple[str, ...] = (),
) -> None:
    """Run explicit or saved migrations for PROFILE in the given order."""
    _sync_service(mgr).sync_profile(
        profile,
        yes=yes,
        sync_types=sync_types,
        skill_include=skill_include,
        skill_exclude=skill_exclude,
        include_system_skills=include_system_skills,
        prune_skills=prune_skills,
        dry_run=dry_run,
        extra_sync_types=extra_sync_types,
        # Keep this private mapping replaceable for integrations/tests that
        # used the old CLI hook, while each default entry delegates to the
        # service implementation.
        migrations=_SYNC_MIGRATIONS,
    )


def _persist_sync_configuration(
    mgr: CodexAlias,
    profile: str,
    *,
    sync_types: tuple[str, ...],
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    include_system_skills: bool,
    prune_skills: bool,
    persist: bool,
    instructions: bool,
) -> None:
    """Persist a successful sync plan after its filesystem work completes."""
    _sync_service(mgr).persist_sync_configuration(
        profile,
        sync_types=sync_types,
        includes=includes,
        excludes=excludes,
        include_system_skills=include_system_skills,
        prune_skills=prune_skills,
        persist=persist,
        instructions=instructions,
    )


@click.group(
    cls=ProfileShortcutGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(__version__, prog_name="codexalias")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """codexalias — run multiple Codex profiles with isolated homes."""
    ctx.obj = CodexAlias(Config.from_env())


@cli.command()
@click.argument("profile")
@click.argument("command_name", required=False)
@click.option("--no-bootstrap", is_flag=True, help="Skip interactive setup prompts.")
@click.pass_context
def add(ctx: click.Context, profile: str, command_name: str | None, no_bootstrap: bool) -> None:
    """Create a wrapper command for PROFILE (default: codex-<profile>)."""
    mgr = _mgr(ctx)
    target = mgr.add_profile(profile, command_name)
    profile_path = mgr.config.profile_path(profile)
    if not no_bootstrap:
        _bootstrap_profile(mgr, profile_path)
    ui.success(f"Created wrapper: {target}")
    ui.info(f"Profile home: {profile_path}")
    if not mgr.doctor().bin_on_path:
        ui.warn(f"{mgr.config.bin_dir} is not on PATH.")


@cli.command(
    context_settings={
        # Everything after PROFILE belongs to Codex.  In particular, do not
        # let Click reject Codex options that codexalias does not know about.
        # Disabling interspersed option parsing also forwards option names
        # shared with this command (for example ``--help``).
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    }
)
@click.argument("profile")
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run(ctx: click.Context, profile: str, codex_args: tuple[str, ...]) -> None:
    """Run codex once under PROFILE without creating a wrapper."""
    mgr = _mgr(ctx)
    argv, env = mgr.run_argv(profile, list(codex_args))
    os.execvpe(argv[0], argv, env)


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("session_id")
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--profile",
    help="Target profile name, or 'default'. Prompts with a list when omitted.",
)
@click.option(
    "--no-launch",
    is_flag=True,
    help="Create the copy but do not launch Codex.",
)
@click.pass_context
def resume(
    ctx: click.Context,
    session_id: str,
    codex_args: tuple[str, ...],
    profile: str | None,
    no_launch: bool,
) -> None:
    """Copy a session for a selected profile, then resume the copy."""
    mgr = _mgr(ctx)
    profiles = mgr.list_profiles()
    choices = [("default", f"default ({mgr.default_source_home()})")]
    choices.extend((item.name, f"{item.name} ({item.path})") for item in profiles)

    target_name = profile or ui.choose("Choose a profile", choices)
    known_names = {value for value, _ in choices}
    if target_name not in known_names:
        raise click.ClickException(f"unknown profile: {target_name}")

    if target_name == "default":
        target_home = mgr.default_source_home()
    else:
        target_home = mgr.profile_home(target_name, must_exist=True)
    target_label = next(label for value, label in choices if value == target_name)
    should_fix = ui.confirm("Fix session provider and model for this profile?")
    target_model = mgr.configured_model(target_home) if should_fix else None

    try:
        result = mgr.clone_session_for_profile(
            session_id, target_home, allow_lossy=False
        )
    except SessionLossyMappingError as exc:
        _confirm_lossy_mapping(exc, "Session resume")
        result = mgr.clone_session_for_profile(
            session_id, target_home, allow_lossy=True
        )
    ui.render_clone_result(result, target_label)
    if should_fix:
        fix_result = mgr.fix_session_provider(
            target_home,
            result.session_id,
            result.provider,
            model=target_model,
        )
        ui.render_fix_result(fix_result)
    if no_launch:
        return

    ui.info(f"Resuming copied session {result.session_id} ...")
    argv, env = mgr.resume_argv(target_home, result.session_id, list(codex_args))
    os.execvpe(argv[0], argv, env)


@cli.command(name="list")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print profile state as JSON for scripts and AI callers.",
)
@click.option(
    "--details",
    is_flag=True,
    help="Show saved sync types and skill selectors in human-readable output.",
)
@click.pass_context
def list_(ctx: click.Context, json_output: bool, details: bool) -> None:
    """List profiles under the profile root."""
    mgr = _mgr(ctx)
    profiles = mgr.list_profiles()
    if json_output:
        click.echo(json.dumps([_profile_payload(mgr, item) for item in profiles]))
        return
    ui.render_profiles(profiles)
    if details:
        for item in profiles:
            payload = _profile_payload(mgr, item)
            ui.console.print(
                f"  [dim]{item.name}: sync={','.join(payload['sync_types']) or '-'}[/]"
            )
            if payload["skill_sync"] is not None:
                ui.console.print(f"  [dim]skills={payload['skill_sync']}[/]")


@cli.command()
@click.argument("profile")
@click.pass_context
def path(ctx: click.Context, profile: str) -> None:
    """Print the absolute home path of PROFILE."""
    ui.console.print(str(_mgr(ctx).profile_home(profile)))


@cli.command()
@click.argument("profile")
@click.argument("command_name", required=False)
@click.option(
    "--keep-data",
    is_flag=True,
    help="Remove only the wrapper command; keep the profile home and data.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the confirmation before deleting profile data.",
)
@click.pass_context
def remove(
    ctx: click.Context,
    profile: str,
    command_name: str | None,
    keep_data: bool,
    yes: bool,
) -> None:
    """Remove PROFILE and its wrapper command (profile data is deleted)."""
    mgr = _mgr(ctx)
    profile_path = mgr.profile_home(profile, must_exist=False)
    if not keep_data and profile_path.is_dir() and not yes:
        if not sys.stdin.isatty():
            raise click.ClickException(
                "removing a profile deletes its data; pass --yes to confirm "
                "in a non-interactive shell"
            )
        if not ui.confirm(f"Delete profile data at {profile_path}?", default=False):
            ui.warn("Aborted; profile kept.")
            return

    result = mgr.remove_profile(profile, command_name, keep_data=keep_data)
    if result.wrapper_removed:
        ui.success(f"Removed wrapper: {result.wrapper_path}")
    else:
        ui.warn(f"Wrapper not found: {result.wrapper_path}")
    if result.home_removed:
        ui.success(f"Removed profile home: {result.profile_path}")
    elif keep_data:
        ui.info(f"Profile data kept: {result.profile_path}")


@cli.command(name="refresh-wrappers")
@click.pass_context
def refresh_wrappers(ctx: click.Context) -> None:
    """Regenerate default commands for all existing profiles."""
    targets = _mgr(ctx).refresh_wrappers()
    for target in targets:
        ui.success(f"Refreshed wrapper: {target}")
    if not targets:
        ui.info("No profiles found.")


@cli.command(name="hooks")
@click.pass_context
def hooks(ctx: click.Context) -> None:
    """Select which root hooks a profile should share."""
    if not sys.stdin.isatty():
        raise click.ClickException("hook selection requires a TTY")
    mgr = _mgr(ctx)
    profile = _choose_profile(mgr)
    if profile is not None:
        _configure_profile_hooks(mgr, profile)


@cli.command(name="sync")
@click.argument("profile", required=False)
@click.option(
    "--all-profiles",
    "--all",
    is_flag=True,
    help="Sync every profile instead of selecting one profile.",
)
@click.option(
    "--type",
    "sync_types",
    type=click.Choice(_SYNC_TYPE_CHOICES, case_sensitive=False),
    multiple=True,
    help="Run one sync type once; repeat to select multiple types.",
)
@click.option(
    "--source",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
    help="Source Codex home for this run (for example ~/.codex).",
)
@click.option(
    "--list-types",
    is_flag=True,
    help="List the available sync types and exit.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit list output as JSON (use with --list-types or --list-skills).",
)
@click.option(
    "--list-skills",
    is_flag=True,
    help="List selectable skills from the source home and exit.",
)
@click.option(
    "--select-skills",
    is_flag=True,
    help=(
        "Open a keyboard table, persist the allowlist, and remove unselected "
        "user skills."
    ),
)
@click.option(
    "--skill",
    "skills",
    multiple=True,
    help="Select one skill directory; repeat for an allowlist.",
)
@click.option(
    "--exclude-skill",
    "excluded_skills",
    multiple=True,
    help="Exclude one skill directory from the selection; repeat as needed.",
)
@click.option(
    "--skills-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read one skill name per line (blank lines and # comments ignored).",
)
@click.option(
    "--include-system-skills",
    is_flag=True,
    help="Include hidden/system skill directories such as .system.",
)
@click.option(
    "--prune-skills",
    is_flag=True,
    help="Remove non-selected user skills from each target profile.",
)
@click.option(
    "--save",
    "persist",
    is_flag=True,
    help="Persist explicit sync types and skill selectors in each profile.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show planned file changes without writing them.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmations for sync types that may overwrite profile files.",
)
@click.option(
    "--instructions",
    is_flag=True,
    help="Enable AGENTS.md/AGENTS.override.md sync for this profile.",
)
@click.pass_context
def sync(
    ctx: click.Context,
    profile: str | None,
    all_profiles: bool,
    sync_types: tuple[str, ...],
    source: Path | None,
    list_types: bool,
    json_output: bool,
    list_skills: bool,
    select_skills: bool,
    skills: tuple[str, ...],
    excluded_skills: tuple[str, ...],
    skills_file: Path | None,
    include_system_skills: bool,
    prune_skills: bool,
    persist: bool,
    dry_run: bool,
    yes: bool,
    instructions: bool,
) -> None:
    """Sync selected or saved content from a source home into profiles."""
    mgr = _mgr(ctx)

    if list_types:
        if json_output:
            click.echo(
                json.dumps(
                    [
                        {"name": name, "description": description}
                        for name, description in _SYNC_TYPE_DESCRIPTIONS.items()
                    ]
                )
            )
        else:
            for name, description in _SYNC_TYPE_DESCRIPTIONS.items():
                ui.console.print(f"{name:<18} {description}")
        return
    if json_output and not list_skills:
        raise click.UsageError("--json requires --list-types or --list-skills")
    if profile is not None and all_profiles:
        raise click.UsageError("PROFILE and --all-profiles cannot be used together")
    if source is not None:
        if not source.is_dir():
            raise click.ClickException(f"source home not found: {source}")
        mgr = CodexAlias(replace(mgr.config, source_home=source))

    includes = tuple(
        dict.fromkeys(
            _skill_selector_name(value, "--skill") for value in skills
        )
    )
    if skills_file is not None:
        includes = tuple(dict.fromkeys((*includes, *_read_skills_file(skills_file))))
    excludes = tuple(
        dict.fromkeys(
            _skill_selector_name(value, "--exclude-skill")
            for value in excluded_skills
        )
    )
    if list_skills:
        names = _source_skill_names(
            mgr.config.source_home, include_system=include_system_skills
        )
        if json_output:
            click.echo(json.dumps(list(names)))
        else:
            for name in names:
                ui.console.print(name)
        return

    if select_skills and (
        skills
        or excluded_skills
        or skills_file is not None
        or include_system_skills
        or dry_run
    ):
        raise click.UsageError(
            "--select-skills cannot be combined with skill filters, "
            "--include-system-skills, or --dry-run"
        )

    one_shot_types = tuple(dict.fromkeys(item.lower() for item in sync_types))
    if select_skills:
        if one_shot_types and one_shot_types != ("skills",):
            raise click.UsageError("--select-skills requires --type skills")
        one_shot_types = ("skills",)
    has_skill_selector = bool(
        includes or excludes or skills_file or include_system_skills or prune_skills
    )
    if has_skill_selector and not one_shot_types:
        one_shot_types = ("skills",)
    if any(item != "skills" for item in one_shot_types) and has_skill_selector:
        raise click.UsageError(
            "--skill/--exclude-skill/--prune-skills require --type skills"
        )
    if persist and not one_shot_types:
        raise click.UsageError("--save requires at least one explicit --type")
    if persist and dry_run:
        raise click.UsageError("--save cannot be combined with --dry-run")

    if all_profiles:
        selected_profiles = [item.name for item in mgr.list_profiles()]
        if not selected_profiles:
            ui.info("No profiles found.")
            return
    elif profile is not None:
        selected_profiles = [profile]
    else:
        if not sys.stdin.isatty():
            raise click.ClickException("sync without a profile requires a TTY")
        selected_profile = _choose_profile(mgr)
        if selected_profile is None:
            return
        selected_profiles = [selected_profile]

    # Resolve every target before changing any of them. This avoids a partial
    # multi-profile run when one of the requested profile names is invalid.
    for selected_profile in selected_profiles:
        mgr.profile_home(selected_profile, must_exist=True)

    if select_skills:
        selection = _interactive_skill_selection(mgr, selected_profiles)
        if selection is None:
            return
        selected, include_system_skills = selection
        includes = selected
        excludes = ()
        has_skill_selector = True
        prune_skills = True
        persist = True

    # Validate allowlist names before --save can touch any profile state.
    if "skills" in one_shot_types:
        _selected_skill_names(
            mgr.config.source_home,
            include=includes,
            exclude=excludes,
            include_system=include_system_skills,
        )

    completed_profiles: list[str] = []
    for selected_profile in selected_profiles:
        active_types = one_shot_types or mgr.profile_sync_types(selected_profile)
        extra_sync_types = (
            ("instructions",) if instructions and not one_shot_types else ()
        )
        if extra_sync_types:
            active_types = tuple(dict.fromkeys((*active_types, *extra_sync_types)))
        if has_skill_selector and "skills" not in active_types:
            raise click.UsageError(
                f"profile '{selected_profile}' has no skills sync type selected"
            )
        if prune_skills and "skills" not in active_types:
            raise click.UsageError(
                "--prune-skills requires an active skills sync type"
            )
        if dry_run and any(
            item in {"hooks", "sessions_shared", "sessions_migrate"}
            for item in active_types
        ):
            raise click.UsageError(
                "--dry-run only supports file/resource sync types, not hooks or sessions"
            )
        if "sessions_migrate" in active_types and not sys.stdin.isatty():
            raise click.ClickException(
                "sync with session migration requires a TTY"
            )
        _sync_profile(
            mgr,
            selected_profile,
            yes=yes,
            sync_types=one_shot_types or None,
            skill_include=includes,
            skill_exclude=excludes,
            include_system_skills=include_system_skills,
            prune_skills=prune_skills,
            dry_run=dry_run,
            extra_sync_types=extra_sync_types,
        )
        completed_profiles.append(selected_profile)

    if not dry_run and (instructions or persist):
        for selected_profile in completed_profiles:
            _persist_sync_configuration(
                mgr,
                selected_profile,
                sync_types=one_shot_types,
                includes=includes,
                excludes=excludes,
                include_system_skills=include_system_skills,
                prune_skills=prune_skills,
                persist=persist,
                instructions=instructions,
            )


@cli.command(name="import")
@click.argument("session_id")
@click.argument("target", default=REF_CURRENT)
@click.pass_context
def import_(ctx: click.Context, session_id: str, target: str) -> None:
    """Import one session from default ~/.codex into TARGET home/profile."""
    mgr = _mgr(ctx)
    target_home = mgr.resolve_home_ref(target).path
    result = mgr.import_session(session_id, target_home)
    ui.render_copy_results([result])


@cli.command(name="fix-session")
@click.argument("session_id")
@click.argument("home", default=REF_CURRENT)
@click.option(
    "--provider",
    help="Replacement provider (default: top-level model_provider in HOME/config.toml).",
)
@click.option(
    "--model",
    help="Replacement model.",
)
@click.option(
    "--from-provider",
    help="Only replace fields with this provider value.",
)
@click.option("--dry-run", is_flag=True, help="Validate and report without writing.")
@click.pass_context
def fix_session(
    ctx: click.Context,
    session_id: str,
    home: str,
    provider: str | None,
    model: str | None,
    from_provider: str | None,
    dry_run: bool,
) -> None:
    """Repair stale provider and optional model metadata in one Codex session."""
    mgr = _mgr(ctx)
    target_home = mgr.resolve_home_ref(home).path
    target_provider = provider or mgr.configured_model_provider(target_home)
    try:
        result = mgr.fix_session_provider(
            target_home,
            session_id,
            target_provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
            allow_lossy=dry_run,
        )
    except SessionLossyMappingError as exc:
        if not sys.stdin.isatty():
            raise click.ClickException(
                "lossy session mapping requires an interactive confirmation"
            ) from exc
        _confirm_lossy_mapping(exc, "Session repair")
        result = mgr.fix_session_provider(
            target_home,
            session_id,
            target_provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
            allow_lossy=True,
        )
    ui.render_fix_result(result)


@cli.group()
def migrate() -> None:
    """Migrate sessions between homes/profiles."""


@migrate.command(name="session")
@click.pass_context
def migrate_session(ctx: click.Context) -> None:
    """Interactive session migration into the current home."""
    mgr = _mgr(ctx)
    if not sys.stdin.isatty():
        raise click.ClickException("interactive session migration requires a TTY")
    _interactive_migrate(mgr, mgr.current_home())


@migrate.command(name="copy")
@click.argument("source", default=REF_SOURCE)
@click.argument("target", default=REF_CURRENT)
@click.pass_context
def migrate_copy(ctx: click.Context, source: str, target: str) -> None:
    """Copy all sessions from SOURCE into TARGET."""
    mgr = _mgr(ctx)
    src = mgr.resolve_home_ref(source).path
    dst = mgr.resolve_home_ref(target).path
    ui.render_copy_results(mgr.copy_all_sessions(src, dst))


@migrate.command(name="one")
@click.argument("source")
@click.argument("session_id")
@click.argument("target", default=REF_CURRENT)
@click.pass_context
def migrate_one(ctx: click.Context, source: str, session_id: str, target: str) -> None:
    """Copy one session (SESSION_ID) from SOURCE into TARGET."""
    mgr = _mgr(ctx)
    src = mgr.resolve_home_ref(source).path
    dst = mgr.resolve_home_ref(target).path
    ui.render_copy_results([mgr.copy_session_by_query(src, session_id, dst)])


@cli.command(name="share-sessions")
@click.argument("profile")
@click.argument("source", default=REF_SOURCE)
@click.pass_context
def share_sessions(ctx: click.Context, profile: str, source: str) -> None:
    """Symlink PROFILE's sessions/history/db to a SOURCE home."""
    mgr = _mgr(ctx)
    actions = mgr.share_sessions(profile, source)
    for action in actions:
        ui.success(action.message)
    ui.info(f"Profile '{profile}' now shares sessions with {mgr.resolve_home_ref(source).label}")


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Show environment and sanity checks."""
    ui.render_doctor(_mgr(ctx).doctor())


def main() -> None:
    try:
        cli()
    except CodexAliasError as exc:
        ui.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
