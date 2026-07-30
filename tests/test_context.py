import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner.context import (  # noqa: E402
    DOWNSTREAM_ENV,
    SESSION_ENV,
    TMUX_ENV,
    TMUX_LABEL_ENV,
    TOKEN_ENV,
    RunnerContext,
)


class RunnerContextTests(unittest.TestCase):
    def context(self) -> RunnerContext:
        return RunnerContext(
            session="job-123456789abc",
            token="0123456789abcdef0123456789abcdef",
            tmux_path=str(Path(sys.executable).resolve()),
            label="codexstay-test",
            downstream_notify=("/opt/journal", "notify"),
        )

    def test_round_trip_replaces_stale_runner_and_tmux_transport_state(self) -> None:
        context = self.context()
        environment = context.to_environment(
            {
                "CODEX_RUNNER_STALE": "old",
                "TMUX": "/tmp/outer,1,0",
                "TMUX_PANE": "%9",
                "TMUX_TMPDIR": "/tmp/outer",
                "TERM": "xterm-256color",
                "KEEP": "current",
            }
        )

        self.assertEqual(RunnerContext.from_environment(environment), context)
        self.assertNotIn("CODEX_RUNNER_STALE", environment)
        self.assertNotIn("TMUX", environment)
        self.assertNotIn("TMUX_PANE", environment)
        self.assertNotIn("TMUX_TMPDIR", environment)
        self.assertEqual(environment["TERM"], "xterm-256color")

        downstream = context.downstream_environment(
            {
                **environment,
                "CODEX_RUNNER_FUTURE": "private",
                "TMUX": "/tmp/current,1,0",
            }
        )
        self.assertFalse(any(name.startswith("CODEX_RUNNER_") for name in downstream))
        self.assertNotIn("TMUX", downstream)
        self.assertEqual(downstream["KEEP"], "current")

    def test_decoder_rejects_each_invalid_identity_field(self) -> None:
        valid = self.context().to_environment({})
        invalid_values = (
            (SESSION_ENV, "other-session"),
            (TOKEN_ENV, "not-hex"),
            (TMUX_ENV, "relative/tmux"),
            (TMUX_LABEL_ENV, "label with spaces"),
        )
        for name, value in invalid_values:
            with self.subTest(name=name):
                environment = {**valid, name: value}
                self.assertIsNone(RunnerContext.from_environment(environment))

    def test_downstream_argv_must_be_safe_for_exec(self) -> None:
        valid = self.context().to_environment({})
        for argument in ("bad\0argument", "bad\ud800argument"):
            with self.subTest(argument=repr(argument)):
                environment = {
                    **valid,
                    DOWNSTREAM_ENV: json.dumps([argument]),
                }
                self.assertIsNone(RunnerContext.from_environment(environment))

                context = RunnerContext(
                    session="job-123456789abc",
                    token="0123456789abcdef0123456789abcdef",
                    tmux_path=str(Path(sys.executable).resolve()),
                    label="codexstay-test",
                    downstream_notify=(argument,),
                )
                with self.assertRaisesRegex(ValueError, "invalid runner context"):
                    context.to_environment({})

    def test_pathologically_nested_downstream_json_is_rejected(self) -> None:
        environment = {
            **self.context().to_environment({}),
            DOWNSTREAM_ENV: "[" * 2000 + "]" * 2000,
        }

        self.assertIsNone(RunnerContext.from_environment(environment))


if __name__ == "__main__":
    unittest.main()
