"""HTML mail body rendered as the prose an agent reads.

The conversion exists because a message with no `text/plain` alternative is ordinary rather than
rare, and `body_text` is the field the tool tells an agent to read. These pin the behaviours that
make the output trustworthy: that presentational markup never leaks through, that real content
always does, and that nothing a sender can write makes it raise.
"""

from __future__ import annotations

import pytest

from gmail_automator.html_text import html_to_text

# ------------------------------------------------------------------ the basics


def test_paragraphs_become_separate_lines() -> None:
    assert html_to_text("<p>first</p><p>second</p>") == "first\n\nsecond"


def test_a_line_break_tag_becomes_a_line_break() -> None:
    assert html_to_text("one<br>two<br/>three") == "one\ntwo\nthree"


def test_inline_markup_does_not_interrupt_a_sentence() -> None:
    assert (
        html_to_text("<p>Hi <b>Dana</b>, see <i>invoice</i> #5521</p>")
        == "Hi Dana, see invoice #5521"
    )


def test_headings_and_list_items_each_get_a_line() -> None:
    assert html_to_text("<h1>Title</h1><ul><li>one</li><li>two</li></ul>") == "Title\n\none\n\ntwo"


def test_nested_inline_tags_do_not_add_breaks() -> None:
    assert html_to_text("<p><span><b><i>deep</i></b></span></p>") == "deep"


# -------------------------------------------------------------- what must vanish


def test_a_style_block_is_not_prose() -> None:
    assert html_to_text("<style>p{color:red}</style><p>visible</p>") == "visible"


def test_a_script_block_is_not_prose() -> None:
    html = "<script>var leak = 'SCRIPT';</script><p>visible</p>"
    assert html_to_text(html) == "visible"


def test_the_head_and_its_title_are_not_prose() -> None:
    assert (
        html_to_text("<html><head><title>Subject line</title></head><body>body</body></html>")
        == "body"
    )


def test_a_conditional_comment_is_not_prose() -> None:
    """Outlook hides `<!--[if mso]>` blocks from other clients; the prose is outside them."""
    html = "<!--[if mso]><table><tr><td>MSO ONLY</td></tr></table><![endif]--><p>everyone</p>"
    assert html_to_text(html) == "everyone"


def test_an_html_comment_is_not_prose() -> None:
    assert html_to_text("<p>before<!-- hidden -->after</p>") == "beforeafter"


# ------------------------------------------------------------- what must survive


def test_character_references_become_characters() -> None:
    assert html_to_text("<p>Tom &amp; Jerry &mdash; &#8364;50 &#x2014; done</p>") == (
        "Tom & Jerry — €50 — done"
    )


def test_a_non_breaking_space_reads_as_a_space() -> None:
    assert html_to_text("<p>Invoice&nbsp;#5521</p>") == "Invoice #5521"


def test_angle_brackets_in_prose_survive() -> None:
    assert html_to_text("<p>if a &lt; b and c &gt; d</p>") == "if a < b and c > d"


def test_an_image_alt_text_is_kept() -> None:
    """An HTML-only mail that is one image still says something, in its alt attribute."""
    assert html_to_text('<img src="cid:logo" alt="Quarterly revenue">') == "Quarterly revenue"


def test_an_image_without_alt_text_adds_nothing() -> None:
    assert html_to_text('<p>text</p><img src="spacer.gif">') == "text"


def test_a_link_keeps_its_label_and_its_url() -> None:
    """ "View invoice" alone tells an agent nothing it can act on."""
    assert html_to_text('<a href="https://billing.nebius.com/i/5521">View invoice</a>') == (
        "View invoice <https://billing.nebius.com/i/5521>"
    )


def test_a_link_whose_label_is_the_url_does_not_repeat_it() -> None:
    assert html_to_text('<a href="https://x.test/a">https://x.test/a</a>') == "https://x.test/a"


def test_a_link_with_no_label_falls_back_to_its_url() -> None:
    assert html_to_text('<a href="https://x.test/b"></a>') == "https://x.test/b"


def test_a_non_web_link_is_not_inlined() -> None:
    """`mailto:` is already what the reader sees; a `cid:` is an inline attachment reference."""
    assert html_to_text('<a href="mailto:a@b.test">write us</a>') == "write us"
    assert html_to_text('<a href="cid:logo">logo</a>') == "logo"


def test_an_unquoted_href_attribute_is_read() -> None:
    assert html_to_text("<a href=https://x.test/c>link</a>") == "link <https://x.test/c>"


def test_an_unclosed_link_at_the_end_of_the_document_keeps_its_url() -> None:
    assert html_to_text('<p>see <a href="https://x.test/d">this') == "see this <https://x.test/d>"


# ------------------------------------------------------------------- whitespace


def test_source_indentation_does_not_survive() -> None:
    html = "<table>\n    <tr>\n        <td>\n            the sentence\n        </td>\n    </tr>\n</table>"
    assert html_to_text(html) == "the sentence"


def test_runs_of_blank_lines_are_capped() -> None:
    assert html_to_text("<div><br><br><br><br><p>text</p></div>") == "text"


def test_leading_and_trailing_whitespace_is_trimmed() -> None:
    assert html_to_text("\n\n   <p>  padded  </p>  \n\n") == "padded"


