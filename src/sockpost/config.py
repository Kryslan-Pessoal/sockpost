"""Runtime configuration: filesystem locations and tuning knobs.

Every value can be overridden with an environment variable prefixed with
``SOCKPOST_``. Defaults follow the XDG Base Directory specification so that a
plain ``pipx install sockpost`` needs no configuration at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "sockpost"
ENV_PREFIX = "SOCKPOST_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / APP_NAME


def default_socket_path() -> Path:
    """Socket location.

    ``XDG_RUNTIME_DIR`` when available (Linux), otherwise ``TMPDIR`` with the
    user id in the name so two accounts on the same host never collide.
    Unix socket paths are limited to about 100 bytes by the kernel, which is
    why the runtime directory is preferred over a deep home directory.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / APP_NAME / "daemon.sock"
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return Path(tmp) / ("%s-%d.sock" % (APP_NAME, os.getuid()))


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one process."""

    socket_path: Path
    db_path: Path
    pid_path: Path
    log_path: Path

    # Delivery pacing.
    delivery_gap: float
    redeliver_gap: int
    drain_batch: int

    # How long the daemon will wait on a write to a peer before giving up on
    # it. Without this a consumer that stops reading blocks the daemon.
    write_timeout: float

    # Acknowledgement watchdog.
    ack_timeout_online: int
    ack_timeout_offline: int

    # Limits.
    max_body_bytes: int

    # Housekeeping.
    orphan_ttl: int
    orphan_sweep_interval: int

    # Client behaviour.
    autostart: bool
    default_id: str | None

    @property
    def log_max_bytes(self) -> int:
        return 10 * 1024 * 1024


def load_settings() -> Settings:
    """Build the settings for the current process from the environment."""
    socket_path = Path(_env("SOCKET") or default_socket_path())
    db_path = Path(_env("DB") or (data_dir() / "queue.db"))
    pid_path = Path(_env("PID") or (state_dir() / "daemon.pid"))
    log_path = Path(_env("LOG") or (state_dir() / "daemon.log"))
    return Settings(
        socket_path=socket_path,
        db_path=db_path,
        pid_path=pid_path,
        log_path=log_path,
        delivery_gap=_env_float("DELIVERY_GAP", 1.0),
        redeliver_gap=_env_int("REDELIVER_GAP", 120),
        drain_batch=_env_int("DRAIN_BATCH", 10),
        write_timeout=_env_float("WRITE_TIMEOUT", 5.0),
        ack_timeout_online=_env_int("ACK_TIMEOUT_ONLINE", 30),
        ack_timeout_offline=_env_int("ACK_TIMEOUT_OFFLINE", 60),
        max_body_bytes=_env_int("MAX_BODY_BYTES", 1024 * 1024),
        orphan_ttl=_env_int("ORPHAN_TTL", 6 * 3600),
        orphan_sweep_interval=_env_int("ORPHAN_SWEEP_INTERVAL", 900),
        autostart=_env_bool("AUTOSTART", True),
        default_id=_env("ID"),
    )


# The kernel truncates Unix socket paths at sun_path, which is 104 bytes on
# macOS and 108 on Linux. Bind fails with a bare "path too long" errno, so the
# limit is checked up front and reported with an actionable message instead.
SOCKET_PATH_LIMIT = 100


def socket_path_problem(path) -> str | None:
    """Return a human readable problem with the socket path, or ``None``."""
    encoded = os.fsencode(str(path))
    if len(encoded) > SOCKET_PATH_LIMIT:
        return ("socket path is %d bytes long and the kernel limit is about %d; "
                "set SOCKPOST_SOCKET to something shorter, for example "
                "/tmp/%s.sock" % (len(encoded), SOCKET_PATH_LIMIT, APP_NAME))
    return None


def ensure_parent(path: Path, mode: int = 0o700) -> None:
    """Create the parent directory of ``path``.

    Permissions are only tightened on directories this process created. An
    existing directory is left alone on purpose: the socket may legitimately
    live in a shared location such as ``/tmp``, and changing the mode of a
    directory owned by somebody else is never this tool's business.
    """
    parent = path.parent
    if parent.exists():
        return
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, mode)
    except OSError:
        pass
