import re
import subprocess
from pathlib import Path

import pytest

from tools import help as command_help
from tools import ui

ROOT = Path(__file__).resolve().parents[1]

MAKEFILE = """\
##@ Running

#> usage: make start STRATEGY=<category>/<name> [MODE=sandbox|testnet]
#> var: STRATEGY | required | the strategy directory under strategies/
#> var: MODE | sandbox | sandbox or testnet
#> example: make start STRATEGY=trend/supertrend MODE=testnet
#> then: make status STRATEGY=trend/supertrend|what it holds
#> note: testnet needs make setup-key first
start: $(TOOLCHAIN_BANNER)  ## Start a strategy
\t@echo start

# An ordinary comment breaks a block off from the target below it.
#> usage: make orphan
# in between
orphan:  ## Has no block of its own
\t@echo orphan
"""


def test_a_block_right_above_a_target_is_its_help() -> None:
    commands = command_help.parse([MAKEFILE])

    start = commands["start"]
    assert start.summary == "Start a strategy"
    assert start.usage == ["make start STRATEGY=<category>/<name> [MODE=sandbox|testnet]"]
    assert start.variables == [
        ("STRATEGY", "required", "the strategy directory under strategies/"),
        ("MODE", "sandbox", "sandbox or testnet"),
    ]
    assert start.examples == ["make start STRATEGY=trend/supertrend MODE=testnet"]
    assert start.then == [("make status STRATEGY=trend/supertrend", "what it holds")]
    assert start.notes == ["testnet needs make setup-key first"]


def test_a_block_not_directly_above_its_target_is_not_its_help() -> None:
    assert command_help.parse([MAKEFILE])["orphan"].usage == []


def test_one_command_is_shown_in_full(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)

    assert command_help.show("start", command_help.parse([MAKEFILE])) == 0

    out = capsys.readouterr().out
    for text in (
        "make start",
        "Start a strategy",
        "make start STRATEGY=<category>/<name> [MODE=sandbox|testnet]",
        "STRATEGY",
        "required",
        "the strategy directory under strategies/",
        "make start STRATEGY=trend/supertrend MODE=testnet",
        "make status STRATEGY=trend/supertrend",
        "testnet needs make setup-key first",
    ):
        assert text in out, text


def test_rich_output_keeps_every_part(monkeypatch) -> None:
    from rich.console import Console

    console = Console(record=True, width=120)
    monkeypatch.setattr(ui, "_console", lambda stream_is_err: console)

    command_help.show("start", command_help.parse([MAKEFILE]))

    text = console.export_text()
    assert "STRATEGY" in text and "make status STRATEGY=trend/supertrend" in text


def test_a_removed_name_points_to_its_successor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)

    assert command_help.show("run", command_help.parse([MAKEFILE])) == 1

    captured = capsys.readouterr()
    assert "make start" in captured.out + captured.err
    assert "renamed" in captured.out + captured.err


def test_a_misspelt_name_gets_the_closest_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)

    assert command_help.show("strat", command_help.parse([MAKEFILE])) == 1

    captured = capsys.readouterr()
    assert "make help CMD=start" in captured.out + captured.err


# The repository's own commands


def _repository() -> dict:
    return command_help.parse(command_help.sources())


def _listed() -> list[str]:
    """Every command make help lists, read from its output."""
    out = subprocess.run(
        ["make", "--no-print-directory", "help"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return re.findall(r"^  ([a-z][a-z0-9-]*)\s{2,}", out, flags=re.MULTILINE)


def test_every_listed_command_has_help() -> None:
    commands = _repository()
    listed = _listed()

    assert len(listed) >= 19
    missing = [name for name in listed if not commands.get(name) or not commands[name].usage]
    assert missing == []


@pytest.mark.parametrize("name", sorted(command_help.parse(
    command_help.sources())))  # fmt: skip
def test_every_variable_in_a_usage_is_explained(name) -> None:
    command = _repository()[name]
    used = {
        variable for line in command.usage for variable in re.findall(r"\b([A-Z][A-Z_]+)=", line)
    }
    explained = {variable for variable, _, _ in command.variables}

    assert used <= explained, sorted(used - explained)


def test_the_renamed_commands_match_the_upgrade_guide() -> None:
    guide = (ROOT / "docs" / "upgrading.md").read_text()
    table = {}
    for row in re.findall(r"^\| (`make [^|]+) \| (`make [^|]+)\|", guide, flags=re.MULTILINE):
        successor = re.findall(r"`make ([a-z-]+)`", row[1])[0]
        for old in re.findall(r"`make ([a-z-]+)`", row[0]):
            table[old] = successor

    assert table == command_help.RENAMED


def test_a_fork_command_in_local_mk_is_explained(tmp_path: Path, monkeypatch) -> None:
    """A fork's targets live in local.mk; `make help CMD=<name>` must explain them too."""
    (tmp_path / "tools" / "runner").mkdir(parents=True)
    (tmp_path / "tools" / "runner" / "runner.mk").write_text("", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        (ROOT / "Makefile").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "local.mk").write_text(
        "##@ Mine\n"
        "#> usage: make my-gate\n"
        "#> note: a check only this fork runs\n"
        "my-gate:  ## A check only this fork runs\n"
        "\t@true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(command_help, "ROOT", tmp_path)

    commands = command_help.parse(command_help.sources())

    assert commands["my-gate"].usage == ["make my-gate"]
    assert commands["setup"].usage, "the template's own commands are still there"


def test_every_command_is_listed_with_its_summary_apart() -> None:
    """A name as long as the listing's column would run into its summary and drop out."""

    # help itself is the one command above every heading; its summary is the listing.
    assert set(_listed()) == set(_repository()) - {"help"}


ARX_COMMANDS = (
    "arx-login",
    "arx-status",
    "arx-logout",
    "arx-evidence",
    "enroll-runner",
    "authorize-runner-transport",
    "runner-safety-policy",
    "deploy-preview",
    "deploy",
    "deploy-stop",
)


def _section(name: str) -> list[str]:
    """The commands make help lists under one heading, in order."""
    out = subprocess.run(
        ["make", "--no-print-directory", "help"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    body = out.split(f"\n{name}\n", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^  ([a-z][a-z0-9-]*)\s{2,}", body, flags=re.MULTILINE)


def test_the_arx_commands_are_listed_in_the_order_a_deployment_takes() -> None:
    assert _section("Deploying through ARX") == list(ARX_COMMANDS)


@pytest.mark.parametrize("name", ARX_COMMANDS)
def test_every_arx_command_is_explained_in_full(name) -> None:
    command = _repository()[name]

    assert command.usage and command.notes or name in {"arx-status", "arx-logout"}
    assert command.then, "every ARX command says what to run next"
    used = {v for line in command.usage for v in re.findall(r"\b([A-Z][A-Z_]+)=", line)}
    assert {variable for variable, _, _ in command.variables} == used
    for example in command.examples:
        assert example.startswith(f"make {name} ")
        given = set(re.findall(r"\b([A-Z][A-Z_]+)=", example))
        assert given <= used, sorted(given - used)


@pytest.mark.parametrize("name", ARX_COMMANDS)
def test_every_arx_command_is_in_the_readme_and_the_deployment_guide(name) -> None:
    for guide in ("README.md", "docs/deploying.md"):
        assert f"make {name}" in (ROOT / guide).read_text(encoding="utf-8"), guide
