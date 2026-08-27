"""Rich rendering helpers for the CLI.

Kept separate from command wiring so the visual language (colors, table style,
prompt phrasing) lives in one place. Nothing here touches the library's
filesystem logic; it only formats value objects and reads user input.
"""

from __future__ import annotations

import os
import select
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .models import (
    CopyStatus,
    DetectedSession,
    DoctorReport,
    HomeRef,
    HookOption,
    HookSyncResult,
    Profile,
    SessionCloneResult,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)

console = Console()
err_console = Console(stderr=True)


def success(message: str) -> None:
    console.print(f"[bold green]OK[/]  {message}")


def info(message: str) -> None:
    console.print(f"[bold cyan]INFO[/]  {message}")


def warn(message: str) -> None:
    console.print(f"[bold yellow]WARN[/]  {message}")


def error(message: str) -> None:
    err_console.print(f"[bold red]ERROR[/]  {message}")


def heading(text: str) -> None:
    """A lightweight section header: bold title with a rule underneath."""
    title = text.upper()
    console.print(f"\n[bold cyan]{title}[/]")
    console.print("[dim]" + "─" * min(len(title), 48) + "[/]")


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
    info(f"DONE  {copied} copied, {skipped} skipped.")


def render_fix_result(result: SessionFixResult) -> None:
    old = ", ".join(result.previous_providers) or "none"
    old_models = ", ".join(result.previous_models) or "none"
    if (
        not result.changed_fields
        and not result.changed_model_fields
        and not result.mapped_records
        and not result.state_changed
        and not result.mapping_warnings
    ):
        message = f"Session {result.session_id} already uses provider '{result.provider}'"
        if result.model is not None:
            message += f" and model '{result.model}'"
        info(message + ".")
        return
    action = "Would update" if result.dry_run else "Updated"
    if result.changed_fields:
        success(
            f"{action} {result.changed_fields} JSONL provider field(s) in "
            f"{result.changed_records} record(s): {old} -> {result.provider}"
        )
    if result.changed_model_fields:
        success(
            f"{action} {result.changed_model_fields} JSONL model field(s) in "
            f"{result.changed_records} record(s): {old_models} -> {result.model}"
        )
    if result.mapped_records:
        mappings = ", ".join(result.applied_mappings)
        success(
            f"{action} {result.mapped_records} JSONL response item(s) with "
            f"compatibility mapping(s): {mappings}"
        )
    if result.dropped_records:
        mappings = ", ".join(result.lossy_mappings)
        drop_action = "Would drop" if result.dry_run else "Dropped"
        warn(
            f"LOSSY  {drop_action} {result.dropped_records} "
            f"non-portable record(s): {mappings}"
        )
    elif result.lossy_mappings:
        mappings = ", ".join(result.lossy_mappings)
        clear_action = "Would clear" if result.dry_run else "Cleared"
        warn(f"LOSSY  {clear_action} backend-bound fields: {mappings}")
    for warning in result.mapping_warnings:
        warn(f"Session history diagnostic: {warning}")
    if result.state_changed:
        state_label = "provider"
        state_target = result.provider
        if result.model is not None:
            state_label = "provider/model"
            state_target += f", model -> {result.model}"
        success(f"{action} {state_label} in SQLite thread state -> {state_target}")
    if result.backup_path is not None:
        info(f"JSONL backup: {result.backup_path}")
    if result.state_backup_path is not None:
        info(f"SQLite backup: {result.state_backup_path}")


def render_clone_result(result: SessionCloneResult, target_label: str) -> None:
    heading("Session copied")
    success(f"{result.source_session_id} -> {result.session_id}")
    info(f"TARGET    {target_label}")
    info(f"PROVIDER  {result.provider}")
    if result.model is not None:
        info(f"MODEL     {result.model}")
    if result.mapped_records:
        mappings = ", ".join(result.applied_mappings)
        info(f"MAPPED    {result.mapped_records} record(s): {mappings}")
    if result.dropped_records:
        mappings = ", ".join(result.lossy_mappings)
        warn(
            f"LOSSY     dropped {result.dropped_records} non-portable "
            f"record(s): {mappings}"
        )
    elif result.lossy_mappings:
        mappings = ", ".join(result.lossy_mappings)
        warn(f"LOSSY     cleared backend-bound fields: {mappings}")
    for warning in result.mapping_warnings:
        warn(f"Session history diagnostic: {warning}")


