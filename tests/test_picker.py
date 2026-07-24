import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner.models import ThreadRecord  # noqa: E402
from codex_runner.picker import resume_picker  # noqa: E402


def record(thread_id: str, title: str, recency: int) -> ThreadRecord:
    return ThreadRecord(
        id=thread_id,
        cwd="/home/test",
        title=title,
        preview="",
        created_at_ms=recency,
        updated_at_ms=recency,
        recency_at_ms=recency,
    )


class FakeScreen:
    def __init__(self, keys: list[object]) -> None:
        self.keys = list(keys)
        self.lines: list[str] = []

    def keypad(self, _enabled: bool) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return 24, 100

    def erase(self) -> None:
        self.lines = []

    def addstr(self, _y: int, _x: int, text: str, _attributes: int) -> None:
        self.lines.append(text)

    def refresh(self) -> None:
        pass

    def timeout(self, _milliseconds: int) -> None:
        pass

    def get_wch(self) -> object:
        if not self.keys:
            raise curses.error()
        return self.keys.pop(0)


class ResumePickerTests(unittest.TestCase):
    def run_picker(
        self,
        records: list[ThreadRecord],
        keys: list[object],
    ) -> tuple[ThreadRecord | None, FakeScreen]:
        screen = FakeScreen(keys)
        with (
            mock.patch(
                "codex_runner.picker.curses.wrapper",
                side_effect=lambda callback: callback(screen),
            ),
            mock.patch(
                "codex_runner.picker.curses.has_colors",
                return_value=False,
            ),
            mock.patch("codex_runner.picker.curses.curs_set"),
        ):
            selected = resume_picker(records, {})
        return selected, screen

    def test_csi_arrow_sequence_moves_selection(self) -> None:
        newest = record("newest", "Newest", 20)
        older = record("older", "Older", 10)

        selected, _ = self.run_picker(
            [newest, older],
            [27, "[", "A", "\n"],
        )

        self.assertEqual(selected, older)

    def test_escape_returns_without_selection(self) -> None:
        selected, _ = self.run_picker(
            [record("one", "One", 10)],
            [27],
        )

        self.assertIsNone(selected)

    def test_control_characters_never_reach_the_screen(self) -> None:
        _, screen = self.run_picker(
            [record("one", "bad\x1b[31m\nname\u202e", 10)],
            [27],
        )

        rendered = "\n".join(screen.lines)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("bad [31m name", rendered)


if __name__ == "__main__":
    unittest.main()
