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
1. Copy plugins/skills/rules from the source home
2. Copy global instructions (`AGENTS.md` + `AGENTS.override.md`)
3. Copy current config (`auth.json` + `config.toml`)
4. Select root-profile hooks to share with the new profile
5. Share sessions with the root home (symlink)
6. Otherwise migrate sessions into the new profile

The choices are recorded as ordered sync types. A later `codexalias sync
<profile>` re-runs the corresponding migration handlers in that order. Pass
`--no-bootstrap` to skip the prompts.

## Commands

```bash
# Create a wrapper command (default: codex-<profile>)
codexalias add <profile> [command-name]

# Import one session from default ~/.codex into current/target home
codexalias import <session-id> [target|@current]

# Repair stale provider/model metadata
codexalias fix-session <session-id> [home|@current] \
  [--provider <provider>] [--model <model>]

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

# Shortcut: run a profile and forward all remaining args to Codex
codexa <profile> [codex args...]

# List profiles
codexalias list

# Print the absolute home path of a profile
codexalias path <profile>

# Remove a profile: its wrapper command and profile data
codexalias remove <profile> [command-name]

# Keep the profile data and remove only the wrapper command
codexalias remove <profile> [command-name] --keep-data

# Environment and sanity checks
codexalias doctor

# Select root hooks for a profile
codexalias hooks

# Reapply the profile's saved migration types from the source home
codexalias sync [profile] [--yes]

# One-shot sync one or more independently managed content types
codexalias sync --all --type skills --source ~/.codex --yes
codexalias sync --all --type plugins --type rules --type prompts --source ~/.codex --yes

# Sync only selected skills; repeat --skill or use a file
codexalias sync --all --type skills \
  --skill review-mr --skill domain-modeling --source ~/.codex --yes
codexalias sync --all --type skills --skills-file ./skills.allowlist \
  --exclude-skill grilling --source ~/.codex --yes

# Persist the selector for future `codexalias sync <profile>` calls
codexalias sync --all --type skills --skill review-mr --save --source ~/.codex --yes

# Preview or clean stale user skills (never removes .system)
codexalias sync --all --type skills --skill review-mr --dry-run --source ~/.codex
codexalias sync --all --type skills --skill review-mr --prune-skills --source ~/.codex --yes

# Show all independently selectable sync types
codexalias sync --list-types

# Interactive skill table (selection is persisted and unselected user skills are removed)
codexalias sync <profile> --select-skills --source ~/.codex --yes

# Machine/AI-readable inventory
codexalias list --json
codexalias sync --list-skills --json --source ~/.codex
codexalias sync --list-types --json

# Enable global instruction sync for an existing profile, then sync it
codexalias sync <profile> --instructions --yes
```

`@source` refers to the configured source home; `@current` refers to the current
`CODEX_HOME` (falling back to the source home when unset). A bare profile name or
an absolute path also works anywhere a home is expected.

`remove` prompts for confirmation before deleting a profile home (auth, config,
sessions, and everything else under the profile). Pass `--yes` to skip the
prompt or `--keep-data` to keep the home and only remove the wrapper. Deleting a
profile that is the configured source home or the current `CODEX_HOME` is
refused.

By default, profile commands resolve `codex` through the user's login shell, as
if `codex ...` had been entered directly. This preserves fish/bash/zsh functions
and aliases as well as PATH-based executable wrappers such as Superset. Existing
generated profile commands pick up wrapper changes automatically; refreshing
them is only necessary when the generated wrapper format itself changes.

## Environment variables

- `CODEXALIAS_PROFILE_ROOT`: profile root directory (default: `~/.codex/profiles`)
- `CODEXALIAS_BIN_DIR`: output directory for wrappers (default: `~/.local/bin`)
- `CODEXALIAS_CODEX_CMD`: original Codex command (default: `codex`)
- `CODEXALIAS_CODEX_WRAPPER`: executable Codex wrapper; takes precedence over
  `CODEXALIAS_CODEX_CMD` for `run`, `resume`, and generated profile commands
- `CODEXALIAS_CODEX_ARGS`: fixed arguments prepended to every Codex invocation
- `CODEXALIAS_SOURCE_HOME`: source home used by `add`/`@source` (default: `$CODEX_HOME` or `~/.codex`)
- `CODEXALIAS_MANAGER_BIN_NAME`: manager binary name used by generated profile commands (default: `codexalias`)

To explicitly override normal shell resolution with a standalone executable:

```bash
export CODEXALIAS_CODEX_WRAPPER="$HOME/.superset/bin/codex"
export CODEXALIAS_CODEX_ARGS="--dangerously-bypass-approvals-and-sandbox"
codexa resume <session-id>
```

The explicit override must be an executable name or path. Without it, shell
aliases and functions are inherited automatically.

## Hook sharing

Codex reads hooks from `$CODEX_HOME/hooks.json`. Because each profile has its
own `CODEX_HOME`, `codexalias add` offers a table of hooks from the configured
source home (`$CODEXALIAS_SOURCE_HOME/hooks.json`, default `~/.codex/hooks.json`).
Use Space to toggle a row, Enter to review the selection, and confirm to write
it. The standalone `codexalias hooks` command first asks which profile to edit
and then opens the same table. Enabled plugin hooks remain selectable even when
the source home has no standalone `hooks.json`.

