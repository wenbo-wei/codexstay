from __future__ import annotations

import io
import json
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from codex_runner import hooks, profile  # noqa: E402
from codex_runner.context import (  # noqa: E402
    DOWNSTREAM_ENV,
    TOKEN_ENV,
    RunnerContext,
)
from codex_runner.tmux_backend import (  # noqa: E402
    OWNED_OPTION,
    PANE_OPTION,
    STATE_OPTION,
    THREAD_OPTION,
    TOKEN_OPTION,
)

FAKE_TMUX_BODY = r"""
import json
import os
import sys
from pathlib import Path


state_path = Path(os.environ["FAKE_TMUX_STATE"])
log_path = Path(os.environ["FAKE_TMUX_LOG"])
arguments = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(arguments) + "\n")

state = json.loads(state_path.read_text(encoding="utf-8"))
command = arguments[4:]


def session_name(target):
    if target.startswith("%"):
        for name, record in state["sessions"].items():
            if record.get("pane_id") == target:
                return name
    return target.removeprefix("=").split(":", 1)[0]


def session_for(target):
    return state["sessions"].get(session_name(target))


def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")


return_code = 0
if not command:
    return_code = 1
elif command[0] == "show-options":
    record = session_for(command[command.index("-t") + 1])
    option = command[-1]
    if (
        record is None
        or not record.get("exists", False)
        or option not in record["options"]
    ):
        if record is not None and option not in record["options"]:
            sys.stderr.write(f"invalid option: {option}\n")
        return_code = 1
    else:
        print(record["options"][option])
elif command[0] == "set-option":
    record = session_for(command[command.index("-t") + 1])
    if record is None or not record.get("exists", False):
        return_code = 1
    else:
        option = command[-2]
        if "-o" in command and option in record["options"]:
            return_code = 1
        else:
            record["options"][option] = command[-1]
            record["set_count"] = record.get("set_count", 0) + 1
            save()
elif command[0] == "display-message":
    name = session_name(command[command.index("-t") + 1])
    record = state["sessions"].get(name)
    if record is None or not record.get("exists", False):
        return_code = 1
    else:
        print(name)
elif command[0] == "has-session":
    record = session_for(command[command.index("-t") + 1])
    return_code = 0 if record is not None and record.get("exists", False) else 1
elif command[0] == "send-keys":
    record = session_for(command[command.index("-t") + 1])
    if record is None or not record.get("exists", False):
        return_code = 1
    elif "-l" in command and command[-1] == "/exit":
        record["exit_typed"] = True
        save()
    elif command[-1] == "Enter":
        record["enter_sent"] = True
        if (
            record.get("exit_typed", False)
            and os.environ.get("FAKE_TMUX_EXIT_ON_ENTER") == "1"
        ):
            record["exists"] = False
        save()
elif command[0] == "kill-session":
    record = session_for(command[command.index("-t") + 1])
    if record is None or not record.get("exists", False):
        return_code = 1
    else:
        record["exists"] = False
        record["killed"] = True
        save()
else:
    return_code = 1

raise SystemExit(return_code)
"""


