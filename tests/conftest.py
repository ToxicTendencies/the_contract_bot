from __future__ import annotations

import os
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def env() -> None:
    os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
    os.environ.setdefault("OWNER_ID", "42")


@pytest.fixture()
def config():
    from app.config import ConfigManager

    return ConfigManager()


@pytest.fixture()
def app(config):
    from app.app import App

    application = App(config)
    application.bot_username = "test_bot"
    application.bot_id = 999
    application.guest_mode_enabled = True
    return application
