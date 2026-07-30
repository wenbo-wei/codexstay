import curses
import json
import os
import shutil
import sys
from pathlib import Path
from typing import NoReturn

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from . import APP_NAME, __version__
from .codex_adapter import CodexAdapterError, list_threads
from .context import RunnerContext
from .models import SessionLaunch
from .picker import resume_picker
from .profile import profile_path, write_profile
from .tmux_backend import SessionAlreadyRunning, TmuxBackend, TmuxError

COMMAND_NAME = APP_NAME
PROFILE_NAME = APP_NAME
TMUX_LABEL = APP_NAME


def die(message: str, code: int = 1) -> NoReturn:
    print(f"{COMMAND_NAME}: {message}", file=sys.stderr)
    raise SystemExit(code)


def executable(command: str) -> str:
    value = shutil.which(command)
    if value is None:
        die(f"{command} command not found")
    return str(Path(value).resolve())


def runner_command_path() -> str:
    value = shutil.which(COMMAND_NAME)
    return str(Path(value if value is not None else sys.argv[0]).resolve())


def ensure_interactive() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        die("an interactive terminal is required")
    term = os.environ.get("TERM", "")
    if not term or term.casefold() == "dumb":
        die("TERM must describe an interactive terminal")


def ensure_profile() -> None:
    if not profile_path(PROFILE_NAME).is_file():
        die(
            f"Codex profile {PROFILE_NAME!r} is not installed; run {COMMAND_NAME} setup"
        )


def configured_notify() -> list[str] | None:
    config = profile_path(PROFILE_NAME).with_name("config.toml")
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8")).get("notify")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return value
    return None


def start_protected(
    backend: TmuxBackend,
    *,
    codex_path: str,
    cwd: str,
    resume_thread_id: str | None = None,
    native_resume_picker: bool = False,
    command_path: str | None = None,
) -> None:
    target = Path(cwd)
    if not target.is_dir():
        target = Path.cwd()
    target = target.absolute()
    runner_path = (
        str(Path(command_path).resolve())
        if command_path is not None
        else runner_command_path()
    )
    notify_value = json.dumps(
        [runner_path, "_notify"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    codex_arguments = [
        codex_path,
        "--profile",
        PROFILE_NAME,
        "--config",
        f"notify={notify_value}",
    ]
    if native_resume_picker:
        codex_arguments.extend(["resume", "--all"])
    elif resume_thread_id:
        codex_arguments.extend(["resume", resume_thread_id])

    downstream = configured_notify()
    if downstream and len(downstream) >= 2:
        try:
            same_command = (
                Path(downstream[0]).resolve() == Path(runner_path).resolve()
                and downstream[1] == "_notify"
            )
        except OSError:
            same_command = False
        if same_command:
            downstream = None
    downstream_notify = tuple(downstream) if downstream else None

    try:
        created = backend.create_session(
            cwd=str(target),
            launch=lambda assigned_session, assigned_token: SessionLaunch(
                command=list(codex_arguments),
                environment=RunnerContext(
                    session=assigned_session,
                    token=assigned_token,
                    tmux_path=backend.tmux_path,
                    label=TMUX_LABEL,
                    downstream_notify=downstream_notify,
                ).to_environment(os.environ),
            ),
            thread_id=resume_thread_id,
        )
    except SessionAlreadyRunning as running:
        backend.attach(running.session)
        return
    backend.attach(created.name, created_token=created.token)


def active_thread_sessions(
    backend: TmuxBackend,
) -> dict[str, str]:
    sessions = sorted(
        backend.list_sessions(),
        key=lambda session: (bool(session.attached), session.created_at),
        reverse=True,
    )
    active: dict[str, str] = {}
    for session in sessions:
        if session.thread_id:
            active.setdefault(session.thread_id, session.name)
    return active


def command_resume(
    backend: TmuxBackend,
    codex_path: str,
) -> None:
    scope = os.environ.get("CODEX_RUNNER_HISTORY_SCOPE", "platform").casefold()
    if scope not in {"platform", "all"}:
        scope = "platform"
    try:
        records = list_threads(codex_path, scope=scope)
    except CodexAdapterError as error:
        print(
            f"{COMMAND_NAME}: custom history unavailable ({error}); "
            "using the native Codex picker",
            file=sys.stderr,
        )
        start_protected(
            backend,
            codex_path=codex_path,
            cwd=os.getcwd(),
            native_resume_picker=True,
        )
        return
    if not records:
        return
    active = active_thread_sessions(backend)
    try:
        selected = resume_picker(records, active)
    except curses.error as error:
        die(f"could not open the resume picker: {error}")
    if selected is None:
        return
    active_session = active.get(selected.id)
    if active_session:
        backend.attach(active_session)
        return
    start_protected(
        backend,
        codex_path=codex_path,
        cwd=selected.cwd,
        resume_thread_id=selected.id,
    )


def install_profile(force: bool) -> None:
    command_path = runner_command_path()
    try:
        path = write_profile(
            command_path,
            profile_name=PROFILE_NAME,
            force=force,
        )
    except (FileExistsError, OSError) as error:
        die(str(error))
    print(path)


def print_help() -> None:
    print(
        f"""\
Usage:
  {COMMAND_NAME}          start a new disconnect-safe Codex turn
  {COMMAND_NAME} resume   search for and resume a Codex thread

Maintenance:
  {COMMAND_NAME} setup [--force]   install the dedicated Codex profile
  {COMMAND_NAME} --version         show the runner version
"""
    )


def main() -> None:
    arguments = sys.argv[1:]
    if arguments == ["_hook"]:
        from .hooks import main as hook_main

        raise SystemExit(hook_main([]))
    if len(arguments) == 2 and arguments[0] == "_notify":
        from .hooks import notify_main

        raise SystemExit(notify_main(arguments[1]))
    if arguments in (["--help"], ["-h"]):
        print_help()
        return
    if arguments == ["--version"]:
        print(f"{COMMAND_NAME} {__version__}")
        return
    if arguments in (["setup"], ["setup", "--force"]):
        install_profile(force="--force" in arguments)
        return
    if arguments not in ([], ["resume"]):
        die(f"Usage: {COMMAND_NAME} | {COMMAND_NAME} resume")

    ensure_interactive()
    ensure_profile()
    tmux_path = executable("tmux")
    codex_path = executable("codex")
    backend = TmuxBackend(tmux_path, label=TMUX_LABEL)
    try:
        backend.validate_attach_context()
        if arguments == ["resume"]:
            command_resume(backend, codex_path)
        else:
            start_protected(
                backend,
                codex_path=codex_path,
                cwd=os.getcwd(),
            )
    except TmuxError as error:
        die(str(error))


if __name__ == "__main__":
    main()
