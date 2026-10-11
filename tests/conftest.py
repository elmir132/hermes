"""Test setup: load main.py without the network libraries or real credentials.

Hermes is a single-file bot that connects to Telegram and Gemini when imported. The tests only
exercise its pure logic (formatting, quota routing, vector maths, memory), so the heavy libraries
are replaced with stand-ins and the settings with dummy values before the module is imported.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for name in (
    "aiogram", "aiogram.enums", "aiogram.filters", "aiogram.fsm", "aiogram.fsm.storage",
    "aiogram.fsm.storage.memory", "aiogram.types", "google", "google.generativeai",
    "httpx", "bs4", "pypdf", "duckduckgo_search", "honcho",
):
    sys.modules.setdefault(name, MagicMock(name=name))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_USER_ID", "42")


@pytest.fixture(scope="session")
def hermes():
    import main

    return main


@pytest.fixture
def memory(hermes, tmp_path, monkeypatch):
    """A fresh SQLite database and a fake embedder for each test."""
    monkeypatch.setattr(hermes, "DB_PATH", tmp_path / "hermes-test.db")

    def fake_embed(text):
        # Three numbers that depend on the words, so similar text gets similar vectors.
        t = text.lower()
        return [float(t.count("heater") + 0.01), float(t.count("deposit") + 0.01), float(t.count("football") + 0.01)]

    monkeypatch.setattr(hermes, "_embed", fake_embed)
    hermes.init_db()
    return hermes
