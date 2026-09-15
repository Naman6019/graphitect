"""Verifies the autouse network guard in conftest.py actually blocks a real
connection attempt, rather than trusting that it does. Named test_zz_* so it
sorts near the end - it's a suite-level safety check, not a graphitect
feature test.
"""

import pytest

anthropic = pytest.importorskip("anthropic", reason="the bundled anthropic dependency is unavailable")


def test_guard_actually_blocks_real_network_calls():
    from graphitect.synthesize.llm_backend import AnthropicBackend

    backend = AnthropicBackend(api_key="fake-key")
    # The SDK wraps our blocked socket.connect into its own connection error
    # rather than propagating it verbatim - the meaningful assertion is that
    # no real network round-trip completes (no 401, no timeout waiting on a
    # real server), not the exact exception type the SDK chooses to wrap it in.
    with pytest.raises(anthropic.APIConnectionError):
        backend.draft("some context", diagram_kind="architecture")


@pytest.mark.allow_network
def test_allow_network_marker_lets_the_guard_be_opted_out(monkeypatch):
    # Confirms the opt-out mechanism itself works, without making a real
    # call: patch socket.socket.connect to something harmless instead of
    # relying on the autouse guard being disengaged.
    import socket

    called = {"connect": False}
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: called.__setitem__("connect", True))
    socket.socket().connect(("127.0.0.1", 80))
    assert called["connect"] is True
