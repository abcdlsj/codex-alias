"""codexalias — rich + click command line on top of the codex_alias library.

The library does the work and raises typed errors; this layer resolves refs,
renders results with rich, drives interactive prompts, and maps errors to a
clean exit. Command names and behaviour mirror the original shell tool.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from . import __version__, ui
from .config import Config
from .errors import CodexAliasError
from .manager import REF_CURRENT, REF_SOURCE, CodexAlias

def _mgr(ctx: click.Context) -> CodexAlias:
    return ctx.obj


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


def _bootstrap_profile(mgr: CodexAlias, profile_path: Path) -> None:
    """Interactive post-create setup, matching the shell tool's prompts."""
    if not sys.stdin.isatty():
        return

    source = mgr.config.source_home
    ui.info(f"Bootstrap from source home: {source}")

    if ui.confirm("Copy plugins/skills from source home?"):
        _copy_plugin_dirs(source, profile_path)
    if ui.confirm("Copy current config (auth.json + config.toml)?"):
        _copy_core_config(source, profile_path)
    if ui.confirm("Share sessions with root home (symlink)?"):
        for action in mgr._link_shared(profile_path, source):
            ui.success(action.message)
    elif ui.confirm("Migrate sessions into this profile?"):
        _interactive_migrate(mgr, profile_path)


_PLUGIN_DIRS = ("skills", ".skills", "plugins", ".plugins", ".agents", "agents", "mcp", ".mcp")
_CORE_CONFIG = ("auth.json", "config.toml")


def _copy_plugin_dirs(src: Path, dst: Path) -> None:
    import shutil

    copied = False
    for name in _PLUGIN_DIRS:
        src_dir = src / name
        if src_dir.is_dir():
            shutil.copytree(src_dir, dst / name, dirs_exist_ok=True)
            ui.success(f"Copied plugin dir: {name}")
            copied = True
    if not copied:
        ui.info(f"No plugin directories found in {src}.")


def _copy_core_config(src: Path, dst: Path) -> None:
    import shutil

    copied = False
    for name in _CORE_CONFIG:
        src_file = src / name
        if src_file.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dst / name)
            ui.success(f"Copied config file: {name}")
            copied = True
        else:
            ui.info(f"Missing config file, skipped: {src_file}")
    if not copied:
        ui.info("No config files copied.")


@click.group(
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


@cli.command()
@click.argument("session_id")
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
    ctx: click.Context, session_id: str, profile: str | None, no_launch: bool
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

    result = mgr.clone_session_for_profile(session_id, target_home)
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
    argv, env = mgr.resume_argv(target_home, result.session_id)
    os.execvpe(argv[0], argv, env)


@cli.command(name="list")
@click.pass_context
def list_(ctx: click.Context) -> None:
    """List profiles under the profile root."""
    ui.render_profiles(_mgr(ctx).list_profiles())


@cli.command()
@click.argument("profile")
@click.pass_context
def path(ctx: click.Context, profile: str) -> None:
    """Print the absolute home path of PROFILE."""
    ui.console.print(str(_mgr(ctx).profile_home(profile)))


@cli.command()
@click.argument("profile")
@click.argument("command_name", required=False)
@click.pass_context
def remove(ctx: click.Context, profile: str, command_name: str | None) -> None:
    """Remove a wrapper command (profile data is kept)."""
    target, removed = _mgr(ctx).remove_wrapper(profile, command_name)
    if removed:
        ui.success(f"Removed wrapper: {target}")
    else:
        ui.warn(f"Wrapper not found: {target}")


@cli.command(name="refresh-wrappers")
@click.pass_context
def refresh_wrappers(ctx: click.Context) -> None:
    """Regenerate default commands for all existing profiles."""
    targets = _mgr(ctx).refresh_wrappers()
    for target in targets:
        ui.success(f"Refreshed wrapper: {target}")
    if not targets:
        ui.info("No profiles found.")


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
    result = mgr.fix_session_provider(
        target_home,
        session_id,
        target_provider,
        model=model,
        from_provider=from_provider,
        dry_run=dry_run,
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
