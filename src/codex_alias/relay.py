"""Optional per-profile ``codex-relay`` process management.

Codex CLI speaks the Responses API, while a number of compatible providers
only expose Chat Completions.  A profile can opt into a local
``codex-relay`` process by adding ``relay.toml`` to its Codex home.  The
manager starts (or reuses) that process and injects a small set of Codex
configuration overrides at launch time.  Profiles without that file are
unchanged.

The relay itself is deliberately an external executable.  Keeping it outside
this package means users can update it independently and codex-alias remains
usable when no relay is installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - Python 3.11+ is covered by the test matrix.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    import tomli as tomllib  # type: ignore[no-redef]

from .config import Config
from .errors import RelayConfigError, RelayUnavailableError

try:  # pragma: no cover - fcntl is present on the supported Unix platforms.
    import fcntl
except ImportError:  # pragma: no cover - keeps imports portable.
    fcntl = None  # type: ignore[assignment]


RELAY_CONFIG_NAME = "relay.toml"
RELAY_STATE_DIR_NAME = ".codexalias-relay"
RELAY_PROVIDER = "codexalias_relay"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_API_KEY_FILE = "auth.json"
DEFAULT_API_KEY_FIELD = "OPENAI_API_KEY"
DEFAULT_READY_TIMEOUT = 8.0


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Validated settings read from one profile's ``relay.toml``."""

    path: Path
    upstream: str
    enabled: bool = True
    host: str = DEFAULT_HOST
    port: int | None = None
    provider: str | None = None
    api_key_env: str | None = DEFAULT_API_KEY_ENV
    api_key_file: Path | None = Path(DEFAULT_API_KEY_FILE)
    api_key_field: str = DEFAULT_API_KEY_FIELD
    command: str | None = None
    extra_args: tuple[str, ...] = ()

    @classmethod
    def from_home(cls, home: Path) -> "RelayConfig | None":
        """Load an enabled profile relay config, or ``None`` when absent."""
        path = home / RELAY_CONFIG_NAME
        if not path.is_file():
            return None

        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RelayConfigError(f"could not read relay config {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise RelayConfigError(f"relay config must be a TOML table: {path}")
        # Accepting a nested table makes the file pleasant to compose while
        # keeping the documented top-level form concise.
        values: dict[str, Any] = raw
        nested = raw.get("relay")
        if isinstance(nested, dict):
            values = {**raw, **nested}

        enabled = _bool_value(values.get("enabled", True), "enabled", path)
        if not enabled:
            return cls(path=path, upstream="", enabled=False)

        upstream = _string_value(values.get("upstream"), "upstream", path)
        host = _string_value(values.get("host", DEFAULT_HOST), "host", path)
        raw_provider = values.get("provider")
        provider = (
            _string_value(raw_provider, "provider", path)
            if raw_provider is not None
            else None
        )
        if provider is not None and not provider.replace("_", "").replace("-", "").isalnum():
            raise RelayConfigError(
                f"relay provider must contain only letters, digits, '_' or '-': {path}"
            )

        port = _port_value(values.get("port"), path)
        api_key_env = _optional_string(
            values.get("api_key_env", DEFAULT_API_KEY_ENV),
            "api_key_env",
            path,
        )
        api_key_field = _string_value(
            values.get("api_key_field", DEFAULT_API_KEY_FIELD),
            "api_key_field",
            path,
        )

        raw_key_file = values.get("api_key_file", DEFAULT_API_KEY_FILE)
        if raw_key_file is None or raw_key_file == "":
            api_key_file = None
        else:
            key_file = _string_value(raw_key_file, "api_key_file", path)
            api_key_file = Path(key_file)

        command = _optional_string(values.get("command"), "command", path)
        raw_extra = values.get("extra_args", [])
        if isinstance(raw_extra, str):
            extra_args = tuple(shlex.split(raw_extra))
        elif isinstance(raw_extra, list) and all(isinstance(item, str) for item in raw_extra):
            extra_args = tuple(raw_extra)
        else:
            raise RelayConfigError(
                f"relay extra_args must be a string or an array of strings: {path}"
            )

        return cls(
            path=path,
            upstream=upstream,
            enabled=True,
            host=host,
            port=port,
            provider=provider,
            api_key_env=api_key_env,
            api_key_file=api_key_file,
            api_key_field=api_key_field,
            command=command,
            extra_args=extra_args,
        )


@dataclass(frozen=True, slots=True)
class RelayState:
    """Persisted identity of a relay process (never contains the API key)."""

    pid: int
    port: int
    host: str
    fingerprint: str
    started_at: float

    @classmethod
    def from_json(cls, raw: object) -> "RelayState | None":
        if not isinstance(raw, dict):
            return None
        try:
            pid = int(raw["pid"])
            port = int(raw["port"])
            host = str(raw["host"])
            fingerprint = str(raw["fingerprint"])
            started_at = float(raw["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if pid <= 0 or not 1 <= port <= 65535 or not host or not fingerprint:
            return None
        return cls(pid, port, host, fingerprint, started_at)

    def as_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "fingerprint": self.fingerprint,
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class RelayStatus:
    """Human- and machine-readable relay status."""

    home: Path
    state: str
    host: str | None = None
    port: int | None = None
    pid: int | None = None
    message: str | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True, slots=True)
class RelayLaunch:
    """Launch additions returned to the Codex process builder."""

    codex_args: tuple[str, ...]
    environment: dict[str, str]
    status: RelayStatus | None


class RelayService:
    """Start and inspect optional relays for profile homes."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def config_for(self, home: Path) -> RelayConfig | None:
        return RelayConfig.from_home(home)

    def prepare(self, home: Path) -> RelayLaunch:
        """Ensure a configured relay is ready and return Codex overrides."""
        relay_config = self.config_for(home)
        if relay_config is None or not relay_config.enabled:
            return RelayLaunch((), {}, None)

        key = self._api_key(home, relay_config)
        state = self._ensure_started(home, relay_config, key)
        base_url = _base_url(state.host, state.port)
        provider = (
            relay_config.provider
            or self._configured_provider(home)
            or RELAY_PROVIDER
        )
        provider_path = f"model_providers.{provider}"
        codex_args = (
            "-c",
            f"model_provider={_toml_string(provider)}",
            "-c",
            f"{provider_path}.name={_toml_string('codex-relay')}",
            "-c",
            f"{provider_path}.base_url={_toml_string(base_url)}",
            "-c",
            f"{provider_path}.wire_api=\"responses\"",
        )
        environment: dict[str, str] = {}
        if relay_config.api_key_env:
            codex_args += (
                "-c",
                f"{provider_path}.env_key={_toml_string(relay_config.api_key_env)}",
            )
            # The local relay does not need to authenticate its client by
            # default, but sending the same key is useful if it is configured
            # to protect its listener.
            environment[relay_config.api_key_env] = key

        status = RelayStatus(
            home=home,
            state="running",
            host=state.host,
            port=state.port,
            pid=state.pid,
        )
        return RelayLaunch(codex_args, environment, status)

    @staticmethod
    def _configured_provider(home: Path) -> str | None:
        """Return the active custom provider, when the profile defines one."""
        path = home / "config.toml"
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        active = raw.get("model_provider")
        providers = raw.get("model_providers")
        if not isinstance(active, str) or not isinstance(providers, dict):
            return None
        if not isinstance(providers.get(active), dict):
            return None
        if not active.replace("_", "").replace("-", "").isalnum():
            return None
        return active

    def start(self, home: Path) -> RelayStatus:
        """Start/reuse the relay configured for ``home``."""
        relay_config = self.config_for(home)
        if relay_config is None or not relay_config.enabled:
            return RelayStatus(home=home, state="disabled", message="relay.toml is not enabled")
        key = self._api_key(home, relay_config)
        state = self._ensure_started(home, relay_config, key)
        return RelayStatus(home, "running", state.host, state.port, state.pid)

    def stop(self, home: Path) -> RelayStatus:
        """Stop the process recorded for ``home`` (if any)."""
        state_dir = self._state_dir(home)
        if not state_dir.is_dir():
            return RelayStatus(home=home, state="stopped", message="no relay process recorded")
        with _file_lock(state_dir / "lock"):
            state = self._read_state(home)
            if state is None:
                return RelayStatus(
                    home=home,
                    state="stopped",
                    message="no relay process recorded",
                )
            if _pid_alive(state.pid):
                _terminate_pid(state.pid)
            self._state_path(home).unlink(missing_ok=True)
            return RelayStatus(
                home=home,
                state="stopped",
                host=state.host,
                port=state.port,
                pid=state.pid,
            )

    def status(self, home: Path) -> RelayStatus:
        """Inspect a profile relay without starting it."""
        relay_config = self.config_for(home)
        if relay_config is None or not relay_config.enabled:
            return RelayStatus(home=home, state="disabled", message="relay.toml is not enabled")
        state = self._read_state(home)
        if state is None:
            return RelayStatus(home=home, state="stopped", message="relay has not been started")
        if not _pid_alive(state.pid):
            return RelayStatus(
                home=home,
                state="stale",
                host=state.host,
                port=state.port,
                pid=state.pid,
            )
        if not self._ready(state.host, state.port, timeout=0.4):
            return RelayStatus(
                home=home,
                state="stale",
                host=state.host,
                port=state.port,
                pid=state.pid,
                message="process is alive but its listener is not ready",
            )
        return RelayStatus(home, "running", state.host, state.port, state.pid)

    def statuses(self, homes: list[Path]) -> list[RelayStatus]:
        return [self.status(home) for home in homes]

    def _ensure_started(self, home: Path, relay_config: RelayConfig, key: str) -> RelayState:
        state_dir = self._state_dir(home)
        state_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(state_dir / "lock"):
            fingerprint = _fingerprint(relay_config, key, self.config.relay_command)
            current = self._read_state(home)
            if (
                current is not None
                and current.fingerprint == fingerprint
                and _pid_alive(current.pid)
                and self._ready(current.host, current.port)
            ):
                return current

            if current is not None and _pid_alive(current.pid):
                _terminate_pid(current.pid)
                self._state_path(home).unlink(missing_ok=True)

            port = relay_config.port or _find_free_port(relay_config.host)
            command = relay_config.command or self.config.relay_command
            parts = shlex.split(command)
            if not parts:
                raise RelayConfigError(f"relay command is empty in {relay_config.path}")
            executable = shutil.which(parts[0]) or (
                parts[0] if Path(parts[0]).is_file() else None
            )
            if executable is None:
                raise RelayUnavailableError(
                    f"relay executable not found: {parts[0]} "
                    "(install with `uv tool install codex-relay`)"
                )

            log_path = state_dir / "relay.log"
            try:
                log_handle = log_path.open("ab")
                process_env = dict(os.environ)
                process_env.update(
                    {
                        "CODEX_RELAY_UPSTREAM": relay_config.upstream,
                        "CODEX_RELAY_API_KEY": key,
                        "CODEX_RELAY_PORT": str(port),
                        "CODEX_RELAY_BIND": relay_config.host,
                    }
                )
                process = subprocess.Popen(
                    [executable, *parts[1:], *relay_config.extra_args],
                    cwd=str(home),
                    env=process_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RelayUnavailableError(
                    f"could not start relay for {relay_config.path}: {exc}"
                ) from exc
            finally:
                try:
                    log_handle.close()
                except UnboundLocalError:
                    pass

            state = RelayState(process.pid, port, relay_config.host, fingerprint, time.time())
            if not self._ready(state.host, state.port):
                if _pid_alive(process.pid):
                    _terminate_pid(process.pid)
                raise RelayUnavailableError(
                    f"relay did not become ready at {_base_url(state.host, state.port)}; "
                    f"see {log_path}"
                )
            self._write_state(home, state)
            return state

    def _api_key(self, home: Path, relay_config: RelayConfig) -> str:
        if relay_config.api_key_file is not None:
            path = relay_config.api_key_file
            if not path.is_absolute():
                path = home / path
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = None
            except (OSError, json.JSONDecodeError) as exc:
                raise RelayConfigError(f"could not read API key file {path}: {exc}") from exc
            if raw is not None:
                value = _lookup_key(raw, relay_config.api_key_field)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        if relay_config.api_key_env:
            value = os.environ.get(relay_config.api_key_env, "").strip()
            if value:
                return value

        source = relay_config.api_key_file or Path("<environment>")
        raise RelayConfigError(
            f"API key not found for relay ({relay_config.api_key_field}); "
            f"set {relay_config.api_key_env or '<none>'} or configure {source}"
        )

    def _state_dir(self, home: Path) -> Path:
        return home / RELAY_STATE_DIR_NAME

    def _state_path(self, home: Path) -> Path:
        return self._state_dir(home) / "state.json"

    def _read_state(self, home: Path) -> RelayState | None:
        path = self._state_path(home)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return RelayState.from_json(raw)

    def _write_state(self, home: Path, state: RelayState) -> None:
        self._state_path(home).write_text(
            json.dumps(state.as_json(), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _ready(host: str, port: int, *, timeout: float = DEFAULT_READY_TIMEOUT) -> bool:
        url = f"{_base_url(host, port)}/models"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            # A running relay may be configured to protect its local listener.
            return exc.code in {401, 403}
        except (OSError, urllib.error.URLError):
            return False


def _bool_value(value: object, name: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise RelayConfigError(f"relay {name} must be boolean: {path}")
    return value


def _string_value(value: object, name: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RelayConfigError(f"relay {name} must be a non-empty string: {path}")
    return value.strip()


def _optional_string(value: object, name: str, path: Path) -> str | None:
    if value is None or value == "":
        return None
    return _string_value(value, name, path)


def _port_value(value: object, path: Path) -> int | None:
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise RelayConfigError(f"relay port must be an integer from 1 to 65535 (or 0): {path}")
    return value


def _lookup_key(raw: object, field: str) -> object:
    value = raw
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _base_url(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{rendered_host}:{port}/v1"


def _fingerprint(relay_config: RelayConfig, key: str, default_command: str) -> str:
    command = relay_config.command or default_command
    material = "\0".join(
        [
            relay_config.upstream,
            relay_config.host,
            str(relay_config.port or "auto"),
            command,
            "\0".join(relay_config.extra_args),
            key,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _find_free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
