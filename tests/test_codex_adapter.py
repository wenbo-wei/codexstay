import os
import signal
import stat
import tempfile
import time
import unittest
from pathlib import Path

from codex_runner.codex_adapter import (
    CodexAdapterError,
    belongs_to_current_platform,
    list_threads,
    record_from_wire,
)
from codex_runner.models import ThreadRecord

FAKE_CODEX = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path


mode = os.environ["FAKE_CODEX_MODE"]
pid_path = Path(os.environ["FAKE_CODEX_PID"])
exit_path = Path(os.environ["FAKE_CODEX_EXIT"])
pid_path.write_text(str(os.getpid()), encoding="utf-8")


def mark_exit(reason):
    exit_path.write_text(reason, encoding="utf-8")


def terminate(_signum, _frame):
    mark_exit("terminated")
    raise SystemExit(0)


def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        mark_exit("eof")
        raise SystemExit(0)
    return json.loads(line)


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


signal.signal(signal.SIGTERM, terminate)

if sys.argv[1:] != ["app-server", "--stdio"]:
    sys.stderr.write("unexpected argv\n")
    raise SystemExit(64)

initialize = read_message()
if initialize.get("method") != "initialize":
    sys.stderr.write("initialize was not first\n")
    raise SystemExit(65)
client_info = initialize.get("params", {}).get("clientInfo", {})
if (
    client_info.get("name") != "codexstay"
    or client_info.get("title") != "Codex-Stay"
):
    sys.stderr.write("unexpected client identity\n")
    raise SystemExit(65)

if mode == "bad_json_handshake":
    sys.stdout.write("{this-is-not-json\n")
    sys.stdout.flush()
    while True:
        time.sleep(60)

emit(
    {
        "id": initialize["id"],
        "result": {
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "fake-codex/1",
        },
    }
)

initialized = read_message()
if initialized != {"method": "initialized", "params": {}}:
    sys.stderr.write("missing initialized notification\n")
    raise SystemExit(66)

request = read_message()
if request.get("method") != "thread/list":
    sys.stderr.write("expected thread/list\n")
    raise SystemExit(67)

if mode == "timeout":
    while True:
        time.sleep(60)
if mode == "stderr_controls":
    sys.stderr.write("\x1b]2;changed-title\x07\nunsafe-line\n")
    sys.stderr.flush()
    while True:
        time.sleep(60)
if mode == "oversized_output":
    sys.stdout.write("x" * (4 * 1024 * 1024 + 1))
    sys.stdout.flush()
    while True:
        time.sleep(60)

params = request.get("params", {})
if mode == "paginate":
    expected = {
        "archived": False,
        "sourceKinds": ["cli"],
        "limit": 100,
        "sortKey": "recency_at",
        "sortDirection": "desc",
    }
    if params != expected:
        emit(
            {
                "id": request["id"],
                "error": {
                    "code": -32602,
                    "message": "unexpected first-page parameters",
                },
            }
        )
    else:
        emit(
            {
                "id": request["id"],
                "result": {
                    "data": [
                        {
                            "id": "thread-older",
                            "cwd": "/home/test/older",
                            "name": None,
                            "preview": "Older preview",
                            "createdAt": 10,
                            "updatedAt": 20,
                            "recencyAt": 30,
                        }
                    ],
                    "nextCursor": "cursor-1",
                },
            }
        )
        request = read_message()
        if (
            request.get("method") != "thread/list"
            or request.get("params", {}).get("cursor") != "cursor-1"
        ):
            emit(
                {
                    "id": request.get("id"),
                    "error": {
                        "code": -32602,
                        "message": "cursor was not forwarded",
                    },
                }
            )
        else:
            emit(
                {
                    "id": request["id"],
                    "result": {
                        "data": [
                            {
                                "id": "thread-newer",
                                "cwd": "/srv/newer",
                                "name": "Newer name",
                                "preview": "Newer preview",
                                "createdAt": 11,
                                "updatedAt": 21,
                                "recencyAt": 41,
                            }
                        ],
                        "nextCursor": None,
                    },
                }
            )
