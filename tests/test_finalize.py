from __future__ import annotations

import io

import pytest
from PIL import Image

from app.store import STATE_SIGNED

from .helpers import FakeChat, FakeMessage, FakeUser


class FakeTelegramBot:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.sent: list[str] = []

    async def edit_message_media(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, chat_id, text):
        self.sent.append(text)
        return None


@pytest.fixture()
def finished(app, monkeypatch):
    bot = FakeTelegramBot()
    app.bot = bot

    async def upload_signed(_bot, data: bytes, title: str) -> str:
        assert data[:2] == b"\xff\xd8"
        image = Image.open(io.BytesIO(data))
        assert image.format == "JPEG"
        app.last_rendered = image
        return "signed-file-id"

    monkeypatch.setattr(app.storage, "upload_signed", upload_signed)
    return bot


@pytest.mark.asyncio
async def test_finalize_edits_message_and_drops_state(app, finished):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = "inline-1"
    app.store.add_signature(contract, field_key="party1", text="A", user_id=1, user_name="A")
    app.store.add_signature(contract, field_key="party2", text="B", user_id=2, user_name="B")

    template = app.config.templates.get("classic")
    assert await app.finalize(contract, template) is True
    assert len(finished.edits) == 1
    edit = finished.edits[0]
    assert edit["inline_message_id"] == "inline-1"
    assert edit["media"].media == "signed-file-id"
    assert "Подписано" in edit["media"].caption
    assert app.store.get(contract.token) is None


@pytest.mark.asyncio
async def test_finalize_marks_self_signed(app, finished):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = "inline-1"
    app.store.add_signature(contract, field_key="party1", text="A", user_id=1, user_name="A")
    app.store.add_signature(contract, field_key="party2", text="A", user_id=1, user_name="A")
    template = app.config.templates.get("classic")
    assert await app.finalize(contract, template) is True
    caption = finished.edits[0]["media"].caption
    assert app.config.text("guest.self_sign_note") in caption
    assert contract.state == STATE_SIGNED


@pytest.mark.asyncio
async def test_finalize_without_message_still_ok(app, finished):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = None
    app.store.add_signature(contract, field_key="party1", text="A", user_id=1, user_name="A")
    app.store.add_signature(contract, field_key="party2", text="B", user_id=2, user_name="B")
    template = app.config.templates.get("classic")
    assert await app.finalize(contract, template) is True
    assert finished.edits == []
    assert app.store.get(contract.token) is None


@pytest.mark.asyncio
async def test_cleanup_expires_contract(app, monkeypatch):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = "inline-1"
    contract.updated_at -= app.store.ttl_seconds + 1

    edits: list[dict] = []

    async def edit_message_media(**kwargs):
        edits.append(kwargs)

    app.bot.edit_message_media = edit_message_media
    monkeypatch.setattr(app.storage, "file_id", lambda key: "file-id")
    await app.cleanup_once()
    assert app.store.get(contract.token) is None
    assert edits and "истёк" in edits[0]["media"].caption.lower()
