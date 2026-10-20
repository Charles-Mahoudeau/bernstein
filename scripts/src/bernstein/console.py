"""Terminal output: colored when possible, plain text otherwise.

Every message is plain line output (no live widgets), so logs stay readable when
piped or captured in CI. Each kind of line carries a text marker (``==>``, ``$``,
``OK``, ``WARN``, ``ERROR``) so the meaning survives without colors.
"""

from __future__ import annotations

import sys
import time
from enum import StrEnum
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .errors import AbortedError

if TYPE_CHECKING:
    from collections.abc import Sequence


class ColorMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class _State:
    quiet = False
    verbose = False
    assume_yes = False
    dry_run = False


state = _State()


def _make_console(mode: ColorMode, *, stderr: bool) -> Console:
    # AUTO leaves detection to Rich, which honours NO_COLOR, FORCE_COLOR and TTYs.
    return Console(
        highlight=False,
        soft_wrap=True,
        stderr=stderr,
        color_system={ColorMode.NEVER: None, ColorMode.ALWAYS: "standard"}.get(mode, "auto"),
        no_color=mode is ColorMode.NEVER,
        force_terminal=True if mode is ColorMode.ALWAYS else None,
    )


out = _make_console(ColorMode.AUTO, stderr=False)
err = _make_console(ColorMode.AUTO, stderr=True)


def configure(
    mode: ColorMode, *, quiet: bool, verbose: bool, assume_yes: bool, dry_run: bool
) -> None:
    global out, err  # noqa: PLW0603 - the console is process-wide by design
    out = _make_console(mode, stderr=False)
    err = _make_console(mode, stderr=True)
    state.quiet = quiet
    state.verbose = verbose
    state.assume_yes = assume_yes
    state.dry_run = dry_run


def _line(*parts: tuple[str, str]) -> Text:
    text = Text()
    for content, style in parts:
        text.append(content, style=style)
    return text


def step(message: str) -> None:
    """A top-level stage of the current command."""
    out.print()
    out.print(_line(("==> ", "bold blue"), (message, "bold")))


def info(message: str) -> None:
    out.print(_line(("    ", ""), (message, "")))


def detail(message: str) -> None:
    """Secondary information, hidden with --quiet."""
    if not state.quiet:
        out.print(_line(("    ", ""), (message, "dim")))


def success(message: str, *, action: bool = False) -> None:
    """``action``: the message reports a change, which a dry run did not make."""
    suffix = (" (dry run)", "magenta") if action and state.dry_run else ("", "")
    out.print(_line(("OK  ", "bold green"), (message, "green"), suffix))


def failure(message: str) -> None:
    """A test verdict that failed (the command's error comes after, on stderr)."""
    out.print(_line(("FAIL ", "bold red"), (message, "red")))


def warn(message: str) -> None:
    out.print(_line(("WARN ", "bold yellow"), (message, "yellow")))


def error(message: str) -> None:
    err.print(_line(("ERROR ", "bold red"), (message, "red")))


def command(display: str, *, query: bool, dry_run: bool = False) -> None:
    """Echo a command line so it can be copied and run by hand."""
    if query:
        out.print(_line(("$ ", "dim"), (display, "dim")))
        return
    suffix = ("  # dry run, not executed", "bold magenta") if dry_run else ("", "")
    out.print(_line(("$ ", "bold cyan"), (display, "cyan"), suffix))


def output(line: str, *, failed: bool = False) -> None:
    """A line of output from a subprocess, indented under its command."""
    out.print(_line(("  │ ", "dim"), (line, "red" if failed else "dim")))


def progress(message: str, *, start: float | None = None) -> None:
    """A timestamped progress line for wait loops, printed on state changes."""
    stamp = time.strftime("%H:%M:%S")
    elapsed = f" +{format_duration(time.monotonic() - start)}" if start is not None else ""
    out.print(_line((f"[{stamp}{elapsed}] ", "dim"), (message, "")))


def format_duration(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        out.print(_line((f"{title}: ", "bold"), ("none", "dim")))
        return
    grid = Table(title=title, title_justify="left", title_style="bold", header_style="bold")
    for column in columns:
        grid.add_column(column, overflow="fold")
    for row in rows:
        grid.add_row(*row)
    out.print(grid)


def confirm(question: str, *, expected: str | None = None) -> None:
    """Ask before a destructive action; raise AbortedError on refusal.

    With ``expected``, the user must type that exact word (used before deletions).
    ``--yes`` skips the prompt. A non-interactive stdin without ``--yes`` refuses.
    """
    if state.assume_yes:
        detail(f"{question} -> yes (--yes)")
        return
    if not sys.stdin.isatty():
        raise AbortedError(f"{question} Refusing without a terminal; pass --yes to confirm.")
    hint = f"type '{expected}' to confirm" if expected else "y/N"
    out.print(_line(("??  ", "bold yellow"), (f"{question} ({hint}) ", "bold")), end="")
    try:
        answer = input().strip()
    except EOFError:
        answer = ""
    accepted = answer == expected if expected else answer.lower() in {"y", "yes"}
    if not accepted:
        raise AbortedError("aborted by user")