class HookHarness(unittest.TestCase):
    SESSION = "job-123456789abc"
    TOKEN = "0123456789abcdef0123456789abcdef"
    THREAD = "019f0000-1111-7222-8333-444455556666"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.tmux_path = self.root / "fake tmux"
        self.tmux_path.write_text(
            f"#!{sys.executable}\n{FAKE_TMUX_BODY}",
            encoding="utf-8",
        )
        self.tmux_path.chmod(0o700)
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "tmux.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        self.seed_session()

    def seed_session(
        self,
        *,
        token: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        options = {
            OWNED_OPTION: "1",
            PANE_OPTION: "%1",
            TOKEN_OPTION: self.TOKEN if token is None else token,
        }
        if thread_id is not None:
            options[THREAD_OPTION] = thread_id
        self.state_path.write_text(
            json.dumps(
                {
                    "sessions": {
                        self.SESSION: {
                            "exists": True,
                            "pane_id": "%1",
                            "options": options,
                            "set_count": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.log_path.write_text("", encoding="utf-8")

    def environment(
        self,
        *,
        token: str | None = None,
        downstream: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        environment = RunnerContext(
            session=self.SESSION,
            token=self.TOKEN if token is None else token,
            tmux_path=str(self.tmux_path),
            label="codexstay-test",
            downstream_notify=downstream,
        ).to_environment(
            {
                "FAKE_TMUX_STATE": str(self.state_path),
                "FAKE_TMUX_LOG": str(self.log_path),
            }
        )
        environment["CODEX_RUNNER_CLOSE_DELAY_SECONDS"] = "0"
        return environment

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def session_state(self) -> dict[str, object]:
        return self.state()["sessions"][self.SESSION]

    def tmux_commands(self) -> list[list[str]]:
        return [
            json.loads(line)[4:]
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def session_start(self, thread_id: str | None = None) -> io.StringIO:
        return io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": self.THREAD if thread_id is None else thread_id,
                    "source": "startup",
                }
            )
        )


class SessionStartHookTests(HookHarness):
    def test_owned_session_records_authoritative_thread_id(self) -> None:
        result = hooks.main(
            [],
            environ=self.environment(),
            stdin=self.session_start(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            self.session_state()["options"][THREAD_OPTION],
            self.THREAD,
        )
        self.assertEqual(self.session_state()["set_count"], 2)
        self.assertEqual(
            self.session_state()["options"][STATE_OPTION],
            "running",
        )

    def test_repeated_session_start_does_not_rewrite_same_thread_id(self) -> None:
        self.seed_session(thread_id=self.THREAD)

        result = hooks.main(
            [],
            environ=self.environment(),
            stdin=self.session_start(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            self.session_state()["options"][THREAD_OPTION],
            self.THREAD,
        )
        self.assertEqual(self.session_state()["set_count"], 1)
        self.assertEqual(
            self.session_state()["options"][STATE_OPTION],
            "running",
        )

    def test_session_start_does_not_replace_conflicting_thread_id(self) -> None:
        existing_thread = "019f9999-1111-7222-8333-444455556666"
        self.seed_session(thread_id=existing_thread)

        result = hooks.main(
            [],
            environ=self.environment(),
            stdin=self.session_start(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            self.session_state()["options"][THREAD_OPTION],
            existing_thread,
        )
        self.assertEqual(self.session_state()["set_count"], 0)

    def test_wrong_token_cannot_record_thread_id(self) -> None:
        wrong_token = "fedcba9876543210fedcba9876543210"

        result = hooks.main(
            [],
            environ=self.environment(token=wrong_token),
            stdin=self.session_start(),
        )

        self.assertEqual(result, 0)
        self.assertNotIn(THREAD_OPTION, self.session_state()["options"])
        self.assertEqual(self.session_state()["set_count"], 0)

    def test_malformed_token_is_rejected_before_tmux_is_contacted(self) -> None:
        environment = self.environment()
        environment[TOKEN_ENV] = "not-a-32-character-hex-token"

        result = hooks.main(
            [],
            environ=environment,
            stdin=self.session_start(),
        )

        self.assertEqual(result, 0)
        self.assertNotIn(THREAD_OPTION, self.session_state()["options"])
        self.assertEqual(self.tmux_commands(), [])


class CloseWorkerTokenTests(HookHarness):
    def test_wrong_token_cannot_request_exit_or_kill_session(self) -> None:
        wrong_token = "fedcba9876543210fedcba9876543210"

        result = hooks.close_worker(self.environment(token=wrong_token))

        self.assertEqual(result, 0)
        self.assertTrue(self.session_state()["exists"])
        command_names = [command[0] for command in self.tmux_commands()]
        self.assertNotIn("send-keys", command_names)
        self.assertNotIn("kill-session", command_names)


class CloseWorkerLifecycleTests(HookHarness):
    def test_missing_pane_identity_uses_token_checked_kill(self) -> None:
        state = self.state()
        del state["sessions"][self.SESSION]["options"][PANE_OPTION]
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        result = hooks.close_worker(self.environment())

        self.assertEqual(result, 0)
        self.assertFalse(self.session_state()["exists"])
        self.assertTrue(self.session_state()["killed"])
        command_names = [command[0] for command in self.tmux_commands()]
        self.assertNotIn("send-keys", command_names)

    def test_exit_command_closes_session_without_kill_fallback(self) -> None:
        environment = self.environment()
        environment["FAKE_TMUX_EXIT_ON_ENTER"] = "1"

        result = hooks.close_worker(environment)

        self.assertEqual(result, 0)
        self.assertFalse(self.session_state()["exists"])
        commands = self.tmux_commands()
        self.assertIn(
            [
                "send-keys",
                "-t",
                "%1",
                "-l",
                "/exit",
            ],
            commands,
        )
        self.assertIn(
            ["send-keys", "-t", "%1", "Enter"],
            commands,
        )
        self.assertNotIn("kill-session", [command[0] for command in commands])

    def test_session_is_killed_only_after_exit_deadline_expires(self) -> None:
        environment = self.environment()

        with (
            mock.patch.object(hooks.time, "sleep"),
            mock.patch.object(
                hooks.time,
                "monotonic",
                side_effect=[0.0, 9.0],
            ),
        ):
            result = hooks.close_worker(environment)

        self.assertEqual(result, 0)
        self.assertFalse(self.session_state()["exists"])
        self.assertTrue(self.session_state()["killed"])
        commands = self.tmux_commands()
        typed_exit_index = commands.index(
            [
                "send-keys",
                "-t",
                "%1",
                "-l",
                "/exit",
            ]
        )
        kill_index = commands.index(["kill-session", "-t", f"={self.SESSION}"])
        self.assertLess(typed_exit_index, kill_index)


class NotificationTests(HookHarness):
    class ImmediateProcess:
        pid = 4242

        def wait(self, timeout: float) -> int:
            return 0

    def test_completion_schedules_close_before_forwarding_notification(self) -> None:
        downstream = ("/opt/codex-journal", "notify")
        environment = self.environment(downstream=downstream)
        environment.update(
            {
                "TMUX": "/tmp/tmux-1000/default,1,0",
                "TMUX_PANE": "%1",
                "TMUX_TMPDIR": "/tmp/example",
            }
        )
        raw_payload = json.dumps(
            {
                "type": "agent-turn-complete",
                "thread-id": self.THREAD,
                "last-assistant-message": "finished",
            }
        )
        spawned: list[tuple[list[str], dict[str, object]]] = []

        def record_spawn(
            command: list[str],
            **kwargs: object,
        ) -> NotificationTests.ImmediateProcess:
            spawned.append((list(command), kwargs))
            return self.ImmediateProcess()

        with mock.patch.object(
            hooks.subprocess,
            "Popen",
            side_effect=record_spawn,
        ):
            result = hooks.notify_main(raw_payload, environment)

        self.assertEqual(result, 0)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(
            spawned[0][0],
            [
                sys.executable,
                "-m",
                "codex_runner.hooks",
                "--close",
            ],
        )
        self.assertEqual(spawned[1][0], [*downstream, raw_payload])
        self.assertTrue(spawned[0][1]["start_new_session"])
        self.assertTrue(spawned[1][1]["start_new_session"])
        downstream_environment = spawned[1][1]["env"]
        self.assertFalse(
            any(name.startswith("CODEX_RUNNER_") for name in downstream_environment)
        )
        for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
            self.assertNotIn(name, downstream_environment)
        close_environment = spawned[0][1]["env"]
        self.assertIn(TOKEN_ENV, close_environment)

    def test_noncompletion_forwards_without_scheduling_close(self) -> None:
        downstream = ("/opt/codex-journal", "notify")
        environment = self.environment(downstream=downstream)
        raw_payload = json.dumps({"type": "approval-requested"})
        spawned: list[list[str]] = []

        def record_spawn(
            command: list[str],
            **kwargs: object,
        ) -> NotificationTests.ImmediateProcess:
            spawned.append(list(command))
            return self.ImmediateProcess()

        with mock.patch.object(
            hooks.subprocess,
            "Popen",
            side_effect=record_spawn,
        ):
            result = hooks.notify_main(raw_payload, environment)

        self.assertEqual(result, 0)
        self.assertEqual(spawned, [[*downstream, raw_payload]])

    def test_hung_downstream_is_terminated_then_killed(self) -> None:
        environment = self.environment(downstream=("/opt/codex-journal", "notify"))
        process = mock.Mock(pid=4343)
        process.wait.side_effect = [
            subprocess.TimeoutExpired("notify", 3),
            subprocess.TimeoutExpired("notify", 1),
            subprocess.TimeoutExpired("notify", 1),
        ]

        with (
            mock.patch.object(
                hooks.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(hooks.os, "killpg") as killpg,
        ):
            hooks.notify_main(
                json.dumps({"type": "approval-requested"}),
                environment,
            )

        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=3), mock.call(timeout=1), mock.call(timeout=1)],
        )
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )

    def test_invalid_context_schedules_nothing_and_forwards_nothing(self) -> None:
        environment = self.environment(downstream=("/opt/codex-journal", "notify"))
        environment[TOKEN_ENV] = "malformed"

        with mock.patch.object(hooks.subprocess, "Popen") as popen:
            result = hooks.notify_main(
                json.dumps({"type": "agent-turn-complete"}),
                environment,
            )

        self.assertEqual(result, 0)
        popen.assert_not_called()

    def test_malformed_downstream_invalidates_the_context(self) -> None:
        environment = self.environment()
        environment[DOWNSTREAM_ENV] = '["/opt/journal", 42]'

        with mock.patch.object(hooks.subprocess, "Popen") as popen:
            result = hooks.notify_main(
                json.dumps({"type": "approval-requested"}),
                environment,
            )

        self.assertEqual(result, 0)
        popen.assert_not_called()


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.command_path = self.root / "bin with spaces" / "codexstay"

    def test_rendered_profile_preserves_hook_and_notify_arguments(self) -> None:
        rendered = profile.render_profile(str(self.command_path))
        parsed = tomllib.loads(rendered)
        resolved = str(self.command_path.resolve())

        self.assertTrue(rendered.startswith(profile.MANAGED_MARKER))
        self.assertEqual(parsed["notify"], [resolved, "_notify"])
        session_start = parsed["hooks"]["SessionStart"]
        self.assertEqual(len(session_start), 1)
        self.assertEqual(session_start[0]["matcher"], "startup|resume")
        self.assertEqual(
            session_start[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": shlex.join([resolved, "_hook"]),
                    "timeout": 5,
                }
            ],
        )
        self.assertNotIn("state", parsed["hooks"])

    def test_profile_install_is_private_and_leaves_no_temporary_file(self) -> None:
        codex_home = self.root / "codex-home"

        installed = profile.write_profile(
            str(self.command_path),
            profile_name="codexstay",
            environ={"CODEX_HOME": str(codex_home)},
        )

        self.assertEqual(installed, codex_home / "codexstay.config.toml")
        self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
        self.assertEqual(codex_home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            installed.read_text(encoding="utf-8"),
            profile.render_profile(str(self.command_path)),
        )
        self.assertEqual(
            list(codex_home.glob(f".{installed.name}.*.tmp")),
            [],
        )

    def test_profile_install_does_not_overwrite_unmanaged_config(self) -> None:
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        destination = codex_home / "codexstay.config.toml"
        original = 'model = "user-choice"\n'
        destination.write_text(original, encoding="utf-8")

        with self.assertRaises(FileExistsError):
            profile.write_profile(
                str(self.command_path),
                profile_name="codexstay",
                environ={"CODEX_HOME": str(codex_home)},
            )

        self.assertEqual(destination.read_text(encoding="utf-8"), original)

    def test_profile_install_preserves_existing_trust_state(self) -> None:
        codex_home = self.root / "codex-home"
        environment = {"CODEX_HOME": str(codex_home)}
        destination = profile.write_profile(
            str(self.command_path),
            profile_name="codexstay",
            environ=environment,
        )
        trusted = (
            destination.read_text(encoding="utf-8")
            + '\n[hooks.state]\ntrusted_hash = "sha256:example"\n'
        )
        destination.write_text(trusted, encoding="utf-8")

        repeated = profile.write_profile(
            str(self.command_path),
            profile_name="codexstay",
            environ=environment,
        )

        self.assertEqual(repeated, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), trusted)

    def test_changed_managed_profile_requires_force(self) -> None:
        codex_home = self.root / "codex-home"
        environment = {"CODEX_HOME": str(codex_home)}
        destination = profile.write_profile(
            "/opt/old-codexstay",
            profile_name="codexstay",
            environ=environment,
        )

        with self.assertRaisesRegex(FileExistsError, "setup --force"):
            profile.write_profile(
                "/opt/new-codexstay",
                profile_name="codexstay",
                environ=environment,
            )

        self.assertIn(
            "/opt/old-codexstay",
            destination.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
