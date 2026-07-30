import fcntl
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from . import APP_NAME
from .context import valid_session_name, valid_tmux_label
from .models import CreatedSession, RunnerSession, SessionLaunch, valid_thread_id


class TmuxError(RuntimeError):
    pass


class SessionAlreadyRunning(TmuxError):
    def __init__(self, session: str) -> None:
        self.session = session
        super().__init__(f"Codex thread is already running in {session}")


PANE_PATTERN = re.compile(r"^%[0-9]+$")
PANE_INTERNAL_ENVIRONMENT = frozenset({"TERM", "TMUX", "TMUX_PANE"})
OWNED_OPTION = "@codex_runner_owned"
PANE_OPTION = "@codex_runner_pane_id"
THREAD_OPTION = "@codex_runner_thread_id"
REQUESTED_THREAD_OPTION = "@codex_runner_requested_thread_id"
TOKEN_OPTION = "@codex_runner_token"
STATE_OPTION = "@codex_runner_state"


class TmuxBackend:
    def __init__(
        self,
        tmux_path: str,
        *,
        label: str = APP_NAME,
        environ: dict[str, str] | None = None,
    ) -> None:
        if not valid_tmux_label(label):
            raise ValueError("invalid tmux server label")
        self.tmux_path = str(Path(tmux_path))
        self.label = label
        self.environ = os.environ.copy() if environ is None else environ.copy()
        self.environ.pop("TMUX_TMPDIR", None)

    def command(self, *arguments: str) -> list[str]:
        return [
            self.tmux_path,
            "-L",
            self.label,
            "-f",
            "/dev/null",
            *arguments,
        ]

    def run(
        self,
        *arguments: str,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments),
            stdin=None,
            text=True,
            capture_output=capture,
            env=self.environ,
            check=False,
        )

    @staticmethod
    def _no_server(result: subprocess.CompletedProcess[str]) -> bool:
        message = f"{result.stdout}\n{result.stderr}".casefold()
        return (
            "no server running" in message
            or "no sessions" in message
            or "failed to connect to server" in message
            or "error connecting to" in message
            or "server exited unexpectedly" in message
        )

    @staticmethod
    def _message(
        result: subprocess.CompletedProcess[str],
        fallback: str,
    ) -> str:
        detail = (result.stderr or result.stdout or "").strip()
        return detail or fallback

    def list_sessions(self) -> list[RunnerSession]:
        result = self.run(
            "list-sessions",
            "-F",
            (
                "#{session_name}\t#{session_attached}\t"
                f"#{{{OWNED_OPTION}}}\t#{{{THREAD_OPTION}}}\t"
                f"#{{{REQUESTED_THREAD_OPTION}}}\t#{{{STATE_OPTION}}}\t"
                "#{session_created}"
            ),
        )
        if result.returncode:
            if self._no_server(result):
                return []
            raise TmuxError(self._message(result, "could not list tmux sessions"))
        sessions: list[RunnerSession] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            (
                name,
                attached,
                owned,
                thread_id,
                requested_thread_id,
                state,
                created_at,
            ) = parts
            if owned != "1" or not valid_session_name(name):
                continue
            try:
                attached_count = int(attached)
                created = int(created_at)
            except ValueError:
                continue
            sessions.append(
                RunnerSession(
                    name=name,
                    attached=attached_count,
                    thread_id=thread_id or requested_thread_id or None,
                    created_at=created,
                    state=state or "unknown",
                )
            )
        return sessions

    def has_session(self, session: str) -> bool:
        if not valid_session_name(session):
            return False
        result = self.run("has-session", "-t", f"={session}")
        if result.returncode == 0:
            return True
        message = f"{result.stdout}\n{result.stderr}".casefold()
        if self._no_server(result) or "can't find session" in message:
            return False
        raise TmuxError(self._message(result, "could not query the tmux session"))

    def get_option(self, session: str, option: str) -> str | None:
        if not valid_session_name(session):
            return None
        result = self.run(
            "show-options",
            "-v",
            "-t",
            f"={session}:",
            option,
        )
        if result.returncode:
            message = f"{result.stdout}\n{result.stderr}".casefold()
            if (
                self._no_server(result)
                or "can't find session" in message
                or "no such session" in message
                or "invalid option" in message
            ):
                return None
            raise TmuxError(self._message(result, "could not query a tmux option"))
        return result.stdout.rstrip("\n")

    def set_option(self, session: str, option: str, value: str) -> None:
        if not valid_session_name(session):
            raise TmuxError("invalid runner session name")
        result = self.run(
            "set-option",
            "-t",
            f"={session}:",
            option,
            value,
        )
        if result.returncode:
            raise TmuxError(
                self._message(result, f"could not set tmux option {option}")
            )

    def set_option_once(self, session: str, option: str, value: str) -> bool:
        if not valid_session_name(session):
            return False
        result = self.run(
            "set-option",
            "-o",
            "-t",
            f"={session}:",
            option,
            value,
        )
        return result.returncode == 0

    def create_session(
        self,
        *,
        cwd: str,
        launch: SessionLaunch | Callable[[str, str], SessionLaunch],
        thread_id: str | None = None,
    ) -> CreatedSession:
        if thread_id is not None and not valid_thread_id(thread_id):
            raise TmuxError("invalid Codex thread id")
        if thread_id is not None:
            with self._creation_lock():
                for active in self.list_sessions():
                    if active.thread_id == thread_id:
                        raise SessionAlreadyRunning(active.name)
                return self._create_session(cwd, launch, thread_id)
        return self._create_session(cwd, launch, thread_id)

    @contextmanager
    def _creation_lock(self) -> Iterator[None]:
        # A fixed, per-UID path makes the lock shared even when two terminals
        # have different TMPDIR values. O_NOFOLLOW plus the checks below make
        # using the sticky temporary directory fail closed.
        lock_path = Path("/tmp") / f".{APP_NAME}-create-{os.getuid()}.lock"  # noqa: S108
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise TmuxError(f"could not open the resume lock: {error}") from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise TmuxError(f"unsafe resume lock file: {lock_path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise TmuxError(
                    f"could not acquire the resume lock: {error}"
                ) from error
            yield
        finally:
            os.close(descriptor)

    def _create_session(
        self,
        cwd: str,
        launch: SessionLaunch | Callable[[str, str], SessionLaunch],
        thread_id: str | None,
    ) -> CreatedSession:
        session = "job-" + secrets.token_hex(16)
        token = secrets.token_hex(16)
        result = self.run(
            "new-session",
            "-E",
            "-d",
            "-P",
            "-F",
            "#{session_id}\t#{pane_id}",
            "-s",
            session,
            "-c",
            cwd,
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        )
        if result.returncode:
            raise TmuxError(
                self._message(result, "could not create background tmux session")
            )
        identifiers = result.stdout.strip().split("\t")
        if (
            len(identifiers) != 2
            or not identifiers[0].startswith("$")
            or not identifiers[1].startswith("%")
        ):
            self.run("kill-session", "-t", f"={session}")
            raise TmuxError("tmux did not return session identifiers")
        session_id, pane_id = identifiers
        try:
            for option, value in (
                (OWNED_OPTION, "1"),
                (PANE_OPTION, pane_id),
                (STATE_OPTION, "starting"),
                ("mouse", "on"),
                ("status", "off"),
                ("destroy-unattached", "off"),
            ):
                result = self.run(
                    "set-option",
                    "-t",
                    session_id,
                    option,
                    value,
                )
                if result.returncode:
                    raise TmuxError(
                        self._message(
                            result,
                            f"could not set tmux option {option}",
                        )
                    )
            if thread_id:
                result = self.run(
                    "set-option",
                    "-t",
                    session_id,
                    REQUESTED_THREAD_OPTION,
                    thread_id,
                )
                if result.returncode:
                    raise TmuxError(
                        self._message(
                            result,
                            (f"could not set tmux option {REQUESTED_THREAD_OPTION}"),
                        )
                    )
            result = self.run(
                "set-option",
                "-t",
                session_id,
                TOKEN_OPTION,
                token,
            )
            if result.returncode:
                raise TmuxError(
                    self._message(
                        result,
                        f"could not set tmux option {TOKEN_OPTION}",
                    )
                )
            resolved_launch = launch(session, token) if callable(launch) else launch
            resolved_command = list(resolved_launch.command)
            resolved_environment = dict(resolved_launch.environment)
            if not resolved_command:
                raise TmuxError("runner command is empty")
            self._replace_session_environment(
                session_id,
                resolved_environment,
            )
            respawn_arguments = [
                "respawn-pane",
                "-k",
                "-t",
                pane_id,
                "-c",
                cwd,
            ]
            for name, value in sorted(resolved_environment.items()):
                if name in PANE_INTERNAL_ENVIRONMENT or name == "PWD":
                    continue
                respawn_arguments.extend(["-e", f"{name}={value}"])
            respawn_arguments.extend(["-e", f"PWD={cwd}", *resolved_command])
            result = self.run(*respawn_arguments)
            if result.returncode:
                raise TmuxError(
                    self._message(result, "could not start Codex inside tmux")
                )
        except BaseException:
            with suppress(OSError):
                self.run("kill-session", "-t", f"={session}")
            raise
        return CreatedSession(name=session, token=token)

    def _replace_session_environment(
        self,
        session_id: str,
        environment: dict[str, str],
    ) -> None:
        global_names: set[str] = set()
        # tmux's -h output contains only hidden variables, not the ordinary
        # global environment, so read both views before constructing removals.
        for arguments in (
            ("show-environment", "-g"),
            ("show-environment", "-gh"),
        ):
            result = self.run(*arguments)
            if result.returncode:
                raise TmuxError(
                    self._message(
                        result,
                        "could not query the tmux global environment",
                    )
                )
            for line in result.stdout.splitlines():
                if not line:
                    continue
                if "=" in line:
                    name = line.split("=", 1)[0]
                elif line.startswith("-"):
                    name = line[1:]
                else:
                    name = line
                if name:
                    global_names.add(name)

        stale_names = global_names - environment.keys() - PANE_INTERNAL_ENVIRONMENT
        for name in sorted(stale_names):
            result = self.run(
                "set-environment",
                "-r",
                "-t",
                session_id,
                name,
            )
            if result.returncode:
                raise TmuxError(
                    self._message(
                        result,
                        f"could not remove stale tmux environment variable {name}",
                    )
                )

    def owns_session(self, session: str, token: str) -> bool:
        return (
            bool(token)
            and self.get_option(session, OWNED_OPTION) == "1"
            and self.get_option(session, TOKEN_OPTION) == token
        )

    def kill_owned_session(self, session: str, token: str | None = None) -> bool:
        if self.get_option(session, OWNED_OPTION) != "1":
            return False
        if token is not None and self.get_option(session, TOKEN_OPTION) != token:
            return False
        result = self.run("kill-session", "-t", f"={session}")
        if result.returncode == 0 or not self.has_session(session):
            return True
        raise TmuxError(self._message(result, "could not close the tmux session"))

    def request_exit(self, session: str, token: str) -> bool:
        if not self.owns_session(session, token):
            return False
        pane = self.get_option(session, PANE_OPTION)
        if pane is None or not PANE_PATTERN.fullmatch(pane):
            return False
        owner = self.run(
            "display-message",
            "-p",
            "-t",
            pane,
            "#{session_name}",
        )
        if owner.returncode or owner.stdout.rstrip("\n") != session:
            return False
        typed = self.run("send-keys", "-t", pane, "-l", "/exit")
        if typed.returncode:
            if not self.has_session(session):
                return False
            raise TmuxError(self._message(typed, "could not request Codex exit"))
        entered = self.run("send-keys", "-t", pane, "Enter")
        if entered.returncode == 0:
            return True
        if not self.has_session(session):
            return False
        raise TmuxError(
            self._message(entered, "could not submit the Codex exit request")
        )

    def socket_path(self) -> str | None:
        result = self.run("display-message", "-p", "#{socket_path}")
        if result.returncode:
            return None
        value = result.stdout.strip()
        return value or None

    def validate_attach_context(self) -> None:
        current = self.environ.get("TMUX", "")
        if not current:
            return
        current_socket = current.split(",", 1)[0]
        target_socket = self.socket_path()
        if not target_socket or os.path.realpath(current_socket) != os.path.realpath(
            target_socket
        ):
            raise TmuxError(
                "cannot attach from inside another tmux server; "
                "detach from that tmux first"
            )

    def attach(
        self,
        session: str,
        *,
        created_token: str | None = None,
    ) -> None:
        if not valid_session_name(session):
            raise TmuxError("invalid runner session name")
        current = self.environ.get("TMUX", "")
        if current:
            result = self.run(
                "switch-client",
                "-t",
                f"={session}",
                capture=False,
            )
        else:
            result = self.run(
                "attach-session",
                "-t",
                f"={session}",
                capture=False,
            )
        if result.returncode == 0:
            return
        exists = self.has_session(session)
        if not exists:
            message = (
                "Codex exited before the terminal could attach"
                if created_token is not None
                else "the Codex session ended before the terminal could attach"
            )
            raise TmuxError(message)
        if created_token is not None:
            self.kill_owned_session(session, created_token)
        raise TmuxError(self._message(result, "could not attach to the tmux session"))
