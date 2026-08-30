"""Outbound-request allowlist.

Directly closes finding **S-3** from the portfolio audit: the original payment
control plane accepted a `webhook` URL from the caller and performed a
server-side `fetch()` to it with no validation. That is a textbook SSRF — an
attacker with the shared token could make the server POST attacker-controlled
bodies to cloud metadata endpoints or internal services.

Every outbound URL in this system passes through `EgressGuard` first.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class EgressViolationError(Exception):
    """Raised when a URL fails the allowlist. Never downgraded to a warning."""


_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # cloud metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class EgressGuard:
    def __init__(self, allowed_hosts: list[str], *, allow_private: bool = False) -> None:
        self.allowed_hosts = [h.lower().lstrip(".") for h in allowed_hosts]
        self.allow_private = allow_private

    def _host_allowed(self, host: str) -> bool:
        host = host.lower()
        return any(host == a or host.endswith(f".{a}") for a in self.allowed_hosts)

    def _resolves_private(self, host: str) -> bool:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # Unresolvable is not automatically safe, but it cannot be
            # reached either. Treat as private so it is rejected.
            return True
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if any(ip in network for network in _BLOCKED_NETWORKS):
                return True
        return False

    def check(self, url: str) -> None:
        """Raise `EgressViolationError` unless `url` is safe to call."""
        parsed = urlparse(url)

        if parsed.scheme not in {"https", "http"}:
            raise EgressViolationError(f"scheme {parsed.scheme!r} is not permitted")
        if parsed.scheme == "http" and not self.allow_private:
            raise EgressViolationError("plaintext http is not permitted for outbound callbacks")
        host = parsed.hostname
        if not host:
            raise EgressViolationError("URL has no host")
        if not self._host_allowed(host):
            raise EgressViolationError(
                f"host {host!r} is not in the egress allowlist {self.allowed_hosts}"
            )
        # Allowlisted *and* not resolving into private space: an allowlisted
        # domain whose DNS points at 169.254.169.254 is still an attack.
        if not self.allow_private and self._resolves_private(host):
            raise EgressViolationError(f"host {host!r} resolves to a private or link-local address")

    def is_allowed(self, url: str) -> bool:
        try:
            self.check(url)
        except EgressViolationError:
            return False
        return True
