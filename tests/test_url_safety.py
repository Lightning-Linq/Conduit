"""Tests for the URL safety / SSRF defense module."""

import pytest

from conduit.services.url_safety import (
    UnsafeURLError,
    validate_domain,
    validate_outbound_url,
    validate_relay_url,
)


class TestSchemeAllowList:
    def test_https_allowed(self):
        validate_outbound_url("https://example.com/")

    def test_http_rejected(self):
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_outbound_url("http://example.com/")

    def test_file_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("file:///etc/passwd")

    def test_ftp_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("ftp://example.com/")

    def test_empty_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("")


class TestIPLiteralBlocking:
    """An attacker who passes an IP literal must be rejected before any
    network activity."""

    def test_localhost_rejected(self):
        with pytest.raises(UnsafeURLError, match="loopback"):
            validate_outbound_url("https://127.0.0.1/")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(UnsafeURLError, match="loopback"):
            validate_outbound_url("https://[::1]/")

    def test_rfc1918_10_rejected(self):
        with pytest.raises(UnsafeURLError, match="private"):
            validate_outbound_url("https://10.0.0.1/")

    def test_rfc1918_172_rejected(self):
        with pytest.raises(UnsafeURLError, match="private"):
            validate_outbound_url("https://172.16.0.1/")

    def test_rfc1918_192_rejected(self):
        with pytest.raises(UnsafeURLError, match="private"):
            validate_outbound_url("https://192.168.1.1/")

    def test_link_local_rejected(self):
        with pytest.raises(UnsafeURLError, match="link-local"):
            validate_outbound_url("https://169.254.1.1/")

    def test_cloud_metadata_rejected(self):
        """169.254.169.254 — the cloud metadata service. Must be blocked."""
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://169.254.169.254/latest/meta-data/")

    def test_cgnat_rejected(self):
        with pytest.raises(UnsafeURLError, match="CGNAT"):
            validate_outbound_url("https://100.64.0.1/")

    def test_unspecified_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://0.0.0.0/")


class TestEmbeddedIPv4Bypass:
    """IPv6 addresses that tunnel a private IPv4 (IPv4-mapped, 6to4, Teredo)
    must be rejected. stdlib's is_* checks miss 6to4 in particular, which is a
    real SSRF bypass — 2002:7f00:1:: encodes 127.0.0.1."""

    def test_6to4_loopback_rejected(self):
        # 2002:7f00:1:: is the 6to4 encoding of 127.0.0.1.
        with pytest.raises(UnsafeURLError, match="loopback"):
            validate_outbound_url("https://[2002:7f00:1::]/")

    def test_6to4_cloud_metadata_rejected(self):
        # 2002:a9fe:a9fe:: encodes 169.254.169.254 (the cloud metadata IP).
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://[2002:a9fe:a9fe::]/latest/meta-data/")

    def test_ipv4_mapped_loopback_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://[::ffff:127.0.0.1]/")

    def test_ipv4_mapped_private_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://[::ffff:10.0.0.1]/")


class TestHostnameResolution:
    """Hostnames are resolved and every resulting IP is checked."""

    def test_localhost_name_rejected(self, monkeypatch):
        # Force the resolver to return loopback so this works without DNS.
        import socket


        def fake_getaddrinfo(host, port):
            return [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(UnsafeURLError):
            validate_outbound_url("https://localhost.evil.example/")

    def test_unresolvable_rejected(self, monkeypatch):
        import socket

        def fake_getaddrinfo(host, port):
            raise socket.gaierror("nodename nor servname provided")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(UnsafeURLError, match="DNS"):
            validate_outbound_url("https://this.does.not.resolve.example/")


class TestUserinfoAndPorts:
    def test_userinfo_rejected(self):
        with pytest.raises(UnsafeURLError, match="userinfo"):
            validate_outbound_url("https://user:pass@example.com/")

    def test_weird_port_rejected(self):
        with pytest.raises(UnsafeURLError, match="port"):
            validate_outbound_url("https://example.com:5432/")


class TestDomainHelper:
    def test_bare_domain_passes(self):
        # Note: this hits real DNS for example.com. If you want to keep
        # tests offline, monkeypatch getaddrinfo.
        validate_domain("example.com")

    def test_domain_with_scheme_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_domain("https://example.com")

    def test_domain_with_path_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_domain("example.com/admin")

    def test_localhost_domain_rejected(self, monkeypatch):
        import socket

        def fake_getaddrinfo(host, port):
            return [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(UnsafeURLError):
            validate_domain("internal.example")


class TestRelayURL:
    """validate_relay_url — SSRF guard for Nostr relay websockets (wss only)."""

    def test_public_wss_allowed(self):
        # Literal public IP -> no DNS, no network.
        assert validate_relay_url("wss://1.1.1.1") == "wss://1.1.1.1"

    def test_ws_plaintext_rejected(self):
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_relay_url("ws://1.1.1.1")

    def test_https_scheme_rejected(self):
        # Only wss is a valid relay scheme; an http(s) URL is not a relay.
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_relay_url("https://1.1.1.1")

    def test_loopback_rejected(self):
        with pytest.raises(UnsafeURLError, match="loopback"):
            validate_relay_url("wss://127.0.0.1")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(UnsafeURLError, match="loopback"):
            validate_relay_url("wss://[::1]")

    def test_private_rejected(self):
        with pytest.raises(UnsafeURLError, match="private"):
            validate_relay_url("wss://10.0.0.1")

    def test_cloud_metadata_rejected(self):
        with pytest.raises(UnsafeURLError):
            validate_relay_url("wss://169.254.169.254")
