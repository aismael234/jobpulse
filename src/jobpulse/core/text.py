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
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)


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


class _ClassTexts(HTMLParser):
    """The text of every element whose class list includes ``class_name``, in order."""

    def __init__(self, class_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self._class_name = class_name
        self.found: list[str] = []
        self._depth = 0  # elements open inside the current match; 0 outside any
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._depth:
            classes = (dict(attrs).get("class") or "").split()
            if self._class_name in classes and tag not in _VOID_TAGS:
                self._depth, self._parts = 1, []
            return
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if tag not in _VOID_TAGS:
            self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._depth or tag in _VOID_TAGS:
            return
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        self._depth -= 1
        if not self._depth:
            self.found.append(_lines("".join(self._parts)))

    def handle_data(self, data: str) -> None:
        if self._depth and not self._skip_depth:
            self._parts.append(data)


def _lines(text: str) -> str:
    """One line per block: whitespace collapsed, empty lines dropped."""
    lines = (collapse_whitespace(line) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


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
    return _lines("".join(collector.parts)) or None


def texts_by_class(markup: str, class_name: str) -> list[str]:
    """The text of every element whose ``class`` includes ``class_name``, in page order, one
    line per block (``""`` for an empty one)."""
    collector = _ClassTexts(class_name)
    collector.feed(markup)
    collector.close()
    return collector.found
