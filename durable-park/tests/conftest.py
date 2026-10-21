import ipaddress
import os
import socket

import pytest

_LIVE = os.environ.get("DURABLE_PARK_LIVE") == "1"


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Unit tests never reach the network (they once opened a real withHuman request through the default
    token file). Loopback and Unix sockets stay allowed; opt-in live tests (DURABLE_PARK_LIVE=1) are exempt."""
    if _LIVE:
        return
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None:
            try:
                loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise AssertionError(f"unit test tried to reach the network: {host}")
        return real_connect(self, address)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch, tmp_path_factory):
    """The plugin's default token file is the VM's real withHuman credential: point it at nothing."""
    monkeypatch.setattr("durable_park.DEFAULT_TOKEN_FILE", str(tmp_path_factory.getbasetemp() / "no-token"))


@pytest.fixture(autouse=True)
def _no_real_audit_key(monkeypatch, tmp_path_factory):
    """Likewise the optional withHuman audit key file."""
    monkeypatch.setattr("durable_park.DEFAULT_AUDIT_KEY_FILE", str(tmp_path_factory.getbasetemp() / "no-audit-key"))


@pytest.fixture(autouse=True)
def _permissive_tool_rules(monkeypatch):
    """Unit tests run without Hermes's tool registry: the default rules lookup accepts any object.
    Tests of the form check pass their own rules explicitly."""
    monkeypatch.setattr("durable_park.formcheck.registry_schema", lambda tool: {"type": "object"})
