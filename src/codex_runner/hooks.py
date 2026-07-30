import json
import math
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from typing import IO

from .context import RunnerContext
from .models import valid_thread_id
from .tmux_backend import (
    STATE_OPTION,
    THREAD_OPTION,
    TmuxBackend,
    TmuxError,
)


def record_thread_id(
    payload: dict[str, object],
    environ: dict[str, str],
) -> int:
    context = RunnerContext.from_environment(environ)
    if context is None or payload.get("hook_event_name") != "SessionStart":
        return 0
    thread_id = payload.get("session_id")
    if not valid_thread_id(thread_id):
        return 0
    backend = TmuxBackend(
        context.tmux_path,
        label=context.label,
        environ=environ,
    )
    if not backend.owns_session(context.session, context.token):
        return 0
    try:
        if not backend.set_option_once(context.session, THREAD_OPTION, thread_id):
            existing = backend.get_option(context.session, THREAD_OPTION)
            if existing not in ("", thread_id):
                return 0
        backend.set_option(context.session, STATE_OPTION, "running")
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
    environ: dict[str, str],
) -> int:
    context = RunnerContext.from_environment(environ)
    if context is None:
        return 0
    time.sleep(close_delay(environ))
    try:
        backend = TmuxBackend(
            context.tmux_path,
            label=context.label,
            environ=environ,
        )
        if not backend.owns_session(context.session, context.token):
            return 0
        backend.set_option(context.session, STATE_OPTION, "closing")
        if not backend.request_exit(context.session, context.token):
            backend.kill_owned_session(context.session, context.token)
            return 0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not backend.has_session(context.session):
                return 0
            time.sleep(0.1)
        backend.kill_owned_session(context.session, context.token)
    except (OSError, TmuxError):
        return 0
    return 0


def schedule_close(
    environ: dict[str, str],
) -> int:
    if RunnerContext.from_environment(environ) is None:
        return 0
    with suppress(OSError):
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_runner.hooks",
                "--close",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=environ,
        )
    return 0


def forward_notification(
    raw_payload: str,
    context: RunnerContext,
    environ: dict[str, str],
) -> None:
    if context.downstream_notify is None:
        return
    try:
        process = subprocess.Popen(
            [*context.downstream_notify, raw_payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=context.downstream_environment(environ),
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
    context = RunnerContext.from_environment(environment)
    if context is None:
        return 0
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("type") == "agent-turn-complete":
        schedule_close(environment)
    forward_notification(raw_payload, context, environment)
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
    if arguments == ["--close"]:
        return close_worker(environment)
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
