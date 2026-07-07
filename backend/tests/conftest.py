"""Test env: everything here must land before any app module is imported.

No real API keys and no network — model-calling functions are stubbed in
the e2e tests; unit tests exercise pure logic only.
"""

import os

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("ROUNDTABLE_PANEL", "test/alpha|Alpha,test/beta|Beta")
os.environ.setdefault("NEWS_PANEL",
                      "test/alpha|Alpha,test/beta|Beta,test/gamma|Gamma")
# Neutralize the rate limiter for in-process request tests.
os.environ.setdefault("RATE_LIMIT_MIN_INTERVAL", "0")
os.environ.setdefault("RATE_LIMIT_PER_IP_WINDOW_MAX", "1000")
os.environ.setdefault("RATE_LIMIT_GLOBAL_WINDOW_MAX", "1000")

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the store at a fresh SQLite file and initialize the schema."""
    from app import store

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(store, "DB_PATH", db_path)
    return store