The table also includes hooks from enabled Codex plugins in the root
`config.toml` (for example, `agent-trace`). When copied to a profile, plugin
hooks are bound to their root plugin directory so `${PLUGIN_ROOT}` continues to
work outside the plugin's own context.

The ordered migration types chosen during `add` are stored in the profile's
`.codexalias.json`. New profiles can manage `skills`, `plugins`, `agents`,
`mcp`, `rules`, `prompts`, `instructions`, `config`, `hooks`, `sessions_shared`,
and `sessions_migrate` independently. The `bundle` type is available for the
old all-in-one behavior. For backward compatibility, a profile whose saved
state contains the historical `plugins` type still runs that bundle; an
explicit `--type plugins` means only `plugins/` and `.plugins/`.
Running `codexalias sync <profile>` walks the saved types in order.

Use `--type TYPE` for a one-shot sync that does not change saved settings;
repeat the option to run multiple types. Use `--all` (or `--all-profiles`) to
target every profile, and `--source PATH` to pin the source home explicitly when
the current process already has a profile-specific `CODEX_HOME`.

Skills are selectable by `--skill NAME`, `--exclude-skill NAME`, or
`--skills-file PATH`. An empty include list means all non-system skills;
`.system` is excluded unless `--include-system-skills` is explicit. `--save`
persists the selector per profile, `--dry-run` previews file operations, and
`--prune-skills` removes only non-selected user skills from the target; it never
removes `.system`. Use `--list-skills` to inspect available skill names.
`--select-skills` opens the same keyboard-driven table used for hooks, saves
the resulting allowlist, and enables pruning of unselected user skills. It
never removes `.system`. `--json` is available for profile, skill, and
sync-type inventories so an AI or shell script can inspect state without
parsing Rich output, then issue an explicit `--type/--skill/--save` command.

Plugin/instruction/config sync asks for confirmation before overwriting profile
files; pass `--yes` for explicit non-interactive approval. Instruction sync
mirrors both global instruction filenames and removes a stale target override
when it no longer exists in the source home. Profile-local hooks are preserved;
hook-specific ownership snapshots remain internal to the hook migration so
changed root hooks can be replaced safely.

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

Codex persists model-provider and model metadata both inside each JSONL session
and in the `state_5.sqlite` thread index. If a provider is later renamed or a
profile uses a different model, `codex resume` can fail before the TUI starts.
Repair both persisted copies with:

```bash
# Preview the repair; "custom" is inferred from ~/.codex/config.toml
codexalias fix-session 019f8938-544e-7160-901c-af1ffb2657a5 --dry-run

# Apply it, but only where the stale value is exactly "aicoding"
codexalias fix-session 019f8938-544e-7160-901c-af1ffb2657a5 \
  --from-provider aicoding \
  --model deepseek-v4-pro
```

The command validates every JSONL record before writing, creates unique
`*.backup.N` copies for changed JSONL and SQLite files, atomically replaces the
JSONL, and conditionally updates only the matching SQLite thread row. Use
`--provider` to override the provider inferred from the selected home's
top-level `model_provider` setting, and `--model` to repair the persisted model
as well.

## Resuming with another profile

`codexa resume <session-id> [codex args...]` shows a numbered Rich list
containing `default` and every added profile. Arguments after the session id
are forwarded to the final Codex resume command, for example
`codexa resume <session-id> --profile luna-high --yolo`. After the profile is selected, it asks
whether to fix the copied session's provider and model. A `y` reads both
values from the target profile's top-level `config.toml`, repairs the new
session's JSONL and SQLite metadata, and then launches Codex. Profiles using
Codex's built-in auth may omit `model_provider`; in that case the source
session's provider is preserved. An `n` keeps the existing behavior and leaves
the copied model unchanged. The source session is never modified. This also
works when profiles share session storage through symlinks because the cloned
session has a distinct ID.

The clone also applies registered response-item compatibility mappings,
regardless of the fix prompt. The current `gpt-5*` rule clears non-empty
plaintext `reasoning.content`; GPT-5 Responses endpoints reject that replay
shape. Rules use model and wire-API capabilities, not hard-coded provider
names.

Encrypted history has a separate portability boundary. Codexalias compares a
normalized `wire_api + base_url` backend fingerprint when both sides are known.
It preserves encrypted reasoning for aliases of the same backend, and treats
foreign encrypted reasoning as an explicitly reported lossy mapping without
dropping its record. It does not guess when either backend is unknown. For a
known foreign backend it keeps the
reasoning record (and its paginated ordinal) while clearing the backend-bound
`encrypted_content`; this preserves the history cursor without replaying
unreadable ciphertext. Foreign encrypted compaction blocks the repair because
deleting it could remove the only copy of earlier context. Incomplete
or orphan historical tool calls are reported as diagnostics but are not changed.
Add future rules to `src/codex_alias/session_mappings.py` and classify each one
as lossless or lossy. When a lossy mapping is needed, the CLI asks for
confirmation before writing a clone or repairing an existing session. Library
callers can pass `allow_lossy=False` to require the same explicit decision in
their own UI.

Use `--profile cpa` to skip the profile picker or `--no-launch` to create the
copy without starting Codex. The fix confirmation is still shown after the
profile is known. The installed executable names are `codex-alias`, `codexa`,
and `codexalias`.

## Development

```bash
uv sync
uv run pytest
```
