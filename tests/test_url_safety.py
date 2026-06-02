"""Tests for the URL safety / SSRF defense module."""

import pytest

from conduit.services.url_safety import (
    UnsafeURLError,
    validate_domain,
    validate_outbound_url,
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
