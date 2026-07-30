import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_TITLE, __version__
from .models import ThreadRecord, valid_thread_id
from .text import sanitize_terminal_text, truncate_display


class CodexAdapterError(RuntimeError):
    pass


_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_STDOUT_BUFFER = 4 * 1024 * 1024


def belongs_to_current_platform(cwd: str, platform: str | None = None) -> bool:
    platform = sys.platform if platform is None else platform
    if not cwd:
        return False
    if platform.startswith("linux"):
        return not (
            cwd == "/Users"
            or cwd.startswith("/Users/")
            or cwd.startswith("\\\\")
            or bool(_WINDOWS_PATH.match(cwd))
        )
    if platform in {"darwin", "macos"}:
        return not (
            cwd == "/home"
            or cwd.startswith("/home/")
            or cwd.startswith("\\\\")
            or bool(_WINDOWS_PATH.match(cwd))
        )
    return True


def record_from_wire(value: object) -> ThreadRecord | None:
    if not isinstance(value, dict):
        return None
    thread_id = value.get("id")
    cwd = value.get("cwd")
    if not valid_thread_id(thread_id):
        return None
    if not isinstance(cwd, str) or not cwd:
        return None

    def seconds_to_ms(field: str) -> int:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0
        try:
            seconds = float(raw)
        except (OverflowError, ValueError):
            return 0
        milliseconds = seconds * 1000
        if not math.isfinite(milliseconds):
            return 0
        return max(0, int(milliseconds))

    title = value.get("name")
    preview = value.get("preview")
    return ThreadRecord(
        id=thread_id,
        cwd=cwd,
        title=title if isinstance(title, str) else "",
        preview=preview if isinstance(preview, str) else "",
        created_at_ms=seconds_to_ms("createdAt"),
        updated_at_ms=seconds_to_ms("updatedAt"),
        recency_at_ms=seconds_to_ms("recencyAt"),
    )


