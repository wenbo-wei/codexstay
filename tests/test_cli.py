import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner import cli  # noqa: E402
from codex_runner.codex_adapter import CodexAdapterError  # noqa: E402
from codex_runner.context import RunnerContext  # noqa: E402
from codex_runner.models import CreatedSession, SessionLaunch  # noqa: E402
from codex_runner.tmux_backend import SessionAlreadyRunning  # noqa: E402


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeBackend:
    tmux_path = str(Path(sys.executable).resolve())

    def __init__(self) -> None:
        self.launch: SessionLaunch | None = None
        self.attached: tuple[str, str | None] | None = None

    def create_session(self, **kwargs: object) -> CreatedSession:
        launch = kwargs["launch"]
        assert callable(launch)
        self.launch = launch(
            "job-123456789abc",
            "0123456789abcdef0123456789abcdef",
        )
        return CreatedSession(
            name="job-123456789abc",
            token="0123456789abcdef0123456789abcdef",
        )

    def attach(
        self,
        session: str,
        *,
        created_token: str | None = None,
    ) -> None:
        self.attached = (session, created_token)


class InteractiveGuardTests(unittest.TestCase):
    def test_non_tty_fails_before_tmux_or_codex_discovery(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(cli.sys, "argv", ["codexstay"]),
            mock.patch.object(cli.sys, "stdin", io.StringIO()),
            mock.patch.object(cli.sys, "stdout", TtyBuffer()),
            mock.patch.object(cli.sys, "stderr", stderr),
            mock.patch.object(cli, "executable") as executable,
            mock.patch.object(cli, "TmuxBackend") as backend,
        ):
            with self.assertRaisesRegex(SystemExit, "1"):
                cli.main()

        executable.assert_not_called()
        backend.assert_not_called()
        self.assertIn("interactive terminal is required", stderr.getvalue())

    def test_dumb_term_fails_before_session_creation(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(cli.sys, "argv", ["codexstay"]),
            mock.patch.object(cli.sys, "stdin", TtyBuffer()),
            mock.patch.object(cli.sys, "stdout", TtyBuffer()),
            mock.patch.object(cli.sys, "stderr", stderr),
            mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
            mock.patch.object(cli, "executable") as executable,
        ):
            with self.assertRaisesRegex(SystemExit, "1"):
                cli.main()

        executable.assert_not_called()
        self.assertIn("TERM must describe", stderr.getvalue())


class CommandConstructionTests(unittest.TestCase):
    def test_notify_wrapper_is_a_highest_precedence_codex_override(self) -> None:
        backend = FakeBackend()
        runner_path = "/opt/bin with spaces/codexstay"
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                cli,
                "configured_notify",
                return_value=["/opt/journal", "notify"],
            ),
        ):
            cli.start_protected(
                backend,
                codex_path="/opt/codex",
                cwd=directory,
                command_path=runner_path,
            )

        assert backend.launch is not None
        expected_notify = json.dumps(
            [str(Path(runner_path).resolve()), "_notify"],
            separators=(",", ":"),
        )
        self.assertEqual(
            backend.launch.command,
            [
                "/opt/codex",
                "--profile",
                "codexstay",
                "--config",
                f"notify={expected_notify}",
            ],
        )
        context = RunnerContext.from_environment(backend.launch.environment)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context.downstream_notify,
            ("/opt/journal", "notify"),
        )
        self.assertEqual(
            backend.attached,
            (
                "job-123456789abc",
                "0123456789abcdef0123456789abcdef",
            ),
        )

    def test_wrapper_is_never_configured_as_its_own_downstream(self) -> None:
        backend = FakeBackend()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                cli,
                "configured_notify",
                return_value=["/opt/codexstay", "_notify"],
            ),
        ):
            cli.start_protected(
                backend,
                codex_path="/opt/codex",
                cwd=directory,
                command_path="/opt/codexstay",
            )

        assert backend.launch is not None
        context = RunnerContext.from_environment(backend.launch.environment)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIsNone(context.downstream_notify)

    def test_app_server_failure_uses_native_picker_inside_protected_tmux(
        self,
    ) -> None:
        backend = FakeBackend()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                cli,
                "list_threads",
                side_effect=CodexAdapterError("protocol changed"),
            ),
            mock.patch.object(cli, "start_protected") as start,
            mock.patch.object(cli.sys, "stderr", stderr),
        ):
            cli.command_resume(backend, "/opt/codex")

        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs["native_resume_picker"])
        self.assertIn("using the native Codex picker", stderr.getvalue())

    def test_concurrent_resume_attaches_the_atomic_winner(self) -> None:
        backend = FakeBackend()
        backend.create_session = mock.Mock(
            side_effect=SessionAlreadyRunning("job-123456789abc")
        )
        with tempfile.TemporaryDirectory() as directory:
            cli.start_protected(
                backend,
                codex_path="/opt/codex",
                cwd=directory,
                resume_thread_id="019f0000-concurrent-resume",
                command_path="/opt/codexstay",
            )

        self.assertEqual(
            backend.attached,
            ("job-123456789abc", None),
        )


if __name__ == "__main__":
    unittest.main()
