# codex-alias

`codex-alias` runs multiple Codex accounts/profiles with separate homes. Each
profile gets an isolated `CODEX_HOME` and a wrapper command (for example
`codex-work`) that forwards to the original `codex` binary, so auth, config, and
history stay separated.

It ships as a Python package with two parts:

- a reusable, UI-free library (`codex_alias`) that does all the filesystem work
- a `rich` + `click` CLI (`codexalias`) built on top of it

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Install the CLI globally as a tool
uv tool install .

# Or work inside the project
uv sync
uv run codexalias doctor
```

`uv tool install .` puts `codexalias` on your PATH. From there:

```bash
codexalias add work
codex-work
```

During `add`, interactive prompts let you:
1. Copy plugins/skills from the source home
2. Copy current config (`auth.json` + `config.toml`)
3. Share sessions with the root home (symlink)
4. Otherwise migrate sessions into the new profile

Pass `--no-bootstrap` to skip the prompts.

## Commands

```bash
# Create a wrapper command (default: codex-<profile>)
codexalias add <profile> [command-name]

# Import one session from default ~/.codex into current/target home
codexalias import <session-id> [target|@current]

# Interactive session migration into the current home
codexalias migrate session

# Copy all sessions from one home into another
codexalias migrate copy <source|@source> [target|@current]

# Copy one session from one home into another
codexalias migrate one <source|@source> <session-id> [target|@current]

# Share sessions with a source home via symlink (existing profile)
codexalias share-sessions <profile> [source|@source]

# Run codex once with a profile (without creating a wrapper)
codexalias run <profile> [codex args...]

# List profiles
codexalias list

# Print the absolute home path of a profile
codexalias path <profile>

# Remove a wrapper command (profile data is kept)
codexalias remove <profile> [command-name]

# Environment and sanity checks
codexalias doctor
```

`@source` refers to the configured source home; `@current` refers to the current
`CODEX_HOME` (falling back to the source home when unset). A bare profile name or
an absolute path also works anywhere a home is expected.

## Environment variables

- `CODEXSWITCH_PROFILE_ROOT`: profile root directory (default: `~/.codex/profiles`)
- `CODEXSWITCH_BIN_DIR`: output directory for wrappers (default: `~/.local/bin`)
- `CODEXSWITCH_CODEX_CMD`: original Codex command (default: `codex`)
- `CODEXSWITCH_SOURCE_HOME`: source home used by `add`/`@source` (default: `$CODEX_HOME` or `~/.codex`)
- `CODEXSWITCH_MANAGER_BIN_NAME`: manager binary name reported by `doctor` (default: `codexalias`)

## Library usage

The core is importable and never prints or exits — it returns value objects or
raises `CodexmError` subclasses, so you can drive it from your own tooling:

```python
from codex_alias import CodexAlias, Config

mgr = CodexAlias(Config.from_env())
mgr.add_profile("work")

for profile in mgr.list_profiles():
    print(profile.name, "shared" if profile.sessions_shared else "isolated")

# Copy one session between homes
src = mgr.resolve_home_ref("@source").path
dst = mgr.resolve_home_ref("work").path
result = mgr.copy_session_by_query(src, "019d1df0-8f1e-7393-b54a-0f0b511c5a33", dst)
print(result.status)
```

## Session sharing

By default each profile has isolated sessions. To share history across profiles
(useful when different provider configs access the same conversations), share
sessions during creation (answer yes to "Share sessions with root home") or for
an existing profile:

```bash
codexalias share-sessions work
```

This symlinks `~/.codex/profiles/work/sessions` (plus `history.jsonl` and the
`state_5.sqlite` / `logs_1.sqlite` metadata databases) to the source home, so
sharing profiles see the same conversation history while keeping separate
auth/config. Existing real files are backed up to `*.backup.N` before being
replaced with a symlink.

## Development

```bash
uv sync
uv run pytest
```
