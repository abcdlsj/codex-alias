"""Rich rendering helpers for the CLI.

Kept separate from command wiring so the visual language (colors, table style,
prompt phrasing) lives in one place. Nothing here touches the library's
filesystem logic; it only formats value objects and reads user input.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .models import (
    CopyStatus,
    DoctorReport,
    HomeRef,
    Profile,
    SessionCopyResult,
    SessionFile,
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


def render_profiles(profiles: list[Profile]) -> None:
    if not profiles:
        console.print("[dim](no profiles yet)[/]")
        return

    table = Table(title="Codex profiles", title_style="bold", header_style="bold cyan")
    table.add_column("Profile")
    table.add_column("Sessions")
    table.add_column("Home", style="dim")
    for profile in profiles:
        shared = (
            Text("shared", style="magenta")
            if profile.sessions_shared
            else Text("isolated", style="green")
        )
        table.add_row(profile.name, shared, str(profile.path))
    console.print(table)


def render_sessions(sessions: list[SessionFile], home_label: str, limit: int = 20) -> None:
    table = Table(
        title=f"Recent sessions · {home_label}",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Session ID")
    table.add_column("Path", style="dim")
    for idx, sf in enumerate(sessions[:limit], start=1):
        table.add_row(str(idx), sf.session_id, sf.relative_path)
    console.print(table)
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


def render_doctor(report: DoctorReport) -> None:
    table = Table(title="codexalias doctor", title_style="bold", show_header=False)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    table.add_row("codex cmd", report.codex_cmd)
    table.add_row("source home", str(report.source_home))
    table.add_row("profile root", str(report.profile_root))
    table.add_row("bin dir", str(report.bin_dir))
    table.add_row("manager bin", report.manager_bin_name)
    table.add_row(
        "bin on PATH",
        "[green]yes[/]" if report.bin_on_path else "[yellow]no[/]",
    )
    if report.codex_present:
        table.add_row("codex present", f"[green]yes[/] ({report.codex_path})")
    else:
        table.add_row("codex present", "[red]no[/]")
    console.print(table)
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