elif mode == "repeat_cursor":
    emit(
        {
            "id": request["id"],
            "result": {"data": [], "nextCursor": "same-cursor"},
        }
    )
    request = read_message()
    emit(
        {
            "id": request["id"],
            "result": {"data": [], "nextCursor": "same-cursor"},
        }
    )
elif mode == "slow_pages":
    page = 0
    while True:
        time.sleep(0.03)
        emit(
            {
                "id": request["id"],
                "result": {
                    "data": [],
                    "nextCursor": f"slow-cursor-{page}",
                },
            }
        )
        page += 1
        request = read_message()
elif mode == "missing_fields":
    emit(
        {
            "id": request["id"],
            "result": {
                "data": [
                    {"cwd": "/home/test/no-id", "preview": "No id"},
                    {"id": "no-cwd", "preview": "No cwd"},
                ],
                "nextCursor": None,
            },
        }
    )
elif mode == "missing_cursor":
    emit(
        {
            "id": request["id"],
            "result": {
                "data": [
                    {
                        "id": "valid-but-partial",
                        "cwd": "/home/test",
                        "preview": "Partial",
                    }
                ]
            },
        }
    )
elif mode == "empty":
    emit(
        {
            "id": request["id"],
            "result": {"data": [], "nextCursor": None},
        }
    )
else:
    sys.stderr.write("unknown fake mode\n")
    raise SystemExit(68)

while sys.stdin.buffer.readline():
    pass
