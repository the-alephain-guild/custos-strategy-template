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
    python3 tools/ui.py next "make status STRATEGY=trend/x|see what it holds" "make stop"
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

try:
    from rich import box
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


# What a value means, for the colour it is shown in; plain text shows the value alone.
TONES = {"up": "green", "down": "red", "muted": "dim", "strong": "bold", "warn": "yellow"}


@dataclass(frozen=True)
class Cell:
    """A value with a tone from TONES, such as a gain shown in green."""

    text: str
    tone: str = ""


def _text(value: str | Cell) -> str:
    return value.text if isinstance(value, Cell) else str(value)


def _markup(value: str | Cell) -> str:
    if isinstance(value, Cell) and value.tone in TONES:
        return f"[{TONES[value.tone]}]{escape(value.text)}[/]"
    return escape(_text(value))


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


def grid(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str | Cell]],
    empty: str = "none",
    align: Sequence[str] | None = None,
) -> None:
    """Rows under column headers; `empty` is shown in place of a table with no rows.

    `align` gives each column "left" or "right"; numbers read best right-aligned.
    """
    aligns = list(align or ["left"] * len(headers))
    console = _console(False)
    if console is None:
        print(title)
        if not rows:
            print(f"  {empty}")
            return
        lines = [list(headers), *[[_text(cell) for cell in row] for row in rows]]
        widths = [max(len(cell) for cell in column) for column in zip(*lines, strict=True)]
        for line in lines:
            cells = [
                cell.rjust(width) if side == "right" else cell.ljust(width)
                for cell, width, side in zip(line, widths, aligns, strict=True)
            ]
            print(("  " + "  ".join(cells)).rstrip())
        return
    if not rows:
        console.print(f"[bold]{escape(title)}[/]  [dim]{escape(empty)}[/]")
        return
    table_ = Table(
        title=escape(title),
        title_justify="left",
        title_style="bold",
        box=box.ROUNDED,
        header_style="bold",
        border_style="dim",
    )
    for header, side in zip(headers, aligns, strict=True):
        table_.add_column(escape(header), justify=side, overflow="fold")
    for row in rows:
        table_.add_row(*(_markup(cell) for cell in row))
    console.print(table_)


def header(title: str, facts: Sequence[str | Cell]) -> None:
    """A title with a line of facts under it, such as how long a run has been up."""
    console = _console(False)
    line = " · ".join(_text(fact) for fact in facts)
    if console is None:
        print(title)
        if line:
            print(f"  {line}")
        return
    body = " [dim]·[/] ".join(_markup(fact) for fact in facts)
    console.print(
        Panel(body, title=f"[bold]{escape(title)}[/]", title_align="left", border_style="cyan")
    )


def space() -> None:
    """A blank line between sections."""
    console = _console(False)
    if console is None:
        print()
    else:
        console.print()


def section(title: str, lines: Sequence[str | Cell]) -> None:
    """A heading with lines under it, such as a command's usage."""
    console = _console(False)
    if console is None:
        print(title)
        for line in lines:
            print(f"  {_text(line)}")
        return
    console.print(f"[bold]{escape(title)}[/]")
    for line in lines:
        console.print(f"  {_markup(line)}")


def stats(items: Sequence[tuple[str, str | Cell]], title: str = "") -> None:
    """A few headline figures side by side, each under its label."""
    console = _console(False)
    if title:
        if console is None:
            print(title)
        else:
            console.print(f"[bold]{escape(title)}[/]")
    if console is None:
        width = max((len(label) for label, _ in items), default=0)
        for label, value in items:
            print(f"  {label.ljust(width)}  {_text(value)}")
        return
    row = Table.grid(padding=(0, 4))
    for _ in items:
        row.add_column()
    row.add_row(*(f"[dim]{escape(label)}[/]\n[bold]{_markup(value)}[/]" for label, value in items))
    console.print(row)


def next_steps(steps: Sequence[tuple[str, str]]) -> None:
    """What to run next, each command with what it does."""
    console = _console(False)
    width = max((len(command) for command, _ in steps), default=0)
    if console is None:
        print("Next:")
        for command, meaning in steps:
            print(f"  {command.ljust(width)}   {meaning}".rstrip())
        return
    # A command and what it does share a line only while both fit; commands can be
    # long, so each takes its own line and the meaning goes under it.
    lines = []
    for command, meaning in steps:
        lines.append(f"[bold cyan]{escape(command)}[/]")
        if meaning:
            lines.append(f"  [dim]{escape(meaning)}[/]")
    console.print(
        Panel("\n".join(lines), title="[bold]Next[/]", title_align="left", border_style="dim")
    )


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
    if len(argv) >= 3 and argv[1] == "next":
        # A command run as a step of another leaves the next steps to the outer one.
        if os.environ.get("CUSTOS_NO_NEXT"):
            return 0
        # Each step is "command|what it does"; the description may be left out.
        next_steps([tuple((step.split("|", 1) + [""])[:2]) for step in argv[2:]])
        return 0
    if len(argv) < 3 or argv[1] not in KINDS:
        print(__doc__)
        return 2
    tag = argv[argv.index("--tag") + 1] if "--tag" in argv else "runner"
    say(argv[1], argv[2], tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
