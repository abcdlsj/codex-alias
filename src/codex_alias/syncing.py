"""Pure file/resource synchronization helpers.

The CLI is responsible for prompts and rendering.  This module only decides
which files to copy/remove and returns messages for the caller to render.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SyncMessage:
    """One user-visible synchronization event, without any rendering code."""

    level: str
    text: str


@dataclass(frozen=True, slots=True)
class SyncTypeSpec:
    """Metadata shared by Click choices, help output, and confirmation rules."""

    name: str
    description: str
    confirm: bool = False


_SKILL_DIRS = ("skills", ".skills")
_PLUGIN_ONLY_DIRS = ("plugins", ".plugins")
_AGENT_DIRS = (".agents", "agents")
_MCP_DIRS = ("mcp", ".mcp")
_LEGACY_PLUGIN_DIRS = (
    *_SKILL_DIRS,
    *_PLUGIN_ONLY_DIRS,
    *_AGENT_DIRS,
    *_MCP_DIRS,
    "rules",
    "prompts",
)
_INSTRUCTION_FILES = ("AGENTS.md", "AGENTS.override.md")
_CORE_CONFIG = ("auth.json", "config.toml")


SYNC_TYPE_SPECS = (
    SyncTypeSpec(
        "skills", "selected skill packages (excludes .system by default)", True
    ),
    SyncTypeSpec("agents", ".agents and agents directories", True),
    SyncTypeSpec("mcp", "mcp and .mcp directories", True),
    SyncTypeSpec("plugins", "plugins and .plugins directories only", True),
    SyncTypeSpec(
        "bundle",
        "legacy bundle: skills, plugins, agents, MCP, rules, prompts",
        True,
    ),
    SyncTypeSpec("rules", "rules directory", True),
    SyncTypeSpec("prompts", "prompts directory", True),
    SyncTypeSpec("instructions", "AGENTS.md and AGENTS.override.md", True),
    SyncTypeSpec("config", "auth.json and config.toml", True),
    SyncTypeSpec("statusline", "status_line and session_picker_view from root config"),
    SyncTypeSpec("hooks", "the profile's saved root-hook selection"),
    SyncTypeSpec("sessions_shared", "shared session symlinks to the source home"),
    SyncTypeSpec("sessions_migrate", "interactive session migration"),
)
SYNC_TYPE_DESCRIPTIONS = {item.name: item.description for item in SYNC_TYPE_SPECS}
SYNC_TYPE_CHOICES = tuple(item.name for item in SYNC_TYPE_SPECS)
# ``plugins-only`` is an internal persisted name produced when the user saves
# explicit ``--type plugins``; it is intentionally not a public Click choice.
SYNC_CONFIRM_TYPES = frozenset(
    {item.name for item in SYNC_TYPE_SPECS if item.confirm} | {"plugins-only"}
)
GRANULAR_SYNC_TYPES = frozenset(
    {"skills", "plugins", "agents", "mcp", "rules", "prompts"}
)


def copy_resource_dirs(
    src: Path,
    dst: Path,
    names: tuple[str, ...],
    *,
    dry_run: bool = False,
    label: str = "resource",
) -> tuple[SyncMessage, ...]:
    """Copy a set of top-level resource directories and report the actions."""
    messages: list[SyncMessage] = []
    copied = False
    for name in names:
        src_dir = src / name
        if src_dir.is_dir():
            if dry_run:
                messages.append(
                    SyncMessage("info", f"Would copy {label} dir: {name}")
                )
            else:
                shutil.copytree(
                    src_dir,
                    dst / name,
                    dirs_exist_ok=True,
                    copy_function=_copy_skipping_dangling_links,
                )
                messages.append(
                    SyncMessage("success", f"Copied {label} dir: {name}")
                )
            copied = True
    if not copied:
        messages.append(SyncMessage("info", f"No {label} directories found in {src}."))
    return tuple(messages)


def _copy_skipping_dangling_links(src: str, dst: str) -> None:
    if os.path.islink(src) and not os.path.exists(src):
        return
    shutil.copy2(src, dst)


def copy_plugin_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    """Copy the historical all-in-one plugin resource bundle."""
    return copy_resource_dirs(
        src, dst, _LEGACY_PLUGIN_DIRS, dry_run=dry_run, label="plugin"
    )


def copy_plugin_only_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    return copy_resource_dirs(
        src, dst, _PLUGIN_ONLY_DIRS, dry_run=dry_run, label="plugin"
    )


def copy_agents_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    return copy_resource_dirs(src, dst, _AGENT_DIRS, dry_run=dry_run, label="agent")


def copy_mcp_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    return copy_resource_dirs(src, dst, _MCP_DIRS, dry_run=dry_run, label="MCP")


def validate_skill_name(value: str, option: str) -> str:
    """Validate a selector as a single top-level skill directory name."""
    value = value.strip()
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.parts[0] in {".", ".."}
    ):
        raise ValueError(
            f"{option} accepts a top-level skill directory name, got {value!r}"
        )
    return value


def read_skills_file(path: Path) -> tuple[str, ...]:
    """Read one validated skill name per line, ignoring comments and blanks."""
    lines = path.read_text(encoding="utf-8").splitlines()
    names: list[str] = []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        names.append(validate_skill_name(value, "--skills-file"))
    return tuple(dict.fromkeys(names))


def source_skill_names(src: Path, *, include_system: bool = False) -> tuple[str, ...]:
    """Return selectable top-level skill package names in sorted order."""
    names: set[str] = set()
    for dirname in _SKILL_DIRS:
        source_dir = src / dirname
        if not source_dir.is_dir():
            continue
        for entry in source_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") and not include_system:
                continue
            names.add(entry.name)
    return tuple(sorted(names))


def selected_skill_names(
    src: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    include_system: bool,
) -> tuple[str, ...]:
    """Resolve an allow/deny selector against the source home."""
    available = set(source_skill_names(src, include_system=include_system))
    if include:
        missing = sorted(set(include) - available)
        if missing:
            raise ValueError(
                "skill(s) not found in source home: " + ", ".join(missing)
            )
        selected = set(include)
    else:
        selected = available
    selected.difference_update(exclude)
    return tuple(sorted(selected))


def _remove_skill_entry(path: Path, *, dry_run: bool) -> SyncMessage:
    if dry_run:
        return SyncMessage("warn", f"Would remove stale skill: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    return SyncMessage("warn", f"Removed stale skill: {path}")


def copy_skills(
    src: Path,
    dst: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    include_system: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> tuple[SyncMessage, ...]:
    """Copy selected skills and optionally prune stale user packages."""
    selected = set(
        selected_skill_names(
            src,
            include=include,
            exclude=exclude,
            include_system=include_system,
        )
    )
    messages: list[SyncMessage] = []
    copied = False
    for dirname in _SKILL_DIRS:
        source_dir = src / dirname
        if not source_dir.is_dir():
            continue
        target_dir = dst / dirname
        source_names = {
            entry.name
            for entry in source_dir.iterdir()
            if entry.is_dir() and entry.name in selected
        }
        for name in sorted(source_names):
            source_entry = source_dir / name
            target_entry = target_dir / name
            if dry_run:
                messages.append(
                    SyncMessage("info", f"Would copy skill: {dirname}/{name}")
                )
            else:
                shutil.copytree(source_entry, target_entry, dirs_exist_ok=True)
                messages.append(
                    SyncMessage("success", f"Copied skill: {dirname}/{name}")
                )
            copied = True

        if not prune or not target_dir.is_dir():
            continue
        for target_entry in sorted(target_dir.iterdir()):
            # Codex owns .system; never remove it through profile syncing.
            if target_entry.name == ".system":
                continue
            # Skill roots may also contain migration manifests or other
            # metadata files. Only directories represent removable packages.
            if not target_entry.is_dir():
                continue
            if target_entry.name not in selected:
                messages.append(_remove_skill_entry(target_entry, dry_run=dry_run))

    if not copied and not prune:
        messages.append(SyncMessage("info", f"No selected skills found in {src}."))
    return tuple(messages)


def copy_instruction_files(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    """Mirror global instruction files, including stale override removal."""
    if src.resolve() == dst.resolve():
        return (
            SyncMessage(
                "info",
                f"Instruction source and target are the same, skipped: {src}",
            ),
        )

    messages: list[SyncMessage] = []
    changed = False
    for name in _INSTRUCTION_FILES:
        src_file = src / name
        dst_file = dst / name
        if src_file.is_file():
            if dry_run:
                messages.append(
                    SyncMessage("info", f"Would copy instruction file: {name}")
                )
            else:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                messages.append(
                    SyncMessage("success", f"Copied instruction file: {name}")
                )
            changed = True
        elif dst_file.is_file() or dst_file.is_symlink():
            if dry_run:
                messages.append(
                    SyncMessage(
                        "info", f"Would remove stale instruction file: {name}"
                    )
                )
            else:
                dst_file.unlink()
                messages.append(
                    SyncMessage(
                        "success", f"Removed stale instruction file: {name}"
                    )
                )
            changed = True
    if not changed:
        messages.append(
            SyncMessage("info", f"No global instruction files found in {src}.")
        )
    return tuple(messages)


def copy_core_config(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[SyncMessage, ...]:
    """Copy the core auth/config files from a source home."""
    messages: list[SyncMessage] = []
    copied = False
    for name in _CORE_CONFIG:
        src_file = src / name
        if src_file.is_file():
            if dry_run:
                messages.append(SyncMessage("info", f"Would copy config file: {name}"))
            else:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_file, dst / name)
                messages.append(SyncMessage("success", f"Copied config file: {name}"))
            copied = True
        else:
            messages.append(
                SyncMessage("info", f"Missing config file, skipped: {src_file}")
            )
    if not copied:
        messages.append(SyncMessage("info", "No config files copied."))
    return tuple(messages)
