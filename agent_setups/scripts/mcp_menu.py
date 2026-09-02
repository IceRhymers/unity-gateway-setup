"""Interactive arrow-key multi-select menu (stdlib only, POSIX ttys).

`choose` renders a checklist the user drives with the arrow keys, space to
toggle, `a` to toggle all, enter to confirm, and `q`/Esc to cancel. It needs a
real terminal and the `termios`/`tty` modules (POSIX). When either is missing
(a redirected stream, or Windows), it raises `MenuUnavailable` so the caller can
fall back to a line-based prompt.

The key handling is split so the state transitions are pure and testable:
`apply_key` takes a normalized key plus the current state and returns the next
state. The raw terminal I/O (`choose`) is a thin shell around it.
"""

from __future__ import annotations

import os
import select
import sys

#: Normalized keys the reducer understands. `_read_key` maps raw bytes to these.
UP = "up"
DOWN = "down"
TOGGLE = "toggle"
ALL = "all"
CONFIRM = "confirm"
CANCEL = "cancel"


class MenuUnavailable(Exception):
    """Raised when a raw-mode terminal menu cannot run in this environment."""


def apply_key(
    key: str | None,
    cursor: int,
    selected: set[int],
    count: int,
) -> tuple[int, set[int], bool, bool]:
    """Pure state transition for one key.

    Returns `(cursor, selected, done, cancelled)`. `selected` is a set of chosen
    indices. `done` ends the loop; `cancelled` means the user abandoned it. An
    unknown key (`None`) is a no-op. Cursor movement wraps around.
    """
    cancelled = False
    done = False
    if key == CANCEL:
        return cursor, selected, True, True
    if key == CONFIRM:
        return cursor, selected, True, False
    if count == 0:
        # Nothing to move over or toggle; only confirm/cancel (handled above) end it.
        return cursor, selected, done, cancelled
    if key == UP:
        cursor = (cursor - 1) % count
    elif key == DOWN:
        cursor = (cursor + 1) % count
    elif key == TOGGLE:
        selected = set(selected)
        selected.discard(cursor) if cursor in selected else selected.add(cursor)
    elif key == ALL:
        # Toggle-all: clear when everything is already selected, else select all.
        selected = set() if len(selected) >= count else set(range(count))
    return cursor, selected, done, cancelled


def _read_key(fd: int) -> str | None:
    """Read one normalized key from a raw fd, or None for an unmapped byte."""
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":
        # An arrow key is ESC [ A/B; a lone Esc has no follow-on. Peek without
        # blocking so a bare Esc cancels instead of hanging on os.read.
        ready, _, _ = select.select([fd], [], [], 0.02)
        if not ready:
            return CANCEL
        seq = os.read(fd, 2).decode(errors="ignore")
        return {"[A": UP, "[B": DOWN}.get(seq)
    if ch in (" ",):
        return TOGGLE
    if ch in ("\r", "\n"):
        return CONFIRM
    if ch in ("q", "Q", "\x03"):  # q or Ctrl-C
        return CANCEL
    if ch in ("a", "A"):
        return ALL
    if ch in ("k", "K"):
        return UP
    if ch in ("j", "J"):
        return DOWN
    return None


def _render(out, header: str | None, options: list[str], cursor: int,
            selected: set[int], footer: str) -> None:
    """Redraw the frame in place from the top of the (alternate) screen.

    Homing the cursor and clearing below each frame keeps the window fixed, so
    it never scrolls or flickers the way an inline reprint does.
    """
    frame: list[str] = []
    if header:
        frame.append(header)
    for i, opt in enumerate(options):
        pointer = ">" if i == cursor else " "
        box = "[x]" if i in selected else "[ ]"
        frame.append(f" {pointer} {box} {opt}")
    frame.append(footer)
    body = "\n".join("\x1b[2K" + line for line in frame)  # clear each line as drawn
    out.write("\x1b[H" + body + "\x1b[J")  # home, draw the frame, clear anything below
    out.flush()


def choose(
    options: list[str],
    preselected: list[int] | set[int],
    header: str | None = None,
) -> list[int] | None:
    """Run the interactive checklist. Returns sorted selected indices, or None if cancelled.

    Raises `MenuUnavailable` when a raw-mode terminal is not available; the caller
    should fall back to a line-based prompt then.
    """
    try:
        import termios
        import tty
    except ImportError as exc:  # not POSIX
        raise MenuUnavailable(str(exc)) from exc
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise MenuUnavailable("stdin/stdout is not a terminal")

    fd = sys.stdin.fileno()
    out = sys.stdout
    count = len(options)
    cursor = 0
    selected = set(preselected)
    footer = "  (↑/↓ move · space toggle · a all/none · enter confirm · q cancel)"

    old = termios.tcgetattr(fd)
    cancelled = False
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l")  # enter alternate screen + hide cursor
        _render(out, header, options, cursor, selected, footer)
        while True:
            try:
                key = _read_key(fd)
            except KeyboardInterrupt:
                key = CANCEL
            cursor, selected, done, cancelled = apply_key(key, cursor, selected, count)
            _render(out, header, options, cursor, selected, footer)
            if done:
                break
    finally:
        out.write("\x1b[?25h\x1b[?1049l")  # show cursor + leave alternate screen (restores terminal)
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return None if cancelled else sorted(selected)
