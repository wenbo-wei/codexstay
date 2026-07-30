import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

SESSION_ENV = "CODEX_RUNNER_SESSION"
TOKEN_ENV = "CODEX_RUNNER_TOKEN"
TMUX_ENV = "CODEX_RUNNER_TMUX"
TMUX_LABEL_ENV = "CODEX_RUNNER_TMUX_LABEL"
DOWNSTREAM_ENV = "CODEX_RUNNER_DOWNSTREAM_NOTIFY"

_RUNNER_ENV_PREFIX = "CODEX_RUNNER_"
_TMUX_TRANSPORT_ENV = frozenset({"TMUX", "TMUX_PANE", "TMUX_TMPDIR"})
_SESSION_PATTERN = re.compile(r"^job-(?:[0-9a-f]{12}|[0-9a-f]{32})$")
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_PATTERN = re.compile(r"^[0-9A-Fa-f]{32}$")


def valid_session_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _SESSION_PATTERN.fullmatch(value) is not None


def valid_tmux_label(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _LABEL_PATTERN.fullmatch(value) is not None


def _valid_token(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def _valid_tmux_path(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        path = Path(value)
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _valid_command_argument(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return True


def _downstream_from_wire(value: object) -> tuple[str, ...] | None:
    if (
        not isinstance(value, list)
        or not value
        or not all(_valid_command_argument(item) for item in value)
    ):
        return None
    return tuple(value)


@dataclass(frozen=True)
class RunnerContext:
    session: str
    token: str
    tmux_path: str
    label: str
    downstream_notify: tuple[str, ...] | None = None

    def _valid(self) -> bool:
        return (
            valid_session_name(self.session)
            and _valid_token(self.token)
            and _valid_tmux_path(self.tmux_path)
            and valid_tmux_label(self.label)
            and (
                self.downstream_notify is None
                or (
                    bool(self.downstream_notify)
                    and all(
                        _valid_command_argument(item) for item in self.downstream_notify
                    )
                )
            )
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> "RunnerContext | None":
        session = environ.get(SESSION_ENV)
        token = environ.get(TOKEN_ENV)
        tmux_path = environ.get(TMUX_ENV)
        label = environ.get(TMUX_LABEL_ENV)
        if (
            not valid_session_name(session)
            or not _valid_token(token)
            or not _valid_tmux_path(tmux_path)
            or not valid_tmux_label(label)
        ):
            return None

        downstream: tuple[str, ...] | None = None
        if DOWNSTREAM_ENV in environ:
            try:
                decoded = json.loads(environ[DOWNSTREAM_ENV])
            except (ValueError, UnicodeError, RecursionError):
                return None
            downstream = _downstream_from_wire(decoded)
            if downstream is None:
                return None

        return cls(
            session=session,
            token=token,
            tmux_path=tmux_path,
            label=label,
            downstream_notify=downstream,
        )

    def to_environment(
        self,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        if not self._valid():
            raise ValueError("invalid runner context")
        environment = self._without_runner_state(base)
        environment.update(
            {
                SESSION_ENV: self.session,
                TOKEN_ENV: self.token,
                TMUX_ENV: self.tmux_path,
                TMUX_LABEL_ENV: self.label,
            }
        )
        if self.downstream_notify is not None:
            environment[DOWNSTREAM_ENV] = json.dumps(
                self.downstream_notify,
                separators=(",", ":"),
            )
        return environment

    def downstream_environment(
        self,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        return self._without_runner_state(base)

    @staticmethod
    def _without_runner_state(
        base: Mapping[str, str],
    ) -> dict[str, str]:
        return {
            name: value
            for name, value in base.items()
            if not name.startswith(_RUNNER_ENV_PREFIX)
            and name not in _TMUX_TRANSPORT_ENV
        }
