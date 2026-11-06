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
