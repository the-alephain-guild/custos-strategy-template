"""Report language is presentation metadata, independent of the trading payload."""

import re
from types import SimpleNamespace

import pytest

from tests.test_backtest_report import T, data
from tools.backtest.language import catalogue, render_template
from tools.backtest.report import ChartError, indicator_snapshot, write_report


@pytest.mark.parametrize("language,tag", [("zh", "zh-CN"), ("en", "en")])
def test_report_renders_selected_language_and_keeps_all_resources_offline(tmp_path, language, tag):
    path = write_report(tmp_path, {"strategy": "raw-strategy-id"}, data(), language=language)
    text = path.read_text()
    assert f'<html lang="{tag}">' in text
    assert f'"language": "{language}"' in text
    assert (path.parent / "assets/chart-i18n.js").is_file()
    assert (path.parent / "assets/report-locales.json").is_file()
    assert 'data-i18n="Signal"' in text
    assert "__REPORT_I18N__" not in text


def test_languages_have_identical_payloads_and_invalid_language_writes_nothing(tmp_path):
    zh = write_report(tmp_path, {"strategy": "demo"}, data(), language="zh")
    en = write_report(tmp_path, {"strategy": "demo"}, data(), language="en")
    assert (zh.parent / "chart.json").read_bytes() == (en.parent / "chart.json").read_bytes()
    assert (zh.parent / "summary.json").read_bytes() == (en.parent / "summary.json").read_bytes()
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="language"):
        write_report(tmp_path, {}, data(), language="fr")
    assert set(tmp_path.rglob("*")) == before


def test_catalogue_has_both_languages_and_matching_interpolation_parameters():
    for key, values in catalogue().items():
        assert set(values) == {"en", "zh"}, key
        assert values["en"].strip() and values["zh"].strip(), key
        assert not re.search(r"[\u4e00-\u9fff]", values["en"]), key
        assert sorted(re.findall(r"\{\w+\}", values["en"])) == sorted(
            re.findall(r"\{\w+\}", values["zh"])
        ), key


def test_generated_english_static_copy_has_no_chinese_prose(tmp_path):
    from html.parser import HTMLParser

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.script = False
            self.words = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.script = True

        def handle_endtag(self, tag):
            if tag == "script":
                self.script = False

        def handle_data(self, value):
            if not self.script:
                self.words.append(value)

    path = write_report(tmp_path, {"strategy": "demo"}, data(), language="en")
    reader = Text()
    reader.feed(path.read_text())
    assert not re.search(r"[\u4e00-\u9fff]", "".join(reader.words))


def test_static_copy_uses_the_catalogue_and_does_not_inject_translation_markup(monkeypatch):
    from tools.backtest import language

    monkeypatch.setattr(
        language, "catalogue", lambda: {"Title": {"zh": "</script><img>", "en": "Title"}}
    )
    text = render_template("<html><p>Title</p><script>__REPORT_I18N__</script></html>", "zh")
    assert "&lt;/script&gt;&lt;img&gt;" in text
    assert "</script><img>" not in text
    assert "\\u003c/script\\u003e" in text


def test_indicator_localized_labels_are_generic_and_not_strategy_specific():
    series = {}
    indicator_snapshot(
        series,
        {"custom": {"value": 1, "label_i18n": {"zh": "自定义指标", "en": "Custom indicator"}}},
        SimpleNamespace(instrument_id="BTC.TEST"),
        SimpleNamespace(ts_event=T, bar_type="daily"),
    )
    descriptor = next(iter(series.values()))
    assert descriptor["label_i18n"] == {"zh": "自定义指标", "en": "Custom indicator"}
    for invalid in [{"en": "Only English"}, {"zh": "中文", "en": ""}, {"zh": 1, "en": "Value"}]:
        with pytest.raises(ChartError, match="label_i18n"):
            indicator_snapshot(
                {},
                {"custom": {"value": 1, "label_i18n": invalid}},
                SimpleNamespace(instrument_id="BTC.TEST"),
                SimpleNamespace(ts_event=T, bar_type="daily"),
            )


def test_cli_forwards_report_language_and_refuses_invalid_language_before_running(
    tmp_path, monkeypatch
):
    import sys

    from tools.backtest import run

    strategy = tmp_path / "demo"
    strategy.mkdir()
    (strategy / "config.yaml").write_text("strategy: {name: demo}")
    calls = []
    monkeypatch.setattr(
        run, "run", lambda *args, **kwargs: calls.append(kwargs) or {"strategy": "demo"}
    )
    arguments = [
        "backtest",
        str(strategy),
        "--start",
        "2025-01-01",
        "--end",
        "2025-02-01",
        "--json",
        "--report-lang",
    ]
    monkeypatch.setattr(sys, "argv", arguments + ["en"])
    assert run.main() == 0
    assert calls[0]["report_lang"] == "en"
    monkeypatch.setattr(sys, "argv", arguments + ["fr"])
    with pytest.raises(SystemExit):
        run.main()
    assert len(calls) == 1
