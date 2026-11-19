import httpx
import pytest

from matrix_tldr.security import (
    BlockedURLError,
    HostResolutionError,
    PinnedTransport,
    blocked_reason,
    parse_ip_literal,
    validate_url,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def resolver_for(mapping: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise HostResolutionError(host)
        return mapping[host]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/",
        "https://LOCALHOST/",
        "https://foo.localhost/",
        "https://127.0.0.1/",
        "https://127.1/",
        "https://10.0.0.1/",
        "https://10.1.2.3/",
        "https://172.16.5.5/",
        "https://192.168.1.1/",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.64.0.1/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:7f00:1]/",
        "https://[fe80::1]/",
        "https://[fc00::1]/",
        "https://[64:ff9b::7f00:1]/",
        "https://[2002:7f00:1::]/",
        "https://2130706433/",  # dezimal = 127.0.0.1
        "https://0x7f000001/",  # hexadezimal
        "https://0x7f.0.0.1/",
        "https://0177.0.0.1/",  # oktal
        "https://017700000001/",
        "https://0.0.0.0/",
        "https://0/",
        "https://[::]/",
        "https://224.0.0.1/",
        "https://255.255.255.255/",
        "https://08.1.1.1/",  # ungültiges Oktal: fail closed
        "https://127.0.0.1./",  # trailing dot
        "https://intranet/",  # Einzel-Label
        "https://nas.local/",
        "https://router.home.arpa/",
        "https://service.internal/",
    ],
)
async def test_internal_targets_are_blocked(url):
    with pytest.raises(BlockedURLError):
        await validate_url(url, resolver=resolver_for({}))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "ftp://example.org/",
        "https://example.org:8443/",
        "https://example.org:80/",
        "https://user:pass@example.org/",
        "https://example.org\r\nHost: evil/",
        "https:///nohost",
    ],
)
async def test_scheme_port_userinfo_and_control_chars_are_blocked(url):
    with pytest.raises(BlockedURLError):
        await validate_url(url, resolver=resolver_for({"example.org": [PUBLIC_V4]}))


async def test_public_ip_literal_is_allowed():
    target = await validate_url(f"https://{PUBLIC_V4}/x", resolver=resolver_for({}))
    assert target.ips == (PUBLIC_V4,)


async def test_public_hostname_is_allowed_and_resolved_ips_are_returned():
    target = await validate_url(
        "https://example.org/x", resolver=resolver_for({"example.org": [PUBLIC_V4, PUBLIC_V6]})
    )
    assert target.host == "example.org"
    assert target.ips == (PUBLIC_V4, PUBLIC_V6)


@pytest.mark.parametrize(
    "resolved",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["192.168.0.10"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:10.0.0.1"],
        ["fd00::1"],
        ["0.0.0.0"],
        [PUBLIC_V4, "10.0.0.5"],  # EINE interne Adresse genügt
        [PUBLIC_V6, "fe80::1"],
    ],
)
async def test_hostname_resolving_to_internal_address_is_blocked(resolved):
    with pytest.raises(BlockedURLError):
        await validate_url(
            "https://evil.example/", resolver=resolver_for({"evil.example": resolved})
        )


async def test_unresolvable_host_raises_resolution_error():
    with pytest.raises(HostResolutionError):
        await validate_url("https://gibt-es-nicht.example/", resolver=resolver_for({}))


def test_parse_ip_literal_normalizes_special_notations():
    assert str(parse_ip_literal("2130706433")) == "127.0.0.1"
    assert str(parse_ip_literal("0x7f000001")) == "127.0.0.1"
    assert str(parse_ip_literal("0177.0.0.1")) == "127.0.0.1"
    assert str(parse_ip_literal("127.1")) == "127.0.0.1"
    assert parse_ip_literal("example.org") is None
    assert parse_ip_literal("1.2.3.4.example.org") is None


def test_blocked_reason_unpacks_ipv4_mapped_and_allows_public():
    assert blocked_reason(parse_ip_literal("::ffff:127.0.0.1")) is not None
    assert blocked_reason(parse_ip_literal("::ffff:8.8.8.8")) is None
    assert blocked_reason(parse_ip_literal("8.8.8.8")) is None
    assert blocked_reason(parse_ip_literal(PUBLIC_V6)) is None


# ----------------------------------------------------------------- PinnedTransport


async def test_transport_pins_ip_and_keeps_host_and_sni():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok")

    transport = PinnedTransport(
        resolver=resolver_for({"example.org": [PUBLIC_V4]}), inner=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get("https://example.org/pfad?q=1")

    assert response.status_code == 200
    assert seen == {
        "url": f"https://{PUBLIC_V4}/pfad?q=1",
        "host": "example.org",
        "sni": "example.org",
    }


async def test_transport_pins_ipv6_literal_in_brackets():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200)

    transport = PinnedTransport(
        resolver=resolver_for({"example.org": [PUBLIC_V6]}), inner=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        await client.get("https://example.org/")
    assert seen["url"] == f"https://[{PUBLIC_V6}]/"


async def test_transport_never_calls_inner_for_blocked_target():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    transport = PinnedTransport(
        resolver=resolver_for({"evil.example": ["10.0.0.9"]}), inner=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(BlockedURLError):
            await client.get("https://evil.example/")
    assert calls == []


async def test_transport_resolves_exactly_once_per_request_against_rebinding():
    """DNS-Rebinding: ein zweiter Lookup mit anderem Ergebnis darf nie stattfinden."""
    answers = iter([[PUBLIC_V4], ["127.0.0.1"]])
    lookups: list[str] = []

    async def rebinding_resolver(host: str, port: int) -> list[str]:
        lookups.append(host)
        return next(answers)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200)

    transport = PinnedTransport(resolver=rebinding_resolver, inner=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        await client.get("https://rebind.example/")
    assert lookups == ["rebind.example"]
    assert seen == [PUBLIC_V4]  # verbunden wurde mit der geprüften IP
