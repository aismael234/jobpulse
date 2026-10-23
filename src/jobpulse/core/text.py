"""Plain-text helpers."""

import html
import re
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")
_BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)
_SKIPPED_TAGS = frozenset({"noscript", "script", "style", "template"})


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str | None:
    """HTML to plain text with one line per block. ``None`` when nothing is left."""
    if not markup:
        return None
    # Greenhouse sends entity-escaped HTML ("&lt;p&gt;"); unescape it once before parsing.
    if "<" not in markup and "&lt;" in markup:
        markup = html.unescape(markup)
    collector = _TextCollector()
    collector.feed(markup)
    collector.close()
    lines = (collapse_whitespace(line) for line in "".join(collector.parts).split("\n"))
    text = "\n".join(line for line in lines if line)
    return text or None