def test_table_cells_share_a_line() -> None:
    """Exchange renders invoice line items as table rows, so a row must stay one line."""
    html = "<table><tr><td>Compute</td><td>42,00 EUR</td></tr><tr><td>Storage</td><td>7,00 EUR</td></tr></table>"
    rendered = html_to_text(html)
    assert rendered is not None
    assert "Compute 42,00 EUR" in rendered
    assert "Storage 7,00 EUR" in rendered


def test_a_block_inside_a_cell_still_breaks_the_line() -> None:
    assert html_to_text(
        "<table><tr><td><p>alpha</p><p>beta</p></td><td>gamma</td></tr></table>"
    ) == ("alpha\n\nbeta\ngamma")


# ---------------------------------------------------------------- nothing to say


@pytest.mark.parametrize(
    "html",
    [
        None,
        "",
        "<p></p>",
        "<div>   \n\t  </div>",
        "<p>&nbsp;</p>",
        "<style>p{color:red}</style>",
        "<script>var x=1;</script>",
    ],
)
def test_a_body_with_nothing_readable_in_it_yields_none(html: str | None) -> None:
    """None, not an empty string: an empty `body_text` reads as a message with an empty body."""
    assert html_to_text(html) is None


# ------------------------------------------------------------ malformed markup


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>unclosed", "unclosed"),
        ("<div><p>one<p>two", "one\ntwo"),
        ("<b>bold <i>both</b> italic", "bold both italic"),
        ("<p>text</p></div></p>", "text"),
        ("<p/>self closed", "self closed"),
        ("<unknown-tag>content</unknown-tag>", "content"),
        # A `<` that cannot begin a tag is prose, not markup to be swallowed.
        ("<<>>weird", "<<>>weird"),
    ],
)
def test_malformed_markup_is_rendered_rather_than_rejected(html: str, expected: str) -> None:
    assert html_to_text(html) == expected


def test_a_pathologically_deep_document_does_not_recurse() -> None:
    depth = 5_000
    assert html_to_text("<div>" * depth + "deep" + "</div>" * depth) == "deep"


def test_a_stray_closing_tag_does_not_crash_or_leak() -> None:
    assert html_to_text("</a></style></script><p>text</p>") == "text"


def test_nested_suppressed_elements_stay_suppressed_until_the_outermost_closes() -> None:
    """Depth counting, not a boolean: closing the inner one must not resume output early."""
    assert (
        html_to_text("<noscript><noscript>LEAK</noscript>still hidden</noscript>shown") == "shown"
    )


def test_an_unclosed_suppressed_element_hides_the_rest_of_the_document() -> None:
    assert html_to_text("<p>before</p><style>p{color:red}") == "before"


def test_a_large_document_is_rendered_in_linear_time() -> None:
    """No quadratic behaviour in the whitespace pass, which the parser output feeds."""
    import time

    html = "<p>sentence</p>" * 20_000
    start = time.monotonic()
    rendered = html_to_text(html)
    elapsed = time.monotonic() - start
    assert rendered is not None and rendered.count("sentence") == 20_000
    assert elapsed < 10, f"took {elapsed:.1f}s for 20k paragraphs"


def test_a_parser_failure_degrades_to_no_text_rather_than_raising(monkeypatch) -> None:
    from gmail_automator.html_text import _TextRenderer

    def explode(self, html: str) -> None:
        raise RuntimeError("parser gave up")

    monkeypatch.setattr(_TextRenderer, "feed", explode)
    assert html_to_text("<p>anything</p>") is None


# --------------------------------------------------------------- a whole message


def test_a_realistic_exchange_invoice_body() -> None:
    html = """<html xmlns:v="urn:schemas-microsoft-com:vml"><head>
    <meta charset="utf-8"><title>Invoice</title>
    <style type="text/css">body{margin:0;padding:0}.btn{background:#0a7}</style>
    <!--[if mso]><table><tr><td>MSO</td></tr></table><![endif]-->
    </head><body bgcolor="#f6f6f6">
    <table role="presentation" width="100%"><tr><td align="center">
    <h1>Your invoice is ready</h1>
    <p>Hi&nbsp;Dana,</p>
    <p>Invoice&nbsp;<strong>#5521</strong> is attached and totals <em>49,00&nbsp;&euro;</em>.</p>
    <table><tr><th align="left">Item</th><th align="left">Amount</th></tr>
    <tr><td>Compute</td><td>42,00 EUR</td></tr>
    <tr><td>Storage</td><td>7,00 EUR</td></tr>
    <tr><td><b>Total</b></td><td><b>49,00 EUR</b></td></tr></table>
    <p><a class="btn" href="https://billing.nebius.com/i/5521">View invoice</a></p>
    <img src="cid:logo" alt="Nebius">
    </td></tr></table>
    <script>track('open');</script>
    </body></html>"""

    rendered = html_to_text(html)
    assert rendered is not None
    assert "Your invoice is ready" in rendered
    assert "Hi Dana," in rendered
    assert "Invoice #5521 is attached and totals 49,00 €." in rendered
    assert "Compute 42,00 EUR" in rendered
    assert "Total 49,00 EUR" in rendered
    assert "View invoice <https://billing.nebius.com/i/5521>" in rendered
    assert "Nebius" in rendered
    # Nothing presentational survived.
    for leak in ("MSO", "SCRIPT", "track(", "Invoice</title>", "color:#0a7", "#f6f6f6", "{"):
        assert leak not in rendered
    # And it is not one enormous whitespace blob.
    assert "\n\n\n" not in rendered
