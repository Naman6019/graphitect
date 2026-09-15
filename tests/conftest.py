"""Autouse network guard.

An earlier test (test_synthesize_raw_grounding_with_key_but_missing_package_
exits_cleanly) silently turned into a live call to Anthropic's API once the
`anthropic` package happened to be installed for an unrelated manual check -
it only failed harmlessly because the API key was fake. A real key would
have spent money. This fixture makes that class of bug impossible: any test
that needs a real network call must explicitly opt out via the
`allow_network` marker.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Network access attempted during a test with no `allow_network` marker. "
            "graphitect's test suite must never make a real API call - use a fake/mock "
            "backend instead (see tests/test_engine.py's FakeBackend)."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: opt this test out of the autouse network block"
    )
