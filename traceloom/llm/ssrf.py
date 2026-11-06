"""SSRF guard shared by webhooks and LLM connections.

`resolve_target` resolves the host once, checks every address it resolves to, and returns the
address to connect to. Callers must connect to that pinned IP (`PinnedTarget.connect_url` with the
original `Host` header / TLS server name) so a second DNS answer cannot swap in another address.

Always refused: link-local (includes 169.254.169.254), cloud metadata addresses, unspecified,
multicast and reserved ranges. Refused unless `allow_private`: loopback, RFC 1918, unique-local,
carrier-grade NAT and anything else that is not globally routable.
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
        default = SCHEMES[self.scheme]
        return self.host if self.port == default else f"{self.host}:{self.port}"


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve host {host!r}") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def classify(address: str) -> str:
    """'blocked' (never allowed), 'private' (allowed by policy) or 'public'."""
    ip: IP = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
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
    addresses = resolver(parts.hostname, port)
    if not addresses:
        raise BlockedAddress(f"cannot resolve host {parts.hostname!r}")
    for address in addresses:  # every answer must pass, not just the one we connect to
        kind = classify(address)
        if kind == "blocked" or (kind == "private" and not allow_private):
            raise BlockedAddress(f"{parts.hostname} resolves to a blocked address")
    return PinnedTarget(url, parts.scheme, parts.hostname, port, addresses[0])
