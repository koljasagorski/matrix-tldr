import httpx
import pytest

from matrix_tldr.fetcher import (
    MAX_REDIRECTS,
    ContentTooLargeError,
    FetchBlockedError,
    FetchConnectionError,
    Fetcher,
    FetchHTTPError,
    FetchTimeoutError,
    NoContentError,
    UnsupportedContentError,
)
from matrix_tldr.security import HostResolutionError, PinnedTransport

PUBLIC_IP = "93.184.216.34"
PARAGRAPH = (
    "Dies ist ein ausreichend langer Absatz mit sinnvollem Inhalt über das Thema Nummer {n}, "
    "damit die Textextraktion genug Material findet und die Mindestlänge sicher überschritten "
    "wird. Jeder Absatz unterscheidet sich, weil Trafilatura identische Wiederholungen entfernt."
)
ARTICLE_HTML = (
    "<html><head><title>Mein Testartikel</title></head><body><article>"
    + "".join(f"<p>{PARAGRAPH.format(n=n)}</p>" for n in range(6))
    + "</article></body></html>"
)
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


async def fake_resolver(host: str, port: int) -> list[str]:
    table = {
        "example.org": [PUBLIC_IP],
        "other.example": [PUBLIC_IP],
        "intern.example": ["10.0.0.5"],
    }
    if host not in table:
        raise HostResolutionError(host)
    return table[host]


def make_fetcher(handler, *, max_bytes: int = 5_000_000) -> Fetcher:
    transport = PinnedTransport(resolver=fake_resolver, inner=httpx.MockTransport(handler))
    return Fetcher(timeout_seconds=5, max_bytes=max_bytes, transport=transport)


async def test_fetch_extracts_text_and_title():
    fetcher = make_fetcher(lambda r: httpx.Response(200, headers=HTML_HEADERS, text=ARTICLE_HTML))
    result = await fetcher.fetch("https://example.org/artikel")
    assert result.title == "Mein Testartikel"
    assert "ausreichend langer Absatz" in result.text
    assert result.url == "https://example.org/artikel"
    await fetcher.aclose()


async def test_request_headers():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers=HTML_HEADERS, text=ARTICLE_HTML)

    fetcher = make_fetcher(handler)
    await fetcher.fetch("https://example.org/")
    assert captured["user-agent"] == "matrix-tldr/1.0 (+Matrix-Bot)"
    assert "text/html" in captured["accept"]
    assert captured["accept-language"] == "de,en"
    await fetcher.aclose()


# ----------------------------------------------------------------- Redirects / SSRF


async def test_redirect_to_internal_ip_is_blocked():
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchBlockedError):
        await fetcher.fetch("https://example.org/")
    assert reached == [PUBLIC_IP]  # der interne Hop wurde nie angefragt
    await fetcher.aclose()


async def test_redirect_to_hostname_resolving_internally_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://intern.example/"})

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchBlockedError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_redirect_to_metadata_service_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://169.254.169.254/latest/meta-data"})

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchBlockedError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_redirect_to_http_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.org/x"})

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchBlockedError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_relative_redirect_is_followed_and_final_url_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/alt":
            return httpx.Response(301, headers={"location": "/neu"})
        return httpx.Response(200, headers=HTML_HEADERS, text=ARTICLE_HTML)

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch("https://example.org/alt")
    assert result.url == "https://example.org/neu"
    await fetcher.aclose()


async def test_redirect_chain_over_limit_fails():
    hops = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hops
        hops += 1
        return httpx.Response(302, headers={"location": "https://other.example/"})

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchHTTPError):
        await fetcher.fetch("https://example.org/")
    assert hops == MAX_REDIRECTS + 1
    await fetcher.aclose()


async def test_direct_internal_url_is_blocked_before_any_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    fetcher = make_fetcher(handler)
    for url in (
        "https://127.0.0.1/",
        "https://localhost/",
        "https://[::1]/",
        "https://2130706433/",
    ):
        with pytest.raises(FetchBlockedError):
            await fetcher.fetch(url)
    assert not called
    await fetcher.aclose()


# ----------------------------------------------------------------- Inhalt / Limits


async def test_size_limit_via_content_length():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={**HTML_HEADERS, "content-length": "999999"}, text="x")

    fetcher = make_fetcher(handler, max_bytes=1024)
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_size_limit_while_streaming_without_content_length():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=HTML_HEADERS, content=b"<p>" + b"a" * 5000)

    fetcher = make_fetcher(handler, max_bytes=2048)
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


@pytest.mark.parametrize(
    "content_type", ["application/pdf", "image/png", "video/mp4", "application/json", ""]
)
async def test_unsupported_content_type(content_type):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, content=b"%PDF-1.7")

    fetcher = make_fetcher(handler)
    with pytest.raises(UnsupportedContentError):
        await fetcher.fetch("https://example.org/datei")
    await fetcher.aclose()


async def test_xhtml_is_accepted():
    fetcher = make_fetcher(
        lambda r: httpx.Response(
            200, headers={"content-type": "application/xhtml+xml"}, text=ARTICLE_HTML
        )
    )
    assert (await fetcher.fetch("https://example.org/")).text
    await fetcher.aclose()


async def test_too_short_text_is_treated_as_no_content():
    html = "<html><body><article><p>Kurz.</p></article></body></html>"
    fetcher = make_fetcher(lambda r: httpx.Response(200, headers=HTML_HEADERS, text=html))
    with pytest.raises(NoContentError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_empty_page_is_treated_as_no_content():
    fetcher = make_fetcher(lambda r: httpx.Response(200, headers=HTML_HEADERS, text=""))
    with pytest.raises(NoContentError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_paywall_statuses_mean_no_readable_content(status):
    fetcher = make_fetcher(lambda r: httpx.Response(status, headers=HTML_HEADERS, text="login"))
    with pytest.raises(NoContentError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_error_statuses_raise_http_error_with_status_code(status):
    fetcher = make_fetcher(lambda r: httpx.Response(status, headers=HTML_HEADERS, text="nope"))
    with pytest.raises(FetchHTTPError) as info:
        await fetcher.fetch("https://example.org/")
    assert info.value.status_code == status
    await fetcher.aclose()


async def test_timeout_is_mapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu langsam", request=request)

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchTimeoutError):
        await fetcher.fetch("https://example.org/")
    await fetcher.aclose()


async def test_connection_error_and_unresolvable_host_are_mapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fetcher = make_fetcher(handler)
    with pytest.raises(FetchConnectionError):
        await fetcher.fetch("https://example.org/")
    with pytest.raises(FetchConnectionError):
        await fetcher.fetch("https://gibt-es-nicht.example/")
    await fetcher.aclose()


async def test_latin1_charset_is_decoded():
    html = ARTICLE_HTML.replace("Überschrift", "Überschrift").encode("latin-1")
    fetcher = make_fetcher(
        lambda r: httpx.Response(
            200, headers={"content-type": "text/html; charset=iso-8859-1"}, content=html
        )
    )
    result = await fetcher.fetch("https://example.org/")
    assert "über das Thema" in result.text
    await fetcher.aclose()
