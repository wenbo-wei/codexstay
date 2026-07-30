import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner.models import SessionLaunch  # noqa: E402
from codex_runner.tmux_backend import (  # noqa: E402
    OWNED_OPTION,
    PANE_OPTION,
    REQUESTED_THREAD_OPTION,
    STATE_OPTION,
    THREAD_OPTION,
    TOKEN_OPTION,
    SessionAlreadyRunning,
    TmuxBackend,
    TmuxError,
)

TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required")
class TmuxBackendIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.label = f"codexstay-test-{uuid.uuid4().hex[:12]}"
        self.tmux_runtime = tempfile.TemporaryDirectory(prefix="codexstay-tmux-")
        self.environment = os.environ.copy()
        self.environment.pop("TMUX", None)
        self.environment["TERM"] = "xterm-256color"
        self.environment["TMUX_TMPDIR"] = self.tmux_runtime.name
        self.backend = TmuxBackend(
            str(TMUX),
            label=self.label,
            environ=self.environment,
        )
        socket_root = Path(self.backend.environ.get("TMUX_TMPDIR", "/tmp"))
        self.socket_file = socket_root / f"tmux-{os.getuid()}" / self.label

    def tearDown(self) -> None:
        try:
            subprocess.run(
                [
                    str(TMUX),
                    "-L",
                    self.label,
                    "-f",
                    os.devnull,
                    "kill-server",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.backend.environ,
                check=False,
            )
        finally:
            try:
                self.socket_file.unlink()
            except FileNotFoundError:
                pass
            self.tmux_runtime.cleanup()

    def wait_until(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true before the timeout")

    def attach_from_non_tty(
        self,
        session: str,
        created_token: str | None,
    ) -> subprocess.CompletedProcess[str]:
        source = """\
import os
import sys
from codex_runner.tmux_backend import TmuxBackend, TmuxError

tmux_path, label, session, token = sys.argv[1:]
backend = TmuxBackend(tmux_path, label=label, environ=os.environ)
try:
    backend.attach(
        session,
        created_token=None if token == "-" else token,
    )
except TmuxError as error:
    print(error, file=sys.stderr)
    raise SystemExit(73)
"""
        environment = self.environment.copy()
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(PROJECT_ROOT / "src"), existing_pythonpath) if value
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                source,
                str(TMUX),
                self.label,
                session,
                created_token if created_token is not None else "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            check=False,
        )

    def test_create_publishes_identity_before_respawning_the_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            observed: dict[str, object] = {}

            def final_launch(session: str, token: str) -> SessionLaunch:
                observed["session_exists"] = self.backend.has_session(session)
                observed["owned"] = self.backend.owns_session(session, token)
                observed["requested_thread_id"] = self.backend.get_option(
                    session,
                    REQUESTED_THREAD_OPTION,
                )
                observed["marker_exists"] = marker.exists()
                pane = self.backend.run(
                    "display-message",
                    "-p",
                    "-t",
                    f"={session}:0.0",
                    "#{pane_current_command}",
                )
                observed["holder_process"] = pane.stdout.strip()
                return SessionLaunch(
                    command=[
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; import sys, time; "
                            "Path(sys.argv[1]).write_text('started'); "
                            "time.sleep(30)"
                        ),
                        str(marker),
                    ],
                    environment=self.environment.copy(),
                )

            created = self.backend.create_session(
                cwd=directory,
                launch=final_launch,
                thread_id="019f0000-test-thread",
            )

            self.wait_until(marker.exists)
            self.assertTrue(observed["session_exists"])
            self.assertTrue(observed["owned"])
            self.assertEqual(
                observed["requested_thread_id"],
                "019f0000-test-thread",
            )
            self.assertFalse(observed["marker_exists"])
            self.assertTrue(observed["holder_process"])
            self.assertRegex(created.name, r"^job-[0-9a-f]{32}$")
            self.assertRegex(created.token, r"^[0-9a-f]{32}$")
            self.assertEqual(
                self.backend.get_option(created.name, OWNED_OPTION),
                "1",
            )
            self.assertEqual(
                self.backend.get_option(created.name, TOKEN_OPTION),
                created.token,
            )
            self.assertRegex(
                self.backend.get_option(created.name, PANE_OPTION) or "",
                r"^%[0-9]+$",
            )
            self.assertEqual(
                self.backend.get_option(created.name, THREAD_OPTION),
                None,
            )
            self.assertEqual(
                self.backend.get_option(created.name, STATE_OPTION),
                "starting",
            )
            self.assertEqual(
                self.backend.get_option(created.name, "mouse"),
                "on",
            )
            self.assertEqual(
                self.backend.get_option(created.name, "status"),
                "off",
            )
            self.assertEqual(
                self.backend.get_option(
                    created.name,
                    "destroy-unattached",
                ),
                "off",
            )

            listed = self.backend.list_sessions()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].name, created.name)
            self.assertEqual(listed[0].thread_id, "019f0000-test-thread")

    def test_new_session_replaces_stale_server_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_marker = Path(directory) / "first-environment.json"
            second_marker = Path(directory) / "second-environment.json"
            probe_command = [
                sys.executable,
                "-c",
                (
                    "import json, os, sys, time; "
                    "from pathlib import Path; "
                    "Path(sys.argv[1]).write_text(json.dumps({"
                    "'probe': os.environ.get('CODEX_STAY_ENV_PROBE'), "
                    "'stale': os.environ.get('CODEX_STAY_ENV_STALE'), "
                    "'pwd': os.environ.get('PWD')}), encoding='utf-8'); "
                    "time.sleep(30)"
                ),
            ]

            first_environment = self.environment.copy()
            first_environment["CODEX_STAY_ENV_PROBE"] = "first"
            first_environment["CODEX_STAY_ENV_STALE"] = "old"
            first_backend = TmuxBackend(
                str(TMUX),
                label=self.label,
                environ=first_environment,
            )
            first_backend.create_session(
                cwd=directory,
                launch=SessionLaunch(
                    command=[*probe_command, str(first_marker)],
                    environment=first_environment,
                ),
            )
            self.wait_until(
                lambda: first_marker.exists() and first_marker.stat().st_size > 0
            )

            second_environment = self.environment.copy()
            second_environment["CODEX_STAY_ENV_PROBE"] = "second"
            second_environment.pop("CODEX_STAY_ENV_STALE", None)
            second_backend = TmuxBackend(
                str(TMUX),
                label=self.label,
                environ=second_environment,
            )
            second_backend.create_session(
                cwd=directory,
                launch=SessionLaunch(
                    command=[*probe_command, str(second_marker)],
                    environment=second_environment,
                ),
            )
            self.wait_until(
                lambda: second_marker.exists() and second_marker.stat().st_size > 0
            )

            self.assertEqual(
                json.loads(first_marker.read_text(encoding="utf-8")),
                {
                    "probe": "first",
                    "stale": "old",
                    "pwd": directory,
                },
            )
            self.assertEqual(
                json.loads(second_marker.read_text(encoding="utf-8")),
                {
                    "probe": "second",
                    "stale": None,
                    "pwd": directory,
                },
            )

    def test_wrong_token_cannot_rollback_an_owned_session(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                environment=self.environment.copy(),
            ),
        )

        wrong_token = "0" * 32
        self.assertNotEqual(wrong_token, created.token)
        self.assertFalse(self.backend.owns_session(created.name, wrong_token))
        self.assertFalse(self.backend.kill_owned_session(created.name, wrong_token))
        self.assertTrue(self.backend.has_session(created.name))

        self.assertTrue(self.backend.kill_owned_session(created.name, created.token))
        self.assertFalse(self.backend.has_session(created.name))

    def test_session_option_uses_an_exact_session_target(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                environment=self.environment.copy(),
            ),
        )

        self.backend.set_option(
            created.name,
            THREAD_OPTION,
            "019f0000-updated-thread",
        )

        self.assertEqual(
            self.backend.get_option(created.name, THREAD_OPTION),
            "019f0000-updated-thread",
        )

    def test_request_exit_types_into_the_owned_session_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "input"
            created = self.backend.create_session(
                cwd=directory,
                launch=SessionLaunch(
                    command=[
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; import sys, time; "
                            "Path(sys.argv[1]).write_text(sys.stdin.readline()); "
                            "time.sleep(30)"
                        ),
                        str(marker),
                    ],
                    environment=self.environment.copy(),
                ),
            )

            self.assertFalse(self.backend.request_exit(created.name, "0" * 32))
            self.assertFalse(marker.exists())
            self.assertTrue(self.backend.request_exit(created.name, created.token))
            self.wait_until(lambda: marker.exists() and marker.read_text() == "/exit\n")
            self.assertEqual(marker.read_text(), "/exit\n")

    def test_request_exit_follows_the_owned_pane_after_it_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "owned-input"
            decoy = Path(directory) / "decoy-input"
            created = self.backend.create_session(
                cwd=directory,
                launch=SessionLaunch(
                    command=[
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; import sys, time; "
                            "Path(sys.argv[1]).write_text(sys.stdin.readline()); "
                            "time.sleep(30)"
                        ),
                        str(marker),
                    ],
                    environment=self.environment.copy(),
                ),
            )
            pane = self.backend.get_option(created.name, PANE_OPTION)
            self.assertIsNotNone(pane)
            split = self.backend.run(
                "split-window",
                "-d",
                "-t",
                str(pane),
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys, time; "
                    "Path(sys.argv[1]).write_text(sys.stdin.readline()); "
                    "time.sleep(30)"
                ),
                str(decoy),
            )
            self.assertEqual(split.returncode, 0, split.stderr)
            moved = self.backend.run(
                "break-pane",
                "-d",
                "-s",
                str(pane),
            )
            self.assertEqual(moved.returncode, 0, moved.stderr)

            window_zero = self.backend.run(
                "display-message",
                "-p",
                "-t",
                f"={created.name}:0.0",
                "#{pane_id}",
            )
            self.assertEqual(window_zero.returncode, 0, window_zero.stderr)
            self.assertNotEqual(window_zero.stdout.strip(), pane)

            self.assertTrue(self.backend.request_exit(created.name, created.token))
            self.wait_until(lambda: marker.exists() and marker.read_text() == "/exit\n")
            self.assertEqual(marker.read_text(), "/exit\n")
            self.assertFalse(decoy.exists())

    def test_kill_is_a_fallback_when_the_pane_ignores_exit(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                environment=self.environment.copy(),
            ),
        )

        self.assertTrue(self.backend.request_exit(created.name, created.token))
        self.assertTrue(self.backend.has_session(created.name))
        self.assertTrue(self.backend.kill_owned_session(created.name, created.token))
        self.assertFalse(self.backend.has_session(created.name))

    def test_create_error_rolls_back_the_holding_session(self) -> None:
        with self.assertRaisesRegex(TmuxError, "runner command is empty"):
            self.backend.create_session(
                cwd=os.getcwd(),
                launch=SessionLaunch(
                    command=[],
                    environment=self.environment.copy(),
                ),
            )

        self.assertEqual(self.backend.list_sessions(), [])

    def test_attach_failure_rolls_back_only_a_new_session(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                environment=self.environment.copy(),
            ),
        )

        result = self.attach_from_non_tty(created.name, created.token)

        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertFalse(self.backend.has_session(created.name))

    def test_attach_failure_preserves_an_existing_session(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                environment=self.environment.copy(),
            ),
        )

        result = self.attach_from_non_tty(created.name, None)

        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertTrue(self.backend.has_session(created.name))

    def test_immediate_command_exit_is_reported_as_an_attach_error(self) -> None:
        created = self.backend.create_session(
            cwd=os.getcwd(),
            launch=SessionLaunch(
                command=[sys.executable, "-c", "raise SystemExit(7)"],
                environment=self.environment.copy(),
            ),
        )
        self.wait_until(lambda: not self.backend.has_session(created.name))

        result = self.attach_from_non_tty(created.name, created.token)

        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("exited before", result.stderr)

    def test_concurrent_resume_creates_only_one_session(self) -> None:
        thread_id = "019f0000-concurrent-resume"

        def create() -> object:
            try:
                return self.backend.create_session(
                    cwd=os.getcwd(),
                    launch=SessionLaunch(
                        command=[
                            sys.executable,
                            "-c",
                            "import time; time.sleep(30)",
                        ],
                        environment=self.environment.copy(),
                    ),
                    thread_id=thread_id,
                )
            except SessionAlreadyRunning as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: create(), range(2)))

        created = [
            outcome
            for outcome in outcomes
            if not isinstance(outcome, SessionAlreadyRunning)
        ]
        duplicates = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, SessionAlreadyRunning)
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].session, created[0].name)
        self.assertEqual(len(self.backend.list_sessions()), 1)

    def test_resuming_again_never_reuses_a_session_name(self) -> None:
        thread_id = "019f0000-repeated-resume"
        launch = SessionLaunch(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            environment=self.environment.copy(),
        )
        first = self.backend.create_session(
            cwd=os.getcwd(),
            launch=launch,
            thread_id=thread_id,
        )
        self.assertTrue(
            self.backend.kill_owned_session(first.name, first.token),
        )

        second = self.backend.create_session(
            cwd=os.getcwd(),
            launch=launch,
            thread_id=thread_id,
        )

        self.assertNotEqual(first.name, second.name)
        self.assertFalse(self.backend.request_exit(first.name, first.token))
        self.assertTrue(self.backend.has_session(second.name))


if __name__ == "__main__":
    unittest.main()