class AppServerClient:
    def __init__(
        self,
        codex_path: str,
        *,
        timeout: float = 5.0,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.codex_path = str(Path(codex_path))
        self.timeout = timeout
        self.environ = os.environ.copy() if environ is None else environ.copy()
        self.process: subprocess.Popen[bytes] | None = None
        self.selector: selectors.BaseSelector | None = None
        self.stdout_buffer = bytearray()
        self.stderr_buffer = bytearray()
        self.request_id = 0
        self.platform_os: str | None = None

    def __enter__(self) -> "AppServerClient":
        try:
            self.process = subprocess.Popen(
                [self.codex_path, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environ,
                bufsize=0,
                start_new_session=True,
            )
            if self.process.stdout is None or self.process.stderr is None:
                raise CodexAdapterError("could not open Codex app-server output pipes")
            self.selector = selectors.DefaultSelector()
            self.selector.register(
                self.process.stdout,
                selectors.EVENT_READ,
                "stdout",
            )
            self.selector.register(
                self.process.stderr,
                selectors.EVENT_READ,
                "stderr",
            )
            response = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": APP_NAME,
                        "title": APP_TITLE,
                        "version": __version__,
                    }
                },
            )
            if not isinstance(response, dict):
                raise CodexAdapterError(
                    "Codex app-server returned an invalid handshake"
                )
            platform_os = response.get("platformOs")
            if isinstance(platform_os, str) and platform_os:
                self.platform_os = platform_os.casefold()
            self.notify("initialized", {})
            return self
        except OSError as error:
            self.close()
            raise CodexAdapterError(
                f"could not start Codex app-server: {error}"
            ) from error
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @classmethod
    def _wait_for_process_group(
        cls,
        process: subprocess.Popen[bytes],
        process_group_id: int,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with suppress(Exception):
                process.poll()
            if not cls._process_group_exists(process_group_id):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    @staticmethod
    def _signal_process_group(process_group_id: int, signal_number: int) -> None:
        with suppress(OSError):
            os.killpg(process_group_id, signal_number)

    def close(self) -> None:
        process = self.process
        selector = self.selector
        self.selector = None
        if selector is not None:
            with suppress(Exception):
                selector.close()
        if process is None:
            return
        process_group_id = process.pid
        try:
            if process.stdin is not None:
                with suppress(Exception):
                    process.stdin.close()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)

            if self._process_group_exists(process_group_id):
                self._signal_process_group(process_group_id, signal.SIGTERM)
                if not self._wait_for_process_group(
                    process,
                    process_group_id,
                    timeout=1,
                ):
                    self._signal_process_group(process_group_id, signal.SIGKILL)
                    self._wait_for_process_group(
                        process,
                        process_group_id,
                        timeout=1,
                    )
        except Exception:
            self._signal_process_group(process_group_id, signal.SIGKILL)
            with suppress(Exception):
                self._wait_for_process_group(
                    process,
                    process_group_id,
                    timeout=1,
                )
        finally:
            with suppress(Exception):
                process.wait(timeout=0)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with suppress(Exception):
                        stream.close()
            self.process = None

    def _send(self, message: dict[str, object]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise CodexAdapterError("Codex app-server is not running")
        payload = (
            json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CodexAdapterError(self._failure_message(str(error))) from error

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send({"method": method, "params": params})

    def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        self.request_id += 1
        request_id = self.request_id
        self._send({"method": method, "id": request_id, "params": params})
        response = self._read_response(request_id, timeout=timeout)
        if "error" in response:
            raise CodexAdapterError(
                f"Codex app-server rejected {method}: {response['error']!r}"
            )
        if "result" not in response:
            raise CodexAdapterError(f"Codex app-server returned no result for {method}")
        return response["result"]

    def _read_response(
        self,
        request_id: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        process = self.process
        selector = self.selector
        if process is None or selector is None:
            raise CodexAdapterError("Codex app-server is not running")
        response_timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, response_timeout)
        while True:
            response = self._extract_response(request_id)
            if response is not None:
                return response
            if process.poll() is not None:
                raise CodexAdapterError(self._failure_message("exited early"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAdapterError(
                    self._failure_message(
                        f"timed out after {response_timeout:g} seconds"
                    )
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                try:
                    file_object = key.fileobj
                    descriptor = (
                        file_object
                        if isinstance(file_object, int)
                        else file_object.fileno()
                    )
                    chunk = os.read(descriptor, 65536)
                except OSError as error:
                    raise CodexAdapterError(
                        self._failure_message(str(error))
                    ) from error
                if not chunk:
                    with suppress(KeyError, ValueError):
                        selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(self.stdout_buffer) + len(chunk) > _MAX_STDOUT_BUFFER:
                        raise CodexAdapterError(
                            "Codex app-server output exceeded the safety limit"
                        )
                    self.stdout_buffer.extend(chunk)
                else:
                    remaining_capacity = max(0, 16384 - len(self.stderr_buffer))
                    self.stderr_buffer.extend(chunk[:remaining_capacity])

    def _extract_response(self, request_id: int) -> dict[str, Any] | None:
        while b"\n" in self.stdout_buffer:
            raw_line, _, remainder = self.stdout_buffer.partition(b"\n")
            self.stdout_buffer = bytearray(remainder)
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CodexAdapterError(
                    f"Codex app-server emitted invalid JSON: {error}"
                ) from error
            if not isinstance(value, dict):
                continue
            if value.get("id") == request_id:
                return value
        return None

    def _failure_message(self, detail: str) -> str:
        safe_detail = truncate_display(sanitize_terminal_text(detail), 1000)
        stderr = truncate_display(
            sanitize_terminal_text(self.stderr_buffer.decode(errors="replace")),
            1000,
        )
        suffix = f"; stderr: {stderr}" if stderr else ""
        return f"Codex app-server {safe_detail}{suffix}"


def list_threads(
    codex_path: str,
    *,
    timeout: float = 5.0,
    total_timeout: float = 15.0,
    scope: str = "platform",
    environ: dict[str, str] | None = None,
) -> list[ThreadRecord]:
    if scope not in {"platform", "all"}:
        raise ValueError("scope must be 'platform' or 'all'")
    if timeout <= 0 or total_timeout <= 0:
        raise ValueError("timeouts must be positive")
    records: list[ThreadRecord] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    wire_count = 0
    with AppServerClient(codex_path, timeout=timeout, environ=environ) as client:
        deadline = time.monotonic() + total_timeout
        for _ in range(100):
            params: dict[str, object] = {
                "archived": False,
                "sourceKinds": ["cli"],
                "limit": 100,
                "sortKey": "recency_at",
                "sortDirection": "desc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAdapterError(
                    f"Codex app-server history lookup timed out after "
                    f"{total_timeout:g} seconds"
                )
            result = client.request(
                "thread/list",
                params,
                timeout=min(timeout, remaining),
            )
            if not isinstance(result, dict):
                raise CodexAdapterError(
                    "Codex app-server returned an invalid thread/list result"
                )
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexAdapterError(
                    "Codex app-server returned an invalid thread list"
                )
            wire_count += len(data)
            if wire_count > 10_000:
                raise CodexAdapterError(
                    "Codex app-server returned too many thread records"
                )
            parsed_on_page = 0
            for value in data:
                record = record_from_wire(value)
                if record is None:
                    continue
                parsed_on_page += 1
                if record.id in seen_ids:
                    continue
                if scope != "all" and not belongs_to_current_platform(
                    record.cwd,
                    platform=client.platform_os,
                ):
                    continue
                seen_ids.add(record.id)
                records.append(record)
            if data and parsed_on_page == 0:
                raise CodexAdapterError(
                    "Codex app-server returned incompatible thread records"
                )
            if "nextCursor" not in result:
                raise CodexAdapterError(
                    "Codex app-server returned no pagination cursor"
                )
            next_cursor = result["nextCursor"]
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CodexAdapterError(
                    "Codex app-server returned an invalid pagination cursor"
                )
            if next_cursor in seen_cursors:
                raise CodexAdapterError("Codex app-server repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise CodexAdapterError("Codex app-server returned too many pages")
    records.sort(
        key=lambda record: (
            record.recency_at_ms or record.updated_at_ms or record.created_at_ms
        ),
        reverse=True,
    )
    return records
