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
# One-shot install onto PATH
make install

# Equivalently, via uv directly
uv tool install .

# Or work inside the project
uv sync
uv run codexalias doctor
```

Other `make` targets: `make test`, `make sync`, `make uninstall`, `make clean`
(run `make help` for the list).

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

# Repair stale provider metadata (provider defaults to HOME/config.toml)
codexalias fix-session <session-id> [home|@current] [--provider <provider>]

# Copy a session for default/another profile, then resume the copy
codexa resume <session-id> [--profile default|<profile>]

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

- `CODEXALIAS_PROFILE_ROOT`: profile root directory (default: `~/.codex/profiles`)
- `CODEXALIAS_BIN_DIR`: output directory for wrappers (default: `~/.local/bin`)
- `CODEXALIAS_CODEX_CMD`: original Codex command (default: `codex`)
- `CODEXALIAS_CODEX_WRAPPER`: executable Codex wrapper; takes precedence over
  `CODEXALIAS_CODEX_CMD` for `run`, `resume`, and generated profile commands
- `CODEXALIAS_CODEX_ARGS`: fixed arguments prepended to every Codex invocation
- `CODEXALIAS_SOURCE_HOME`: source home used by `add`/`@source` (default: `$CODEX_HOME` or `~/.codex`)
- `CODEXALIAS_MANAGER_BIN_NAME`: manager binary name used by generated profile commands (default: `codexalias`)

To reuse a wrapper that automatically adds yolo flags, hooks, or notifications:

```bash
export CODEXALIAS_CODEX_WRAPPER="$HOME/.superset/bin/codex"
export CODEXALIAS_CODEX_ARGS="--dangerously-bypass-approvals-and-sandbox"
codexa resume <session-id>
```

The value must be an executable name or path. Shell aliases and functions are
not executable files and therefore cannot be used as process wrappers.

## Library usage

The core is importable and never prints or exits — it returns value objects or
raises `CodexAliasError` subclasses, so you can drive it from your own tooling:

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

## Repairing a session

Codex persists model-provider metadata both inside each JSONL session and in
the `state_5.sqlite` thread index. If a provider is later renamed or removed,
`codex resume` can fail before the TUI starts with `Model provider '<name>' not
found`. Repair both persisted copies with:

```bash
# Preview the repair; "custom" is inferred from ~/.codex/config.toml
codexalias fix-session 019f8938-544e-7160-901c-af1ffb2657a5 --dry-run

# Apply it, but only where the stale value is exactly "aicoding"
codexalias fix-session 019f8938-544e-7160-901c-af1ffb2657a5 \
  --from-provider aicoding
```

The command validates every JSONL record before writing, creates unique
`*.backup.N` copies for changed JSONL and SQLite files, atomically replaces the
JSONL, and conditionally updates only the matching SQLite thread row. Use
`--provider` to override the provider inferred from the selected home's
top-level `model_provider` setting.

## Resuming with another profile

`codexa resume <session-id>` shows a numbered Rich list containing
`default` and every added profile. It always creates a new session ID, copies
the JSONL, history, and SQLite thread metadata, changes the provider only in
the copy, and launches Codex with the selected profile. The source session is
never modified. This also works when profiles share session storage through
symlinks because the cloned session has a distinct ID.

Use `--profile cpa` to skip the prompt or `--no-launch` to create the copy
without starting Codex. The installed executable names are `codex-alias`,
`codexa`, and `codexalias`.

## Development

```bash
uv sync
uv run pytest
```
