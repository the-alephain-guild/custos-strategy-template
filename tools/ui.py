#!/usr/bin/env python3
"""Everything the repository's commands say to the person running them, and ask.

Output goes through rich and questions through questionary when they are
installed. Some commands run where they are not -- before any environment
exists, or with `uv run --no-project` -- so each call falls back to plain text
and `input`, and nothing here fails for want of either library.

Questions are asked interactively only when both stdin and stdout are a
terminal. Otherwise a question reads one line from stdin, so CI, pipes and tests
behave as they would without this module.

Messages go to stderr when they are warnings or errors, to stdout otherwise.

Usage from a Makefile recipe:
    python3 tools/ui.py error "no testnet key is sealed" --tag runner
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

try:
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
except ImportError:  # outside the project environment; plain text is the fallback
    Console = None

try:
    import questionary
except ImportError:  # same reason as above
    questionary = None

KINDS = {
    "info": ("cyan", "·"),
    "ok": ("green", "✓"),
    "warn": ("yellow", "!"),
    "error": ("bold red", "✗"),
}


class Cancelled(Exception):
    """The person running the command stopped at a question."""


def _console(stream_is_err: bool):
    if Console is None:
        return None
    return Console(stderr=stream_is_err, highlight=False)


def say(kind: str, message: str, tag: str = "runner") -> None:
    to_err = kind in ("warn", "error")
    console = _console(to_err)
    if console is None:
        print(f"[{tag}] {message}", file=sys.stderr if to_err else sys.stdout)
        return
    style, mark = KINDS[kind]
    console.print(f"[{style}]{mark}[/] [dim]{tag}[/] {escape(message)}")


def info(message: str, tag: str = "runner") -> None:
    say("info", message, tag)


def ok(message: str, tag: str = "runner") -> None:
    say("ok", message, tag)


def warn(message: str, tag: str = "runner") -> None:
    say("warn", message, tag)


def error(message: str, tag: str = "runner") -> None:
    say("error", message, tag)


def panel(title: str, lines: Sequence[str], kind: str = "info") -> None:
    """A block of explanation under a title."""
    console = _console(False)
    if console is None:
        print(title)
        for line in lines:
            print(f"  {line}")
        return
    style = KINDS[kind][0]
    body = "\n".join(escape(line) for line in lines)
    console.print(Panel(body, title=escape(title), title_align="left", border_style=style))


def table(title: str, rows: Sequence[tuple[str, str]]) -> None:
    """Labelled values, one per row."""
    console = _console(False)
    if console is None:
        print(title)
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            print(f"  {label.ljust(width)}  {value}")
        return
    grid = Table(title=escape(title), title_justify="left", show_header=False, box=None)
    grid.add_column(style="bold", no_wrap=True)
    # Values are often paths or commit ids: wrapped, never cut short.
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(escape(label), escape(value))
    console.print(grid)


def interactive() -> bool:
    return questionary is not None and sys.stdin.isatty() and sys.stdout.isatty()


def _answer(value):
    if value is None:  # questionary returns None on Ctrl-C
        raise Cancelled
    return value


def _line(prompt: str) -> str:
    try:
        return input(f"{prompt}: ")
    except EOFError:
        return ""


def ask(prompt: str) -> str:
    if interactive():
        return _answer(questionary.text(f"{prompt}:").ask()).strip()
    return _line(prompt).strip()


def secret(prompt: str) -> str:
    if interactive():
        return _answer(questionary.password(f"{prompt}:").ask()).strip()
    return _line(prompt).strip()


def choose(prompt: str, choices: Sequence[tuple[str, str]], default: str) -> str:
    """One of `choices`, given as (value, description); `default` when not interactive."""
    if not interactive():
        return default
    options = [questionary.Choice(title=label, value=value) for value, label in choices]
    return _answer(questionary.select(prompt, choices=options, default=default).ask())


def confirm(prompt: str, default: bool = False) -> bool:
    if not interactive():
        return default
    return _answer(questionary.confirm(prompt, default=default).ask())


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in KINDS:
        print(__doc__)
        return 2
    tag = argv[argv.index("--tag") + 1] if "--tag" in argv else "runner"
    say(argv[1], argv[2], tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
