"""Rich rendering helpers for the CLI.

Kept separate from command wiring so the visual language (colors, table style,
prompt phrasing) lives in one place. Nothing here touches the library's
filesystem logic; it only formats value objects and reads user input.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from .models import (
    CopyStatus,
    DoctorReport,
    HomeRef,
    Profile,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)

console = Console()
err_console = Console(stderr=True)


def success(message: str) -> None:
    console.print(f"[green]✓[/] {message}")


def info(message: str) -> None:
    console.print(f"[cyan]•[/] {message}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/] {message}")


def error(message: str) -> None:
    err_console.print(f"[bold red]Error:[/] {message}")


def heading(text: str) -> None:
    """A lightweight section header: bold title with a rule underneath."""
    console.print(f"\n[bold]{text}[/]")
    console.print("[dim]" + "─" * min(len(text), 48) + "[/]")


def render_profiles(profiles: list[Profile]) -> None:
    if not profiles:
        console.print("[dim](no profiles yet)[/]")
        return

    name_width = max(len(p.name) for p in profiles)
    heading("Codex profiles")
    for profile in profiles:
        marker, marker_style = (
            ("shared", "magenta")
            if profile.sessions_shared
            else ("isolated", "green")
        )
        line = Text()
        line.append("● ", style=marker_style)
        line.append(profile.name.ljust(name_width), style="bold")
        line.append(f"  {marker:<8}", style=marker_style)
        console.print(line)
        console.print(f"  [dim]{profile.path}[/]")


def render_sessions(sessions: list[SessionFile], home_label: str, limit: int = 20) -> None:
    shown = sessions[:limit]
    num_width = len(str(len(shown)))
    heading(f"Recent sessions · {home_label}")
    for idx, sf in enumerate(shown, start=1):
        line = Text()
        line.append(f"{str(idx).rjust(num_width)}. ", style="cyan")
        line.append(sf.session_id, style="bold")
        console.print(line)
        console.print(f"{' ' * (num_width + 2)}[dim]{sf.relative_path}[/]")
    if len(sessions) > limit:
        console.print(
            f"[dim]… {len(sessions) - limit} more. Enter a full session id to "
            "pick one outside this list.[/]"
        )


def render_copy_results(results: list[SessionCopyResult]) -> None:
    if not results:
        info("No sessions found.")
        return
    copied = sum(1 for r in results if r.status is CopyStatus.COPIED)
    skipped = len(results) - copied
    for r in results:
        if r.status is CopyStatus.COPIED:
            success(f"Copied session {r.session_id}")
        else:
            console.print(f"[dim]∘ already present, skipped {r.session_id}[/]")
    info(f"Done: {copied} copied, {skipped} skipped.")


def render_fix_result(result: SessionFixResult) -> None:
    old = ", ".join(result.previous_providers) or "none"
    if not result.changed_fields and not result.state_changed:
        info(f"Session {result.session_id} already uses provider '{result.provider}'.")
        return
    action = "Would update" if result.dry_run else "Updated"
    if result.changed_fields:
        success(
            f"{action} {result.changed_fields} JSONL provider field(s) in "
            f"{result.changed_records} record(s): {old} -> {result.provider}"
        )
    if result.state_changed:
        success(f"{action} provider in SQLite thread state -> {result.provider}")
    if result.backup_path is not None:
        info(f"JSONL backup: {result.backup_path}")
    if result.state_backup_path is not None:
        info(f"SQLite backup: {result.state_backup_path}")


def render_doctor(report: DoctorReport) -> None:
    if report.bin_on_path:
        path_value = Text("yes", style="green")
    else:
        path_value = Text("no", style="yellow")
    if report.codex_present:
        codex_value = Text.assemble(("yes", "green"), f" ({report.codex_path})")
    else:
        codex_value = Text("no", style="red")

    rows: list[tuple[str, Text | str]] = [
        ("codex cmd", report.codex_cmd),
        ("source home", str(report.source_home)),
        ("profile root", str(report.profile_root)),
        ("bin dir", str(report.bin_dir)),
        ("manager bin", report.manager_bin_name),
        ("bin on PATH", path_value),
        ("codex present", codex_value),
    ]
    key_width = max(len(key) for key, _ in rows)

    heading("codexalias doctor")
    for key, value in rows:
        line = Text()
        line.append(key.ljust(key_width) + "  ", style="bold cyan")
        line.append(value if isinstance(value, Text) else Text(value))
        console.print(line)
    if not report.bin_on_path:
        warn(f"{report.bin_dir} is not on PATH; wrappers won't be found until it is.")
    if not report.codex_present:
        warn(f"'{report.codex_cmd}' not found on PATH.")


def choose(prompt: str, options: list[tuple[str, str]]) -> str:
    """Numbered single-choice picker. ``options`` is (value, label); returns value."""
    for idx, (_, label) in enumerate(options, start=1):
        console.print(f"  [cyan]{idx}[/]. {label}")
    default = "1"
    while True:
        raw = Prompt.ask(prompt, default=default)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        warn("Please enter a valid number.")


def confirm(question: str, default: bool = False) -> bool:
    return Confirm.ask(question, default=default)


def home_ref_label(ref: HomeRef) -> str:
    return ref.label
