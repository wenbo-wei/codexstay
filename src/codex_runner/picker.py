import curses
import time
from contextlib import suppress

from .models import ThreadRecord
from .text import title_for, truncate_display


def relative_age(record: ThreadRecord) -> str:
    timestamp_ms = record.recency_at_ms or record.updated_at_ms or record.created_at_ms
    seconds = max(0, int(time.time() - timestamp_ms / 1000))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def resume_picker(
    records: list[ThreadRecord],
    active_by_id: dict[str, str],
) -> ThreadRecord | None:
    ordered = list(reversed(records))
    query = ""
    visible = ordered
    selected = max(0, len(visible) - 1)

    def picker(screen: curses.window) -> ThreadRecord | None:
        nonlocal query, visible, selected
        screen.keypad(True)
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
        except curses.error:
            pass
        with suppress(curses.error):
            curses.curs_set(0)
        with suppress(AttributeError):
            curses.set_escdelay(25)

        def add_line(
            y: int,
            text: str,
            *,
            selected_line: bool = False,
        ) -> None:
            height, width = screen.getmaxyx()
            if y < 0 or y >= height or width < 2:
                return
            clipped = truncate_display(text, max(1, width - 4))
            attributes = curses.A_REVERSE if selected_line else curses.A_NORMAL
            with suppress(curses.error):
                screen.addstr(y, 1, clipped, attributes)

        def render() -> None:
            nonlocal selected
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 7 or width < 40:
                add_line(0, "Resize the terminal to at least 40 x 7")
                add_line(max(0, height - 1), "esc exit")
                screen.refresh()
                return

            selected = min(selected, max(0, len(visible) - 1))
            list_height = max(1, height - 5)
            end = min(len(visible), max(list_height, selected + 1))
            start = max(0, end - list_height)
            add_line(0, f"Search: {query}")
            for display_row, row_index in enumerate(range(start, end), 2):
                record = visible[row_index]
                marker = "❯ " if row_index == selected else "  "
                age = f"{relative_age(record):>8}"
                active = "  [running]" if record.id in active_by_id else ""
                add_line(
                    display_row,
                    f"{marker}{age}  {title_for(record)}{active}",
                    selected_line=row_index == selected,
                )
            if not visible:
                add_line(2, "  No matching sessions")
            add_line(
                height - 2,
                f"{selected + 1 if visible else 0} / {len(visible)}",
            )
            add_line(
                height - 1,
                "enter resume   esc exit   type search   backspace clear   ↑/↓ browse",
            )
            screen.refresh()

        def read_key() -> object:
            key = screen.get_wch()
            if key not in (27, "\x1b"):
                return key
            screen.timeout(35)
            try:
                try:
                    second = screen.get_wch()
                except curses.error:
                    return 27
                if second not in ("[", "O"):
                    return 27
                try:
                    third = screen.get_wch()
                except curses.error:
                    return 27
                if third == "A":
                    return curses.KEY_UP
                if third == "B":
                    return curses.KEY_DOWN
                if second == "[" and third in ("5", "6"):
                    try:
                        fourth = screen.get_wch()
                    except curses.error:
                        return 27
                    if fourth == "~":
                        return curses.KEY_PPAGE if third == "5" else curses.KEY_NPAGE
                return 27
            finally:
                screen.timeout(-1)

        while True:
            render()
            try:
                key = read_key()
            except curses.error:
                continue
            if key in ("\r", "\n", curses.KEY_ENTER):
                return visible[selected] if visible else None
            if key in (27, "\x1b", "\x03"):
                return None
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_UP:
                selected = max(0, selected - 1)
                continue
            if key == curses.KEY_DOWN:
                selected = min(max(0, len(visible) - 1), selected + 1)
                continue
            if key == curses.KEY_PPAGE:
                selected = max(0, selected - 10)
                continue
            if key == curses.KEY_NPAGE:
                selected = min(max(0, len(visible) - 1), selected + 10)
                continue
            if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                query = query[:-1]
            elif isinstance(key, str) and key.isprintable():
                query += key
            else:
                continue
            folded_query = query.casefold()
            visible = [
                record
                for record in ordered
                if folded_query in title_for(record).casefold()
            ]
            selected = max(0, len(visible) - 1)

    return curses.wrapper(picker)
