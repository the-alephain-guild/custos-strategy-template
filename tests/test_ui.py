import builtins
import io
import sys

import pytest

from tools import ui


def test_messages_fall_back_to_plain_text_without_rich(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.ok("sealed", tag="runner")
    ui.error("no key", tag="runner")
    captured = capsys.readouterr()
    assert captured.out == "[runner] sealed\n"
    assert captured.err == "[runner] no key\n"


def test_panels_and_tables_fall_back_to_plain_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.panel("Testnet key", ["Use a key from the testnet."])
    ui.table("Backtest", [("Orders", "36"), ("Win rate", "15.4%")])
    out = capsys.readouterr().out
    assert "Testnet key\n  Use a key from the testnet." in out
    assert "  Orders    36" in out and "  Win rate  15.4%" in out


def test_rich_output_keeps_the_words(capsys) -> None:
    ui.ok("sealed the testnet key", tag="runner")
    ui.table("Backtest", [("Orders", "36")])
    out = capsys.readouterr().out
    assert "sealed the testnet key" in out and "Orders" in out and "36" in out


def test_markup_in_a_message_is_printed_as_written(capsys) -> None:
    ui.info("[bold]not bold[/bold]")
    assert "[bold]not bold[/bold]" in capsys.readouterr().out


def test_questions_without_a_terminal_read_a_line(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed key\n"))
    assert not ui.interactive()
    assert ui.ask("API key") == "typed key"
    assert ui.secret("API secret") == ""  # nothing left to read
    assert ui.choose("Which?", [("a", "A"), ("b", "B")], default="b") == "b"
    assert ui.confirm("Replace?", default=False) is False


def test_questions_without_questionary_fall_back_to_input(monkeypatch) -> None:
    monkeypatch.setattr(ui, "questionary", None)
    monkeypatch.setattr(builtins, "input", lambda prompt: "from input")
    assert ui.ask("API key") == "from input"


def test_an_interrupted_question_is_a_cancellation(monkeypatch) -> None:
    class Asked:
        def ask(self):
            return None  # what questionary returns on Ctrl-C

    class Fake:
        @staticmethod
        def password(prompt):
            return Asked()

    monkeypatch.setattr(ui, "questionary", Fake)
    monkeypatch.setattr(ui, "interactive", lambda: True)
    with pytest.raises(ui.Cancelled):
        ui.secret("API secret")


def test_grids_fall_back_to_aligned_plain_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.grid("Positions", ["Instrument", "Quantity"], [["BTCUSDT-PERP", "-0.011"], ["ETH", "2"]])
    assert capsys.readouterr().out.splitlines() == [
        "Positions",
        "  Instrument    Quantity",
        "  BTCUSDT-PERP  -0.011",
        "  ETH           2",
    ]


def test_an_empty_grid_says_so(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.grid("Open orders", ["Order"], [], empty="none")
    assert capsys.readouterr().out.splitlines() == ["Open orders", "  none"]


def test_rich_grids_keep_every_cell(capsys) -> None:
    ui.grid("Positions", ["Instrument", "Quantity"], [["BTCUSDT-PERP", "[-0.011]"]])
    out = capsys.readouterr().out
    assert "Instrument" in out and "BTCUSDT-PERP" in out and "[-0.011]" in out


def _recording(monkeypatch):
    """A rich console whose output a test can read back, wide enough not to wrap."""
    from rich.console import Console

    console = Console(record=True, width=120, force_terminal=True, color_system="standard")
    monkeypatch.setattr(ui, "_console", lambda stream_is_err: console)
    return console


def test_cells_carry_a_tone_that_rich_colours_and_plain_text_drops(monkeypatch, capsys) -> None:
    console = _recording(monkeypatch)
    ui.grid("PnL", ["Result"], [[ui.Cell("+1.00", "up")], [ui.Cell("-2.00", "down")]])
    styled = console.export_text(styles=True)
    assert "\x1b[32m+1.00" in styled and "\x1b[31m-2.00" in styled

    monkeypatch.setattr(ui, "_console", lambda stream_is_err: None)
    ui.grid("PnL", ["Result"], [[ui.Cell("+1.00", "up")]])
    assert "+1.00" in capsys.readouterr().out


def test_numbers_can_be_right_aligned_in_plain_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.grid(
        "Fills", ["Side", "Price"], [["BUY", "9.5"], ["SELL", "10,000"]], align=["left", "right"]
    )
    assert capsys.readouterr().out.splitlines()[1:] == [
        "  Side   Price",
        "  BUY      9.5",
        "  SELL  10,000",
    ]


def test_a_header_and_stats_keep_every_fact(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.header("supertrend · sandbox", ["running for 12m", ui.Cell("updated 4s ago", "muted")])
    ui.stats([("Equity", "10,000.00"), ("Change", ui.Cell("▼ -0.35", "down"))])
    out = capsys.readouterr().out
    assert "supertrend · sandbox" in out
    assert "running for 12m · updated 4s ago" in out
    assert "Equity" in out and "10,000.00" in out and "▼ -0.35" in out


def test_rich_stats_and_header_keep_every_fact(monkeypatch) -> None:
    console = _recording(monkeypatch)
    ui.header("supertrend · sandbox", ["running for 12m"])
    ui.stats([("Equity", "10,000.00"), ("Drawdown", "<0.01%")])
    text = console.export_text()
    assert "supertrend · sandbox" in text and "running for 12m" in text
    assert "10,000.00" in text and "<0.01%" in text


def test_next_steps_list_each_command_with_what_it_does(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    ui.next_steps([("make start STRATEGY=trend/x MODE=sandbox", "run it"), ("make status", "")])
    assert capsys.readouterr().out.splitlines() == [
        "Next:",
        "  make start STRATEGY=trend/x MODE=sandbox   run it",
        "  make status",
    ]


def test_next_steps_from_a_makefile(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    assert ui.main(["ui.py", "next", "make logs STRATEGY=trend/x|follow its log", "make stop"]) == 0
    out = capsys.readouterr().out
    assert "make logs STRATEGY=trend/x" in out and "follow its log" in out and "make stop" in out


def test_next_steps_are_left_to_the_outer_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    monkeypatch.setenv("CUSTOS_NO_NEXT", "1")
    assert ui.main(["ui.py", "next", "make stop"]) == 0
    assert capsys.readouterr().out == ""
