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
