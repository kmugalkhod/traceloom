import pytest

from traceloom.llm.ssrf import BlockedAddress, classify, resolve_target


def fixed(*addresses):
    return lambda host, port: list(addresses)


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",
        "169.254.1.1",
        "fe80::1",
        "fd00:ec2::254",
        "100.100.100.200",
        "0.0.0.0",
        "::ffff:169.254.169.254",
    ],
)
@pytest.mark.parametrize("allow_private", [False, True])
def test_metadata_and_link_local_always_refused(ip, allow_private):
    with pytest.raises(BlockedAddress):
        resolve_target("http://x.example/", allow_private=allow_private, resolver=fixed(ip))


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.9",
        "fd12::1",
        "100.64.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_private_refused_unless_allowed(ip):
    with pytest.raises(BlockedAddress):
        resolve_target("https://x.example/", resolver=fixed(ip))
    assert resolve_target("https://x.example/", allow_private=True, resolver=fixed(ip)).ip == ip


def test_hostname_resolving_to_blocked_ip_refused():
    with pytest.raises(BlockedAddress):
        resolve_target("https://evil.example/", resolver=fixed("10.1.1.1"))


def test_any_blocked_answer_refuses():
    with pytest.raises(BlockedAddress):
        resolve_target("https://x.example/", resolver=fixed("93.184.216.34", "169.254.169.254"))


def test_ip_literal_is_checked_without_dns():
    with pytest.raises(BlockedAddress):
        resolve_target("http://169.254.169.254/latest/meta-data")
    with pytest.raises(BlockedAddress):
        resolve_target("http://127.0.0.1:8080/")


def test_pinned_ip_returned_for_the_request():
    t = resolve_target("https://api.example.com:8443/v1/x?a=1", resolver=fixed("93.184.216.34"))
    assert t.ip == "93.184.216.34" and t.host == "api.example.com" and t.port == 8443
    assert t.connect_url == "https://93.184.216.34:8443/v1/x?a=1"
    assert t.host_header == "api.example.com:8443"
    v6 = resolve_target("http://h.example/", resolver=fixed("2606:4700::1"))
    assert v6.connect_url == "http://[2606:4700::1]:80/" and v6.host_header == "h.example"


def test_an_ipv6_literal_host_is_bracketed_in_the_host_header():
    t = resolve_target("http://[::1]:8080/v1", allow_private=True)
    assert t.host_header == "[::1]:8080"
    assert resolve_target("https://[2606:4700::1]/").host_header == "[2606:4700::1]"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x.example/",
        "file:///etc/passwd",
        "http:///x",
        "http://u:p@x.example/",
        "http://x.example:99999/",
    ],
)
def test_bad_urls_refused(url):
    with pytest.raises(BlockedAddress):
        resolve_target(url, resolver=fixed("93.184.216.34"))


def test_classify_public():
    assert classify("93.184.216.34") == "public"


@pytest.mark.parametrize(
    "ip",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 well-known prefix + 169.254.169.254
        "64:ff9b:1::a9fe:a9fe",  # NAT64 local-use prefix (RFC 8215)
        "2002:a9fe:a9fe::1",  # 6to4 wrapping 169.254.169.254
        "2002:a9fe:1::",  # 6to4 wrapping link-local 169.254.0.1
    ],
)
@pytest.mark.parametrize("allow_private", [False, True])
def test_nat64_and_6to4_wrapped_metadata_is_refused(ip, allow_private):
    assert classify(ip) == "blocked"
    with pytest.raises(BlockedAddress):
        resolve_target("https://x.example/", allow_private=allow_private, resolver=fixed(ip))


@pytest.mark.parametrize("ip", ["64:ff9b::a00:5", "2002:a00:5::1", "2002:7f00:1::1"])
def test_nat64_and_6to4_wrapped_private_needs_allow_private(ip):
    assert classify(ip) == "private"


def test_nat64_and_6to4_wrapped_public_stays_public():
    assert classify("64:ff9b::5db8:d822") == "public"
    assert classify("2002:5db8:d822::1") == "public"


def test_ip_literal_is_never_handed_to_the_resolver():
    def lying(host, port):
        raise AssertionError(f"resolver asked about {host}")

    with pytest.raises(BlockedAddress):
        resolve_target("http://169.254.169.254/", allow_private=True, resolver=lying)
    assert resolve_target("https://93.184.216.34/", resolver=lying).ip == "93.184.216.34"