def render_detected_session(result: DetectedSession) -> None:
    """Render profile/session detection without interpreting output as markup."""
    heading("Detected previous session")
    info(f"PROFILE  {result.profile or '(unknown)'}")
    info(f"SESSION  {result.session_id}")
    info(f"CWD      {result.cwd}")
    info(f"ROLLOUT  {result.path}")
    if result.last_output:
        console.print("[bold cyan]LAST OUTPUT[/]")
        console.print(Text(result.last_output))
    else:
        warn("No assistant output found in the session.")


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
        ("codex wrapper", report.codex_wrapper or "(none)"),
        ("effective cmd", report.effective_codex_cmd),
        ("fixed args", " ".join(report.codex_args) or "(none)"),
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
    heading(prompt)
    for idx, (_, label) in enumerate(options, start=1):
        console.print(f"  [cyan]{idx}[/]. {label}")
    default = "1"
    while True:
        raw = Prompt.ask("Select", default=default)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        warn("Please enter a valid number.")


def confirm(question: str, default: bool = False) -> bool:
    return Confirm.ask(question, default=default)


@contextmanager
def _raw_key_mode() -> Iterator[None]:
    """Read one key at a time while always restoring the terminal settings."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


_ESCAPE_SEQUENCE_TIMEOUT = 0.1


def _escape_sequence_key(sequence: bytes) -> str | None:
    direct = {
        b"\x1b[A": "up",
        b"\x1b[B": "down",
        b"\x1b[C": "right",
        b"\x1b[D": "left",
        b"\x1bOA": "up",
        b"\x1bOB": "down",
        b"\x1bOC": "right",
        b"\x1bOD": "left",
    }
    if sequence in direct:
        return direct[sequence]
    if sequence.startswith((b"\x1b[", b"\x1bO")) and sequence[-1:] in b"ABCD":
        return {
            b"A": "up",
            b"B": "down",
            b"C": "right",
            b"D": "left",
        }[sequence[-1:]]
    return None


def _read_key() -> str:
    fd = sys.stdin.fileno()
    # TextIOWrapper may buffer the rest of an escape sequence after ESC.
    # Reading the fd directly keeps select() and the bytes in sync.
    key = os.read(fd, 1)
    if key in (b"\r", b"\n"):
        return "confirm"
    if key == b" ":
        return "toggle"
    if key in (b"q", b"Q", b"\x03"):
        return "cancel"
    if key in (b"j", b"J"):
        return "down"
    if key in (b"k", b"K"):
        return "up"
    if key in (b"a", b"A"):
        return "all"
    if key in (b"n", b"N"):
        return "none"
    if key != b"\x1b":
        return "unknown"

    sequence = bytearray(key)
    deadline = time.monotonic() + _ESCAPE_SEQUENCE_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        sequence.extend(chunk)
        parsed = _escape_sequence_key(bytes(sequence))
        if parsed is not None:
            return parsed
        if sequence[-1:] in b"~ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            break
    if bytes(sequence) == b"\x1b":
        return "cancel"
    return "unknown"


def _render_hook_picker(
    profile: str,
    source_path: Path,
    options: list[HookOption],
    selected: set[str],
    cursor: int,
) -> None:
    console.clear()
    heading(f"Root hooks -> {profile}")
    console.print(f"[dim]Source: {source_path}[/]")
    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    # Keep the selection column visible on narrow terminals.  Rich will
    # shrink all columns as a last resort when their measured widths don't
    # fit; with every metadata column set to ``no_wrap`` that used to squeeze
    # this column down to an ellipsis, hiding the checkbox entirely.  Keep
    # metadata columns flexible so they absorb the compression first.
    table.add_column("", width=5, no_wrap=True, overflow="crop")
    table.add_column("Source", max_width=18, style="yellow", overflow="ellipsis")
    table.add_column("Event", max_width=14, style="cyan", overflow="ellipsis")
    table.add_column(
        "Matcher", max_width=16, style="magenta", overflow="ellipsis"
    )
    table.add_column("Type", max_width=9, style="green", overflow="ellipsis")
    table.add_column(
        "Command / prompt", ratio=1, overflow="ellipsis", no_wrap=True, max_width=96
    )
    for index, option in enumerate(options):
        pointer = ">" if index == cursor else " "
        marker = "[x]" if option.key in selected else "[ ]"
        marker_style = "bold green" if option.key in selected else "dim"
        selection = Text()
        selection.append(pointer)
        selection.append(" ")
        selection.append(marker, style=marker_style)
        table.add_row(
            selection,
            option.source,
            option.event,
            option.matcher,
            option.hook_type,
            option.detail,
            style="reverse" if index == cursor else None,
        )
    console.print(table)
    console.print(
        "[dim]↑/↓ or j/k move   Space toggle   a all   n none   "
        "Enter review   q cancel[/]"
    )


def select_hooks(
    profile: str, source_path: Path, options: list[HookOption]
) -> set[str] | None:
    """Interactively select root hooks; return None when cancelled."""
    if not options:
        heading(f"Root hooks -> {profile}")
        console.print(f"[dim]Source: {source_path}[/]")
        warn("No selectable hooks found in the root profile.")
        return set()
    if not sys.stdin.isatty():
        raise RuntimeError("hook selection requires an interactive terminal")

    selected = {option.key for option in options if option.selected}
    cursor = 0
    try:
        with _raw_key_mode():
            while True:
                _render_hook_picker(profile, source_path, options, selected, cursor)
                key = _read_key()
                if key == "confirm":
                    break
                if key == "cancel":
                    console.clear()
                    warn("Hook selection cancelled; nothing was written.")
                    return None
                if key == "up":
                    cursor = (cursor - 1) % len(options)
                elif key == "down":
                    cursor = (cursor + 1) % len(options)
                elif key == "toggle":
                    option = options[cursor]
                    if option.key in selected:
                        selected.remove(option.key)
                    else:
                        selected.add(option.key)
                elif key == "all":
                    selected = {option.key for option in options}
                elif key == "none":
                    selected.clear()
    except (ImportError, OSError, RuntimeError) as exc:
        raise RuntimeError(f"cannot start hook selection TUI: {exc}") from exc
    console.clear()
    return selected


def _render_skill_picker(
    profile: str,
    source_path: Path,
    names: list[str],
    selected: set[str],
    cursor: int,
) -> None:
    console.clear()
    heading(f"Skills -> {profile}")
    console.print(f"[dim]Source: {source_path}[/]")
    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    table.add_column("", width=5, no_wrap=True, overflow="crop")
    table.add_column("Selected", width=8, no_wrap=True)
    table.add_column("Skill", ratio=1, style="cyan", overflow="ellipsis")
    for index, name in enumerate(names):
        pointer = ">" if index == cursor else " "
        marker = "[x]" if name in selected else "[ ]"
        marker_style = "bold green" if name in selected else "dim"
        selection = Text()
        selection.append(pointer)
        selection.append(" ")
        selection.append(marker, style=marker_style)
        table.add_row(
            selection,
            "yes" if name in selected else "no",
            name,
            style="reverse" if index == cursor else None,
        )
    console.print(table)
    console.print(
        "[dim]↑/↓ or j/k move   Space toggle   a all   n none   "
        "Enter save   q cancel[/]"
    )


def select_skills(
    profile: str,
    source_path: Path,
    names: list[str],
    selected: set[str] | None = None,
) -> set[str] | None:
    """Interactively select skill names; return None when cancelled."""
    if not names:
        heading(f"Skills -> {profile}")
        console.print(f"[dim]Source: {source_path}[/]")
        warn("No selectable skills found in the source home.")
        return set()
    if not sys.stdin.isatty():
        raise RuntimeError("skill selection requires an interactive terminal")

    selected_names = set(names if selected is None else selected)
    selected_names.intersection_update(names)
    cursor = 0
    try:
        with _raw_key_mode():
            while True:
                _render_skill_picker(
                    profile, source_path, names, selected_names, cursor
                )
                key = _read_key()
                if key == "confirm":
                    break
                if key == "cancel":
                    console.clear()
                    warn("Skill selection cancelled; nothing was written.")
                    return None
                if key == "up":
                    cursor = (cursor - 1) % len(names)
                elif key == "down":
                    cursor = (cursor + 1) % len(names)
                elif key == "toggle":
                    name = names[cursor]
                    if name in selected_names:
                        selected_names.remove(name)
                    else:
                        selected_names.add(name)
                elif key == "all":
                    selected_names = set(names)
                elif key == "none":
                    selected_names.clear()
    except (ImportError, OSError, RuntimeError) as exc:
        raise RuntimeError(f"cannot start skill selection TUI: {exc}") from exc
    console.clear()
    return selected_names


def render_hook_sync_result(result: HookSyncResult) -> None:
    heading("Profile hooks")
    if result.changed:
        success(
            f"Synced {result.selected_count} selected root hook(s): "
            f"{result.added} added, {result.removed} removed"
        )
    else:
        info(f"Profile already matches {result.selected_count} selected root hook(s).")
    for key in result.missing:
        warn(f"Saved root hook is no longer present: {key}")
    if result.backup_path is not None:
        info(f"Hook backup: {result.backup_path}")


def home_ref_label(ref: HomeRef) -> str:
    return ref.label
