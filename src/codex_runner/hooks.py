import json
import math
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import IO

from .models import valid_thread_id
from .tmux_backend import (
    LABEL_PATTERN,
    SESSION_PATTERN,
    STATE_OPTION,
    THREAD_OPTION,
    TmuxBackend,
    TmuxError,
)

SESSION_ENV = "CODEX_RUNNER_SESSION"
TOKEN_ENV = "CODEX_RUNNER_TOKEN"
TMUX_ENV = "CODEX_RUNNER_TMUX"
TMUX_LABEL_ENV = "CODEX_RUNNER_TMUX_LABEL"
DOWNSTREAM_ENV = "CODEX_RUNNER_DOWNSTREAM_NOTIFY"


def hook_context(
    environ: dict[str, str],
) -> tuple[str, str, str, str] | None:
    session = environ.get(SESSION_ENV, "")
    token = environ.get(TOKEN_ENV, "")
    tmux_path = environ.get(TMUX_ENV, "")
    label = environ.get(TMUX_LABEL_ENV, "")
    if not SESSION_PATTERN.fullmatch(session):
        return None
    if len(token) != 32:
        return None
    try:
        int(token, 16)
    except ValueError:
        return None
    if not LABEL_PATTERN.fullmatch(label):
        return None
    path = Path(tmux_path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return session, token, str(path), label


def record_thread_id(
    payload: dict[str, object],
    environ: dict[str, str],
) -> int:
    context = hook_context(environ)
    if context is None or payload.get("hook_event_name") != "SessionStart":
        return 0
    thread_id = payload.get("session_id")
    if not valid_thread_id(thread_id):
        return 0
    session, token, tmux_path, label = context
    backend = TmuxBackend(tmux_path, label=label, environ=environ)
    if not backend.owns_session(session, token):
        return 0
    try:
        if not backend.set_option_once(session, THREAD_OPTION, thread_id):
            existing = backend.get_option(session, THREAD_OPTION)
            if existing not in ("", thread_id):
                return 0
        backend.set_option(session, STATE_OPTION, "running")
    except (OSError, TmuxError):
        return 0
    return 0


def close_delay(environ: dict[str, str]) -> float:
    try:
        value = float(environ.get("CODEX_RUNNER_CLOSE_DELAY_SECONDS", "2"))
    except ValueError:
        return 2.0
    if not math.isfinite(value):
        return 2.0
    return max(0.0, min(value, 10.0))


def close_worker(
    session: str,
    token: str,
    tmux_path: str,
    label: str,
    environ: dict[str, str],
) -> int:
    if (
        hook_context(
            {
                **environ,
                SESSION_ENV: session,
                TOKEN_ENV: token,
                TMUX_ENV: tmux_path,
                TMUX_LABEL_ENV: label,
            }
        )
        is None
    ):
        return 0
    time.sleep(close_delay(environ))
    try:
        backend = TmuxBackend(tmux_path, label=label, environ=environ)
        if not backend.owns_session(session, token):
            return 0
        backend.set_option(session, STATE_OPTION, "closing")
        if not backend.request_exit(session, token):
            backend.kill_owned_session(session, token)
            return 0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not backend.has_session(session):
                return 0
            time.sleep(0.1)
        backend.kill_owned_session(session, token)
    except (OSError, TmuxError):
        return 0
    return 0


def schedule_close(
    environ: dict[str, str],
) -> int:
    context = hook_context(environ)
    if context is None:
        return 0
    session, token, tmux_path, label = context
    with suppress(OSError):
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_runner.hooks",
                "--close",
                session,
                token,
                tmux_path,
                label,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=environ,
        )
    return 0


def downstream_command(environ: dict[str, str]) -> list[str] | None:
    raw = environ.get(DOWNSTREAM_ENV, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        return None
    return value


def forward_notification(
    raw_payload: str,
    environ: dict[str, str],
) -> None:
    command = downstream_command(environ)
    if command is None:
        return
    try:
        child_environment = environ.copy()
        for name in (
            DOWNSTREAM_ENV,
            SESSION_ENV,
            TOKEN_ENV,
            TMUX_ENV,
            TMUX_LABEL_ENV,
            "CODEX_RUNNER_CLOSE_DELAY_SECONDS",
            "TMUX",
            "TMUX_PANE",
            "TMUX_TMPDIR",
        ):
            child_environment.pop(name, None)
        process = subprocess.Popen(
            [*command, raw_payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=child_environment,
        )
    except OSError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with suppress(OSError, ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)


def notify_main(
    raw_payload: str,
    environ: dict[str, str] | None = None,
) -> int:
    environment = os.environ.copy() if environ is None else environ.copy()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("type") == "agent-turn-complete":
        schedule_close(environment)
    forward_notification(raw_payload, environment)
    return 0


def handle_payload(
    payload: object,
    environ: dict[str, str],
) -> int:
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return record_thread_id(payload, environ)
    return 0


def main(
    argv: list[str] | None = None,
    environ: dict[str, str] | None = None,
    stdin: IO[str] | None = None,
) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    environment = os.environ.copy() if environ is None else environ.copy()
    if len(arguments) == 5 and arguments[0] == "--close":
        return close_worker(
            arguments[1],
            arguments[2],
            arguments[3],
            arguments[4],
            environment,
        )
    if arguments:
        return 0
    source = sys.stdin if stdin is None else stdin
    try:
        payload = json.load(source)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return 0
    return handle_payload(payload, environment)


if __name__ == "__main__":
    raise SystemExit(main())
