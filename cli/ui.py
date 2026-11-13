"""Terminal display for gaai: colour, panels, tables and a spinner, with the standard library only.

Colour is used only on a terminal, and never when NO_COLOR is set (no-color.org) or for a pipe, so
`gaai status | grep` and the tests see plain text.
"""

import itertools
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CODES = {"bold": "1", "dim": "2", "italic": "3", "red": "31", "green": "32", "yellow": "33", "blue": "34",
         "magenta": "35", "cyan": "36", "grey": "90", "teal": "38;5;37", "amber": "38;5;214"}


def color_on(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    return os.environ.get("FORCE_COLOR") is not None or (hasattr(stream, "isatty") and stream.isatty())


def style(text: str, *names: str) -> str:
    if not names or not color_on():
        return text
    return f"\x1b[{';'.join(CODES[n] for n in names)}m{text}\x1b[0m"


def visible_len(text: str) -> int:
    return len(ANSI.sub("", text))


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 30)).columns, 110))


OK, WARN, BAD, INFO = "✓", "!", "✗", "·"


def mark(state: str) -> str:
    """A coloured status mark: ok, warn, bad or info."""
    return {"ok": style(OK, "green"), "warn": style(WARN, "amber"), "bad": style(BAD, "red")}.get(state, style(INFO, "grey"))


def panel(title: str, lines: list[str], tone: str = "teal") -> str:
    """A rounded box with a title, sized to the terminal."""
    w = width() - 2
    top = style("╭─ ", tone) + style(title, "bold") + " " + style("─" * max(0, w - visible_len(title) - 4) + "╮", tone)
    body = []
    for line in lines or [""]:
        for part in _wrap(line, w - 3):
            body.append(style("│ ", tone) + part + " " * max(0, w - 3 - visible_len(part)) + style(" │", tone))
    return "\n".join([top, *body, style("╰" + "─" * (w - 1) + "╯", tone)])


def _wrap(line: str, w: int) -> list[str]:
    if visible_len(line) <= w:
        return [line]
    plain = ANSI.sub("", line)                       # a long line loses its colour rather than its words
    out, cur = [], ""
    words = []
    for word in plain.split(" "):                    # a word longer than the line is cut
        words += [word[i:i + w - 2] for i in range(0, len(word), w - 2)] or [""]
    for word in words:
        if len(cur) + len(word) + 1 > w and cur:
            out.append(cur)
            cur = "  " + word
        else:
            cur = f"{cur} {word}" if cur else word
    return [*out, cur]


def table(rows: list[list[str]], header: list[str] | None = None) -> str:
    """Columns padded to their widest cell (colour codes not counted)."""
    rows = [header, *rows] if header else rows
    if not rows:
        return ""
    cols = [max(visible_len(r[i]) for r in rows if i < len(r)) for i in range(max(len(r) for r in rows))]
    out = []
    for n, r in enumerate(rows):
        cells = [c + " " * (cols[i] - visible_len(c)) for i, c in enumerate(r)]
        line = "  ".join(cells).rstrip()
        out.append(style(line, "bold") if header and n == 0 else line)
    return "\n".join(out)


LOGO = [
    "   ██████   █████   █████  ██",
    "  ██       ██   ██ ██   ██ ██",
    "  ██   ███ ███████ ███████ ██",
    "  ██    ██ ██   ██ ██   ██ ██",
    "   ██████  ██   ██ ██   ██ ██",
]


def banner(subtitle: str) -> str:
    shades = ["teal", "teal", "cyan", "cyan", "blue"]
    art = [style(line, s, "bold") for line, s in zip(LOGO, shades)]
    return "\n".join(["", *art, "", "  " + style("governed agentic AI", "bold") + style("  ·  " + subtitle, "grey"), ""])


_quiet = threading.Event()          # set: a spinner keeps still (a question is being asked, output is shown)
_lock = threading.Lock()


@contextmanager
def paused(stream=None):
    """Hold any running spinner and clear its line, e.g. while asking the operator a question."""
    stream = stream or sys.stderr
    _quiet.set()
    with _lock:
        if color_on(stream):
            stream.write("\r\x1b[2K")
            stream.flush()
    try:
        yield
    finally:
        _quiet.clear()


@contextmanager
def spinner(text: str, stream=None):
    """A spinner while something slow runs; silent when output is not a terminal."""
    stream = stream or sys.stderr
    if not color_on(stream):
        yield
        return
    stop = threading.Event()

    def spin():
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if stop.is_set():
                break
            with _lock:
                if not _quiet.is_set():
                    stream.write(f"\r{style(frame, 'teal')} {style(text, 'grey')}")
                    stream.flush()
            time.sleep(0.08)
        with _lock:
            stream.write("\r\x1b[2K")
            stream.flush()

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
