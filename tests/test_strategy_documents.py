"""The strategy skeleton's documents come in English and Chinese, English by default."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "templates" / "strategy"
DOCUMENTS = (
    "README.md.jinja",
    "insight/notes.md.jinja",
    "design.md.jinja",
    "backtests/README.md.jinja",
    "modeling/README.md.jinja",
)
CJK = re.compile(r"[一-鿿]")
BRANCHES = re.compile(
    r"\A\{%- if language == 'zh' -%\}\n(?P<zh>.*)\n\{%- else -%\}\n(?P<en>.*)\n\{%- endif %\}\n\Z",
    re.DOTALL,
)


@pytest.mark.parametrize("document", DOCUMENTS)
def test_each_document_has_a_chinese_and_an_english_branch(document: str) -> None:
    match = BRANCHES.match((SKELETON / document).read_text(encoding="utf-8"))
    assert match, f"{document} is not one zh branch followed by one en branch"
    assert CJK.search(match["zh"]), f"{document}'s Chinese branch has no Chinese"
    assert not CJK.search(match["en"]), f"{document}'s English branch has Chinese in it"


def test_english_is_the_default_language() -> None:
    copier = (REPO / "copier.yml").read_text(encoding="utf-8")
    question = copier[copier.index("\nlanguage:") :].split("\n\n", 1)[0]

    assert "English: en" in question
    assert "简体中文: zh" in question
    assert "default: en" in question


def test_modeling_holds_prototypes_beside_the_design() -> None:
    """design.md is the rule; modeling/ keeps the prototypes and analysis behind it."""
    assert (SKELETON / "design.md.jinja").is_file()
    assert not (SKELETON / "modeling" / "model.md.jinja").exists()
    assert (SKELETON / "modeling" / "README.md.jinja").is_file()
    assert (SKELETON / "modeling" / "prototype.py.jinja").is_file()
    assert (SKELETON / "modeling" / "analysis" / ".gitkeep").is_file()
    example = REPO / "examples" / "trend" / "sma_cross" / "modeling"
    assert (example / "README.md").is_file()
    assert (example / "prototype.py").is_file()
