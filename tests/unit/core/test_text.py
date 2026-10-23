from jobpulse.core.text import collapse_whitespace, html_to_text


def test_greenhouse_entity_escaped_content_becomes_text() -> None:
    content = (
        "&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;Build APIs &amp;amp; services.&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;3+ years&lt;/li&gt;&lt;li&gt;Go&amp;nbsp;or Python&lt;/li&gt;&lt;/ul&gt;"
    )
    assert html_to_text(content) == "About\nBuild APIs & services.\n3+ years\nGo or Python"


def test_plain_html() -> None:
    assert html_to_text("<div><p>One</p><p>Two <b>bold</b></p></div>") == "One\nTwo bold"


def test_line_breaks() -> None:
    assert html_to_text("first<br>second<br/>third") == "first\nsecond\nthird"


def test_script_and_style_are_dropped() -> None:
    assert html_to_text("<style>p { color: red }</style><p>Hi</p><script>run()</script>") == "Hi"


def test_empty_input() -> None:
    assert html_to_text(None) is None
    assert html_to_text("") is None
    assert html_to_text("<p> </p>") is None


def test_collapse_whitespace() -> None:
    assert collapse_whitespace("  a \t b\n c\N{NO-BREAK SPACE}d ") == "a b c d"
