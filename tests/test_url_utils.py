from matrix_tldr.url_utils import display_domain, extract_urls, strip_reply_fallback


def test_trailing_punctuation_is_trimmed():
    body = (
        "Schau mal: https://example.org/a. Und https://example.org/b, dann (https://example.org/c)!"
    )
    assert extract_urls(body, max_links=10) == [
        "https://example.org/a",
        "https://example.org/b",
        "https://example.org/c",
    ]


def test_balanced_parentheses_are_kept():
    url = "https://de.wikipedia.org/wiki/Foo_(Bar)"
    assert extract_urls(f"siehe {url}", max_links=5) == [url]
    assert extract_urls(f"(siehe {url})", max_links=5) == [url]


def test_quotes_and_angle_brackets():
    assert extract_urls('"https://example.org/x"', max_links=5) == ["https://example.org/x"]
    assert extract_urls("<https://example.org/x>", max_links=5) == ["https://example.org/x"]


def test_markdown_link_target_is_extracted_without_paren():
    assert extract_urls("[Titel](https://example.org/x)", max_links=5) == ["https://example.org/x"]


def test_max_links_limits_result():
    body = "https://a.example/1 https://b.example/2 https://c.example/3"
    assert extract_urls(body, max_links=1) == ["https://a.example/1"]
    assert len(extract_urls(body, max_links=2)) == 2


def test_http_and_other_schemes_are_ignored():
    body = "http://example.org ftp://example.org javascript:alert(1) file:///etc/passwd"
    assert extract_urls(body, max_links=5) == []


def test_http_ignored_but_https_kept():
    assert extract_urls("http://a.example https://b.example", max_links=5) == ["https://b.example"]


def test_duplicates_are_removed():
    body = "https://example.org/a https://example.org/a"
    assert extract_urls(body, max_links=5) == ["https://example.org/a"]


def test_formatted_body_hrefs_are_added_without_duplicates():
    body = "Lies https://example.org/a und das hier"
    html = '<a href="https://example.org/a">link</a> <a href="https://example.org/b">zwei</a>'
    assert extract_urls(body, html, max_links=5) == [
        "https://example.org/a",
        "https://example.org/b",
    ]


def test_formatted_body_href_with_hidden_target():
    html = '<a href="https://example.org/geheim">klick hier</a>'
    assert extract_urls("klick hier", html, max_links=5) == ["https://example.org/geheim"]


def test_href_with_http_is_ignored():
    html = '<a href="http://example.org/a">x</a><a href="javascript:alert(1)">y</a>'
    assert extract_urls("", html, max_links=5) == []


def test_broken_html_does_not_crash():
    assert extract_urls("x", "<a href=<<<>>> https://", max_links=5) == []


def test_invalid_port_is_skipped():
    assert extract_urls("https://example.org:99999/x https://ok.example/", max_links=5) == [
        "https://ok.example/"
    ]


def test_reply_fallback_is_stripped():
    body = "> <@a:x> https://old.example/zitat\n\nneue Nachricht"
    html = "<mx-reply><blockquote>https://old.example/zitat</blockquote></mx-reply>neu"
    new_body, new_html = strip_reply_fallback(body, html)
    assert extract_urls(new_body, new_html, max_links=5) == []
    assert new_body == "neue Nachricht"


def test_display_domain_strips_www():
    assert display_domain("https://www.example.org/pfad") == "example.org"
    assert display_domain("https://news.example.org/") == "news.example.org"


def test_mention_pill_is_not_a_link():
    body = "@tldr:m.sgr.ski was meinst du?"
    formatted = '<a href="https://matrix.to/#/@tldr:m.sgr.ski">tldr</a> was meinst du?'
    assert extract_urls(body, formatted, max_links=5) == []


def test_matrix_to_permalinks_are_ignored_but_real_links_kept():
    body = "https://matrix.to/#/!room:server/$event https://example.org/a"
    assert extract_urls(body, max_links=5) == ["https://example.org/a"]