mark_exit("eof")
"""


class AppServerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex = self.root / "codex"
        self.codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(
            self.codex.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

    def environment(self, mode: str) -> tuple[dict[str, str], Path, Path]:
        pid_path = self.root / f"{mode}.pid"
        exit_path = self.root / f"{mode}.exit"
        environment = os.environ.copy()
        environment.update(
            {
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_PID": str(pid_path),
                "FAKE_CODEX_EXIT": str(exit_path),
            }
        )
        return environment, pid_path, exit_path

    def read_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                return int(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                time.sleep(0.01)
        self.fail("fake Codex did not publish its pid")

    def process_exists(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_until_reaped(self, pid: int, timeout: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.process_exists(pid):
                return True
            time.sleep(0.01)
        return not self.process_exists(pid)

    def force_reap(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_pages_until_null_cursor_and_maps_wire_records(self) -> None:
        environment, _, _ = self.environment("paginate")

        records = list_threads(
            str(self.codex),
            environ=environment,
            scope="all",
        )

        self.assertEqual(
            records,
            [
                ThreadRecord(
                    id="thread-newer",
                    cwd="/srv/newer",
                    title="Newer name",
                    preview="Newer preview",
                    created_at_ms=11_000,
                    updated_at_ms=21_000,
                    recency_at_ms=41_000,
                ),
                ThreadRecord(
                    id="thread-older",
                    cwd="/home/test/older",
                    title="",
                    preview="Older preview",
                    created_at_ms=10_000,
                    updated_at_ms=20_000,
                    recency_at_ms=30_000,
                ),
            ],
        )

    def test_repeated_cursor_is_rejected(self) -> None:
        environment, _, _ = self.environment("repeat_cursor")

        with self.assertRaisesRegex(
            CodexAdapterError,
            "repeated a pagination cursor",
        ):
            list_threads(str(self.codex), environ=environment)

    def test_missing_required_fields_do_not_look_like_empty_history(self) -> None:
        environment, _, _ = self.environment("missing_fields")

        with self.assertRaises(CodexAdapterError):
            list_threads(str(self.codex), environ=environment)

    def test_missing_cursor_cannot_look_like_complete_history(self) -> None:
        environment, _, _ = self.environment("missing_cursor")

        with self.assertRaisesRegex(CodexAdapterError, "pagination cursor"):
            list_threads(str(self.codex), environ=environment)

    def test_linux_scope_excludes_users_root_and_descendants(self) -> None:
        expected = {
            "/Users": False,
            "/Users/alice/project": False,
            "/UsersElsewhere/project": True,
            "/home/alice/project": True,
            "/srv/project": True,
        }

        self.assertEqual(
            {
                path: belongs_to_current_platform(path, platform="linux")
                for path in expected
            },
            expected,
        )

    def test_macos_scope_excludes_home_root_and_descendants(self) -> None:
        expected = {
            "/home": False,
            "/home/alice/project": False,
            "/homeElsewhere/project": True,
            "/Users/alice/project": True,
        }

        self.assertEqual(
            {
                path: belongs_to_current_platform(path, platform="macos")
                for path in expected
            },
            expected,
        )

    def test_nonfinite_timestamps_are_treated_as_unknown(self) -> None:
        for timestamp in (float("nan"), float("inf"), float("-inf"), 10**1000):
            with self.subTest(timestamp=timestamp):
                record = record_from_wire(
                    {
                        "id": "019f0000-nonfinite-timestamp",
                        "cwd": "/home/test",
                        "createdAt": timestamp,
                        "updatedAt": timestamp,
                        "recencyAt": timestamp,
                    }
                )
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.created_at_ms, 0)
                self.assertEqual(record.updated_at_ms, 0)
                self.assertEqual(record.recency_at_ms, 0)

    def test_successful_empty_history_closes_the_child(self) -> None:
        environment, pid_path, exit_path = self.environment("empty")

        records = list_threads(str(self.codex), environ=environment)
        pid = self.read_pid(pid_path)

        self.assertEqual(records, [])
        self.assertEqual(exit_path.read_text(encoding="utf-8"), "eof")
        self.assertFalse(self.process_exists(pid))

    def test_timeout_is_reported_and_the_child_is_reaped(self) -> None:
        environment, pid_path, exit_path = self.environment("timeout")

        with self.assertRaisesRegex(CodexAdapterError, "timed out"):
            list_threads(
                str(self.codex),
                environ=environment,
                timeout=0.02,
            )
        pid = self.read_pid(pid_path)

        self.assertIn(
            exit_path.read_text(encoding="utf-8"),
            {"eof", "terminated"},
        )
        self.assertFalse(self.process_exists(pid))

    def test_total_pagination_timeout_bounds_the_entire_lookup(self) -> None:
        environment, pid_path, _ = self.environment("slow_pages")
        started = time.monotonic()

        with self.assertRaisesRegex(CodexAdapterError, "timed out"):
            list_threads(
                str(self.codex),
                environ=environment,
                timeout=0.5,
                total_timeout=0.05,
            )
        elapsed = time.monotonic() - started
        pid = self.read_pid(pid_path)

        self.assertLess(elapsed, 0.4)
        self.assertFalse(self.process_exists(pid))

    def test_oversized_unframed_output_is_rejected_and_reaped(self) -> None:
        environment, pid_path, _ = self.environment("oversized_output")

        with self.assertRaisesRegex(CodexAdapterError, "safety limit"):
            list_threads(
                str(self.codex),
                environ=environment,
                timeout=1,
            )
        pid = self.read_pid(pid_path)

        self.assertFalse(self.process_exists(pid))

    def test_stderr_controls_are_sanitized_before_display(self) -> None:
        environment, _, _ = self.environment("stderr_controls")

        with self.assertRaises(CodexAdapterError) as raised:
            list_threads(
                str(self.codex),
                environ=environment,
                timeout=0.5,
            )

        message = str(raised.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\x07", message)
        self.assertNotIn("\n", message)
        self.assertIn("unsafe-line", message)

    def test_invalid_scope_is_rejected_before_starting_codex(self) -> None:
        environment, pid_path, _ = self.environment("empty")

        with self.assertRaises(ValueError):
            list_threads(
                str(self.codex),
                environ=environment,
                scope="somewhere",
            )

        self.assertFalse(pid_path.exists())

    def test_handshake_parse_failure_does_not_leak_the_child(self) -> None:
        environment, pid_path, _ = self.environment("bad_json_handshake")
        pid = None
        try:
            with self.assertRaisesRegex(CodexAdapterError, "invalid JSON"):
                list_threads(str(self.codex), environ=environment)
            pid = self.read_pid(pid_path)

            self.assertTrue(
                self.wait_until_reaped(pid),
                "fake app-server remained alive after initialize failed",
            )
        finally:
            if pid is None and pid_path.exists():
                pid = self.read_pid(pid_path)
            if pid is not None and self.process_exists(pid):
                self.force_reap(pid)


if __name__ == "__main__":
    unittest.main()
