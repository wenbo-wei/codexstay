import re
import unicodedata

from .models import ThreadRecord

_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


def sanitize_terminal_text(value: object) -> str:
    text = "" if value is None else str(value)
    cleaned: list[str] = []
    for character in text:
        if character in _BIDI_CONTROLS:
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"}:
            cleaned.append(" ")
        else:
            cleaned.append(character)
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def title_for(record: ThreadRecord) -> str:
    title = sanitize_terminal_text(record.title)
    if not title or title.casefold() in {"new thread", "new chat"}:
        title = sanitize_terminal_text(record.preview)
    return title[:200] if title else "Untitled thread"


def character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    if character in {"\u200d", "\ufe0e", "\ufe0f"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(character_width(character) for character in text)


def truncate_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for character in text:
        next_width = character_width(character)
        if used + next_width > width - 1:
            break
        result.append(character)
        used += next_width
    return "".join(result) + "…"
