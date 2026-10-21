"""Render static report copy from the same catalogue used by the offline viewer."""

import html
import json
from html.parser import HTMLParser
from pathlib import Path

LANGUAGES = ("zh", "en")


def validate_language(language: str) -> None:
    if language not in LANGUAGES:
        raise ValueError("report language must be zh or en")


def catalogue() -> dict:
    return json.loads((Path(__file__).with_name("assets") / "report-locales.json").read_text())


class LocalizedHTML(HTMLParser):
    def __init__(self, language: str):
        super().__init__(convert_charrefs=True)
        self.language, self.messages = language, catalogue()
        self.output: list[str] = []
        self.tags: list[str] = []

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "html":
            fields["lang"] = "zh-CN" if self.language == "zh" else "en"
        for key in ("aria-label", "title"):
            if fields.get(key) in self.messages:
                source = fields[key]
                fields[f"data-i18n-{key}"] = source
                fields[key] = self.messages[source][self.language]
        encoded = "".join(
            f' {key}="{html.escape(value, quote=True)}"' if value is not None else f" {key}"
            for key, value in fields.items()
        )
        self.output.append(f"<{tag}{encoded}>")
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.tags.append(tag)

    def handle_endtag(self, tag):
        self.output.append(f"</{tag}>")
        if self.tags and self.tags[-1] == tag:
            self.tags.pop()

    def handle_data(self, data):
        if self.tags and self.tags[-1] in {"script", "style", "pre", "code"}:
            self.output.append(data)
            return
        key = " ".join(data.split())
        if key not in self.messages:
            self.output.append(html.escape(data))
            return
        translated = html.escape(self.messages[key][self.language])
        if self.tags and self.tags[-1] in {"title", "option"}:
            self.output.append(translated)
        else:
            self.output.append(
                (" " if data[:1].isspace() else "")
                + f'<span data-i18n="{html.escape(key, quote=True)}">{translated}</span>'
                + (" " if data[-1:].isspace() else "")
            )


def render_template(template: str, language: str) -> str:
    validate_language(language)
    renderer = LocalizedHTML(language)
    renderer.feed(template)
    config = json.dumps({"language": language, "messages": renderer.messages}, ensure_ascii=True)
    config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "".join(renderer.output).replace("__REPORT_I18N__", config)
