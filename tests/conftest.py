"""Test configuration."""

from __future__ import annotations

import sys

import pytest
import pytest_socket

if sys.platform == "win32":
    # The Windows event loop needs a socketpair, which the HA test plugin
    # blocks; the integration itself only talks to 127.0.0.1 in tests.
    pytest_socket.disable_socket = lambda *args, **kwargs: None
    pytest_socket.socket_allow_hosts = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the integration from custom_components."""
    return
