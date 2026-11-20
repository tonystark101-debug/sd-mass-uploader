"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_internet_lines(monkeypatch):
    """Tests never probe this computer's real network: the line pool is off unless a test
    builds its own with stand-in discovery (tests/unit/test_network.py)."""
    monkeypatch.setattr("src.network.discovery.supported", lambda: False)
