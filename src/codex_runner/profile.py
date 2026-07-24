import json
import os
import shlex
import tempfile
from contextlib import suppress
from pathlib import Path

from . import APP_NAME

MANAGED_MARKER = f"# Managed by {APP_NAME}."


def codex_home(environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def profile_path(
    profile_name: str,
    environ: dict[str, str] | None = None,
) -> Path:
    return codex_home(environ) / f"{profile_name}.config.toml"


def render_profile(command_path: str) -> str:
    resolved = str(Path(command_path).resolve())
    hook_command = shlex.join([resolved, "_hook"])
    toml_hook_command = json.dumps(hook_command, ensure_ascii=False)
    notify_command = json.dumps([resolved, "_notify"], ensure_ascii=False)
    return f"""\
{MANAGED_MARKER}

notify = {notify_command}

[[hooks.SessionStart]]
matcher = "startup|resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = {toml_hook_command}
timeout = 5
"""


def write_profile(
    command_path: str,
    *,
    profile_name: str,
    force: bool = False,
    environ: dict[str, str] | None = None,
) -> Path:
    path = profile_path(profile_name, environ)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = render_profile(command_path)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current.startswith(content):
            return path
        if not force:
            if current.startswith(MANAGED_MARKER):
                raise FileExistsError(
                    f"{path} differs from the current managed profile; "
                    "run setup --force and review the hook again"
                )
            raise FileExistsError(
                f"{path} already exists and is not managed by this tool"
            )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return path
