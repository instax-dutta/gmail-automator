"""Rendering an HTML mail body as the plain text an agent can actually read.

A message with no `text/plain` alternative is normal, not rare: Exchange, Outlook and most marketing
and notification senders emit `multipart/alternative` with only a `text/html` part. Handing an agent
`body_text: null` for those leaves it reading markup, or guessing at the content of an image.

The markup stays available in `body_html`; this produces the prose view. The goal is a readable
transcript of what a person would see, not a faithful document: layout, styling and presentational
markup carry no meaning once the tags are gone, and in a mail body they are mostly table scaffolding
around a few sentences.

No third-party converter. `html.parser` is lenient about the malformed markup mail is made of, which
is the property that matters here, and adding a dependency for this would be the larger risk.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

#: Elements that end a line. Both the opening and the closing tag emit a break, so a block reads as
#: its own paragraph whether or not the markup nests it in a table cell.
BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "legend",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)

#: A cell separates its neighbours on one line rather than breaking it, because the rows of an
#: invoice or a spec are a table and read as one line each. Exchange builds nearly every HTML-only
#: body out of tables, so this is the common case rather than an exotic one.
CELL_ELEMENTS = frozenset({"td", "th"})

#: Elements whose content is never prose. `head` carries the title and metadata Outlook generates,
#: and `script`/`style` blocks are the bulk of the bytes in a typical HTML-only mail.
SUPPRESSED_ELEMENTS = frozenset(
    {
        "head",
        "iframe",
        "link",
        "meta",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
        "title",
        "video",
        "audio",
        "source",
        "track",
        "canvas",
        "map",
        "applet",
    }
)

#: Only an absolute web link is worth inlining. `mailto:` and `tel:` are already the visible text,
#: and a `cid:` reference to an inline attachment is noise.
_INLINEABLE_SCHEMES = ("http://", "https://")

_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_EXCESS_BREAKS = re.compile(r"\n{3,}")


class _TextRenderer(HTMLParser):
    """Collects the visible text of an HTML document, one chunk at a time."""

    def __init__(self) -> None:
        # `convert_charrefs` resolves `&amp;` and `&#8212;` into the characters themselves, so no
        # entity bookkeeping is needed here; leaving it off would emit `amp` without the semicolon.
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppressed_by: str | None = None
        self._suppressed_depth = 0
        # An open anchor and the chunk index its text started at, so the URL can be inlined once
        # the visible text is known.
        self._link: tuple[str, int] | None = None

    # ------------------------------------------------------------------ output

    def text(self) -> str:
        return _normalise("".join(self._chunks))

    def _emit(self, value: str) -> None:
        if self._suppressed_by is None and value:
            self._chunks.append(value)

    def _break(self) -> None:
        self._emit("\n")

    # ------------------------------------------------------------------- tags

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppressed_by is not None:
            if tag == self._suppressed_by:
                self._suppressed_depth += 1
            return
        if tag in SUPPRESSED_ELEMENTS:
            self._suppressed_by = tag
            self._suppressed_depth = 1
            return
        if tag in BLOCK_ELEMENTS:
            self._break()
        elif tag in CELL_ELEMENTS:
            self._emit(" ")
        if tag == "a":
            self._link = (_attribute(attrs, "href"), len(self._chunks))
        elif tag == "img":
            # An HTML-only mail that is one big image still says something, in its alt text.
            alt = (_attribute(attrs, "alt") or "").strip()
            if alt:
                self._emit(alt)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>` and `<img ... />`: the start tag carries the meaning, the end tag carries none.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppressed_by is not None:
            if tag == self._suppressed_by:
                self._suppressed_depth -= 1
                if self._suppressed_depth <= 0:
                    self._suppressed_by = None
            return
        if tag == "a":
            self._close_link()
            return
        if tag in CELL_ELEMENTS:
            self._emit(" ")
        elif tag in BLOCK_ELEMENTS:
            self._break()

    # ------------------------------------------------------------------- text

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def _close_link(self) -> None:
        if self._link is None:
            return
        href, start = self._link
        self._link = None
        if not href or not href.lower().startswith(_INLINEABLE_SCHEMES):
            return
        label = "".join(self._chunks[start:]).strip()
        if not label:
            # A bare URL is its own label; there is nothing to attach it to.
            self._emit(href)
        elif href not in label:
            # "View invoice" on its own tells an agent nothing it can act on.
            self._emit(f" <{href}>")

    def close(self) -> None:
        super().close()
        # An unclosed `<a>` at the end of the document would otherwise lose its URL.
        if self._link is not None:
            self._close_link()


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name:
            return value or ""
    return ""


def _normalise(text: str) -> str:
    """Collapse the whitespace that inline styling leaves behind.

    A mail body is one long run of newlines and indentation between the table rows that hold the
    sentences. Trimming each line and capping the runs of blank lines is the difference between a
    transcript and a page of whitespace.
    """
    # `&nbsp;` is a non-breaking space, not a word separator the reader can see.
    text = text.replace("\xa0", " ")
    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    return _EXCESS_BREAKS.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str | None) -> str | None:
    """Render an HTML body as plain text, or None if it yields nothing usable.

    Never raises: a body that defeats the parser costs the caller the derived text it would
    otherwise have had, and the markup in `body_html` is still there to fall back on.
    """
    if not html:
        return None
    renderer = _TextRenderer()
    try:
        renderer.feed(html)
        renderer.close()
    except Exception:
        return None
    rendered = renderer.text()
    return rendered or None


__all__ = ["html_to_text"]
