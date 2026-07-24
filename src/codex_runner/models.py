from dataclasses import dataclass
from typing import TypeGuard


def valid_thread_id(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(character.isprintable() and character != "\t" for character in value)
    )


@dataclass(frozen=True)
class ThreadRecord:
    id: str
    cwd: str
    title: str
    preview: str
    created_at_ms: int
    updated_at_ms: int
    recency_at_ms: int


@dataclass(frozen=True)
class RunnerSession:
    name: str
    attached: int
    thread_id: str | None
    created_at: int
    state: str = "unknown"


@dataclass(frozen=True)
class CreatedSession:
    name: str
    token: str
