import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner.context import RunnerContext  # noqa: E402
from codex_runner.models import SessionLaunch  # noqa: E402
from codex_runner.tmux_backend import TmuxBackend  # noqa: E402

TMUX = shutil.which("tmux")

FAKE_CODEX = r"""\
import json
import os
import sys
import time
from pathlib import Path

from codex_runner import hooks


(
    ready_path,
    release_path,
    completed_path,
    input_path,
) = sys.argv[1:]

Path(ready_path).write_text("ready", encoding="utf-8")
while not Path(release_path).exists():
    time.sleep(0.02)

hooks.notify_main(
    json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": "019f0000-e2e-thread",
            "last-assistant-message": "finished",
        }
    ),
    os.environ,
)
Path(completed_path).write_text("completed", encoding="utf-8")
line = sys.stdin.readline()
Path(input_path).write_text(line, encoding="utf-8")
"""


@unittest.skipUnless(TMUX, "tmux is required")
class DisconnectLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.label = f"codexstay-e2e-{uuid.uuid4().hex[:12]}"
        self.environment = os.environ.copy()
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_TMPDIR", None)
        self.environment["TERM"] = "xterm-256color"
        existing_pythonpath = self.environment.get("PYTHONPATH", "")
        self.environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(PROJECT_ROOT / "src"), existing_pythonpath) if value
        )
        self.backend = TmuxBackend(
            str(TMUX),
            label=self.label,
            environ=self.environment,
        )
        self.socket_file = Path("/tmp") / f"tmux-{os.getuid()}" / self.label
        self.client: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None

    def tearDown(self) -> None:
        if self.client is not None and self.client.poll() is None:
            try:
                os.killpg(self.client.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.client.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.client.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.client.wait(timeout=1)
        if self.master_fd is not None:
            os.close(self.master_fd)
        try:
            self.backend.run("kill-server")
        finally:
            self.socket_file.unlink(missing_ok=True)
        self.assertFalse(self.socket_file.exists())

    def wait_until(self, predicate, timeout: float = 6.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true before timeout")

    def test_detached_turn_finishes_then_exits_gracefully(self) -> None:
        ready = self.root / "ready"
        release = self.root / "release"
        completed = self.root / "completed"
        input_path = self.root / "input"

        created = self.backend.create_session(
            cwd=str(self.root),
            launch=lambda session, token: SessionLaunch(
                command=[
                    sys.executable,
                    "-c",
                    FAKE_CODEX,
                    str(ready),
                    str(release),
                    str(completed),
                    str(input_path),
                ],
                environment={
                    **RunnerContext(
                        session=session,
                        token=token,
                        tmux_path=str(TMUX),
                        label=self.label,
                    ).to_environment(self.environment),
                    "CODEX_RUNNER_CLOSE_DELAY_SECONDS": "0",
                },
            ),
        )
        self.wait_until(ready.exists)

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self.client = subprocess.Popen(
            self.backend.command(
                "attach-session",
                "-t",
                f"={created.name}",
            ),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=self.environment,
            start_new_session=True,
        )
        os.close(slave_fd)
        self.wait_until(
            lambda: any(
                session.name == created.name and session.attached == 1
                for session in self.backend.list_sessions()
            )
        )

        os.killpg(self.client.pid, signal.SIGTERM)
        self.client.wait(timeout=2)
        self.wait_until(
            lambda: (
                self.backend.has_session(created.name)
                and self.backend.list_sessions()[0].attached == 0
            )
        )

        release.touch()
        self.wait_until(completed.exists)
        self.wait_until(lambda: not self.backend.has_session(created.name))

        self.assertEqual(input_path.read_text(encoding="utf-8"), "/exit\n")


if __name__ == "__main__":
    unittest.main()
