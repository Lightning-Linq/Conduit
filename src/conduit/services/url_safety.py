"""
SSRF defense — validates outbound URLs before Conduit fetches them.

Conduit makes HTTP calls in two places that take user/provider input:
  1. skill_executor.execute_skill_webhook  — POSTs the payment proof to a
     provider-supplied endpoint_url.
  2. provider_verification.verify_domain   — GETs https://<domain>/.well-known/...

Without validation, either path can be steered at internal services
(cloud metadata, localhost, RFC1918 ranges) and used as a probe primitive
or — worse, in skill_executor's case — to exfiltrate the payment preimage
to an attacker-chosen URL.

This module rejects unsafe URLs *before* any DNS or socket activity that
the request would trigger. It does not defend against DNS rebinding
(the resolver can return different IPs between validation and connect);
for that, callers should follow up with a constrained transport.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL points at an address Conduit refuses to talk to."""


# Default allow-list. Skill executor and domain verification both
# require HTTPS — there's no reason a real provider should serve their
# webhook over plaintext.
DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason string if the IP is in a blocked range, else None.

    Order matters: more-specific labels first so error messages are
    accurate (169.254.x.x is link-local AND private in stdlib, but
    "link-local" is the more useful label for the operator).
    """
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address (0.0.0.0 / ::)"
    if ip.is_private:
        return "private (RFC1918) address"
    if ip.is_reserved:
        return "reserved address"
    # IPv4 specifics
    if isinstance(ip, ipaddress.IPv4Address):
        # 169.254.169.254 is link-local (caught above) but be explicit
        if str(ip) == "169.254.169.254":
            return "cloud metadata service"
        # Carrier-grade NAT
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return "CGNAT address"
    return None


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to all of its A/AAAA records. May raise socket.gaierror."""
    # Try literal first — covers attackers who pass an IP directly
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {e}") from e

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            addrs.append(ipaddress.IPv4Address(sockaddr[0]))
        elif family == socket.AF_INET6:
            # sockaddr[0] may include scope id for IPv6 (e.g. "fe80::1%eth0")
            addr_str = sockaddr[0].split("%", 1)[0]
            addrs.append(ipaddress.IPv6Address(addr_str))
    return addrs


def validate_outbound_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES,
) -> str:
    """
    Validate an outbound URL. Returns the URL on success; raises
    UnsafeURLError on any rejection.

    Checks:
      - scheme is in `allowed_schemes`
      - host parses (no empty / userinfo-only / port-only)
      - host resolves to at least one public, routable IP
      - NO resolved IP is loopback, private, link-local, multicast,
        reserved, unspecified, CGNAT, or cloud-metadata.

    Note: this does not prevent DNS rebinding. For full defense, callers
    should pin a transport to the resolved IP after validation.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL is empty")

    parsed = urlparse(url.strip())

    if parsed.scheme.lower() not in allowed_schemes:
        raise UnsafeURLError(
            f"scheme {parsed.scheme!r} is not allowed "
            f"(allowed: {sorted(allowed_schemes)})"
        )

    if not parsed.hostname:
        raise UnsafeURLError(f"URL has no host: {url!r}")

    # Reject userinfo in URL (foo@host can be confusing / used in tricks)
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with userinfo are not allowed")

    # Reject non-default ports unless explicitly safe (443 for https, 80 for http)
    # This is a defense-in-depth step; the IP check below is the main gate.
    if parsed.port is not None and parsed.port not in (80, 443):
        raise UnsafeURLError(f"non-standard port {parsed.port} is not allowed")

    addrs = _resolve(parsed.hostname)
    if not addrs:
        raise UnsafeURLError(f"host {parsed.hostname!r} did not resolve to any IP")

    for ip in addrs:
        reason = _is_unsafe_ip(ip)
        if reason is not None:
            raise UnsafeURLError(
                f"host {parsed.hostname!r} resolves to {ip} ({reason}) — refusing to connect"
            )

    return url


def resolve_and_validate(url: str) -> tuple[str, str, list[str]]:
    """
    Validate an outbound URL AND return the resolved IPs.

    Returns (url, hostname, [resolved_ip_strings]).
    Callers should connect to the resolved IP to prevent DNS rebinding (H3).
    """
    validated = validate_outbound_url(url)
    parsed = urlparse(validated)
    hostname = parsed.hostname or ""
    addrs = _resolve(hostname)
    return validated, hostname, [str(ip) for ip in addrs]


def validate_domain(domain: str) -> str:
    """
    Validate a bare hostname (no scheme). Used by provider domain verification,
    which constructs https://<domain>/.well-known/... at the call site.

    Returns the normalized domain on success; raises UnsafeURLError otherwise.
    """
    if not isinstance(domain, str) or not domain.strip():
        raise UnsafeURLError("domain is empty")

    d = domain.strip().lower()

    # Don't allow scheme/path in what should be a bare hostname
    if "/" in d or ":" in d or "@" in d or " " in d:
        raise UnsafeURLError(f"domain {domain!r} must be a bare hostname")

    # Reuse the URL validator by synthesizing the https URL we'd fetch.
    validate_outbound_url(f"https://{d}/")
    return d
