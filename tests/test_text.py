import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_runner.models import ThreadRecord, valid_thread_id  # noqa: E402
from codex_runner.text import (  # noqa: E402
    display_width,
    sanitize_terminal_text,
    title_for,
    truncate_display,
)


def record(*, title: str = "", preview: str = "") -> ThreadRecord:
    return ThreadRecord(
        id="019f0000-test",
        cwd="/home/test",
        title=title,
        preview=preview,
        created_at_ms=1,
        updated_at_ms=2,
        recency_at_ms=3,
    )


class TerminalTextTests(unittest.TestCase):
    def test_terminal_controls_and_bidi_overrides_are_removed(self) -> None:
        value = "safe\x1b[31m\nnext\r\u202ereversed\tend"

        cleaned = sanitize_terminal_text(value)

        self.assertEqual(cleaned, "safe [31m next reversed end")
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\u202e", cleaned)
        self.assertNotIn("\n", cleaned)

    def test_placeholder_title_falls_back_to_sanitized_preview(self) -> None:
        self.assertEqual(
            title_for(record(title="New Thread", preview="work\nfinished")),
            "work finished",
        )
        self.assertEqual(title_for(record()), "Untitled session")

    def test_display_truncation_respects_wide_characters(self) -> None:
        self.assertEqual(display_width("A中B"), 4)
        self.assertEqual(truncate_display("A中BC", 4), "A中…")
        self.assertEqual(display_width(truncate_display("A中BC", 4)), 4)

    def test_thread_ids_reject_delimiter_and_terminal_controls(self) -> None:
        self.assertTrue(valid_thread_id("019f0000-1111-7222-8333-444455556666"))
        self.assertFalse(valid_thread_id("thread\tother"))
        self.assertFalse(valid_thread_id("thread\nother"))
        self.assertFalse(valid_thread_id("thread\u202eother"))
        self.assertFalse(valid_thread_id(""))


if __name__ == "__main__":
    unittest.main()
