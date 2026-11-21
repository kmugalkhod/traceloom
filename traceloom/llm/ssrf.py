"""SSRF guard shared by webhooks and LLM connections.

`resolve_target` resolves the host once, checks every address it resolves to, and returns the
address to connect to. Callers must connect to that pinned IP (`PinnedTarget.connect_url` with the
original `Host` header / TLS server name) so a second DNS answer cannot swap in another address.

Always refused: link-local (includes 169.254.169.254), cloud metadata addresses, unspecified,
multicast and reserved ranges. Refused unless `allow_private`: loopback, RFC 1918, unique-local,
carrier-grade NAT and anything else that is not globally routable. IPv6 addresses that carry an
IPv4 address (IPv4-mapped, NAT64 `64:ff9b::/96` and `64:ff9b:1::/48`, 6to4 `2002::/16`) are judged
by the IPv4 address inside them.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

type IP = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254")
)
SCHEMES = {"http": 80, "https": 443}
NAT64_PREFIXES = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class BlockedAddress(ValueError):
    """The URL is malformed, unresolvable or points somewhere the policy forbids."""


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    scheme: str
    host: str  # the name from the URL: send it as the Host header and TLS server name
    port: int
    ip: str  # the validated address to connect to

    @property
    def connect_url(self) -> str:
        parts = urlsplit(self.url)
        ip = f"[{self.ip}]" if ":" in self.ip else self.ip
        return urlunsplit((self.scheme, f"{ip}:{self.port}", parts.path, parts.query, ""))

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host  # an IPv6 literal
        return host if self.port == SCHEMES[self.scheme] else f"{host}:{self.port}"


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve host {host!r}") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _embedded_ipv4(ip: IP) -> IP:
    """The IPv4 address an IPv6 translation/tunnel address points at, else `ip` itself."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if any(ip in net for net in NAT64_PREFIXES):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def classify(address: str) -> str:
    """'blocked' (never allowed), 'private' (allowed by policy) or 'public'."""
    ip: IP = _embedded_ipv4(ipaddress.ip_address(address.split("%")[0]))
    if (
        ip in METADATA_ADDRESSES
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or (ip.is_reserved and not ip.is_loopback)  # python lists ::1 as reserved
    ):
        return "blocked"
    return "public" if ip.is_global else "private"


def resolve_target(
    url: str, *, allow_private: bool = False, resolver: Resolver = system_resolver
) -> PinnedTarget:
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES:
        raise BlockedAddress("URL must start with http:// or https://")
    if not parts.hostname:
        raise BlockedAddress("URL has no host")
    if parts.username or parts.password:
        raise BlockedAddress("URL must not contain credentials")
    try:
        port = parts.port or SCHEMES[parts.scheme]
    except ValueError as exc:
        raise BlockedAddress("URL has an invalid port") from exc
    try:  # an IP literal is checked as written, never handed to a resolver
        addresses = [str(ipaddress.ip_address(parts.hostname))]
    except ValueError:
        addresses = resolver(parts.hostname, port)
    if not addresses:
        raise BlockedAddress(f"cannot resolve host {parts.hostname!r}")
    for address in addresses:  # every answer must pass, not just the one we connect to
        kind = classify(address)
        if kind == "blocked" or (kind == "private" and not allow_private):
            raise BlockedAddress(f"{parts.hostname} resolves to a blocked address")
    return PinnedTarget(url, parts.scheme, parts.hostname, port, addresses[0])
