from __future__ import annotations

import io

import pytest
from PIL import Image

from app.store import STATE_OPEN, STATE_SIGNED


class FakeTelegramBot:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.sent: list[str] = []

    async def edit_message_media(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, chat_id, text):
        self.sent.append(text)

    async def send_photo(self, chat_id, photo, caption="", parse_mode=None):
        self.sent.append(caption)
        return None


@pytest.fixture()
def finished(app, monkeypatch):
    bot = FakeTelegramBot()
    app.bot = bot
    app.archived_caption: str | None = None

    async def upload_signed(_bot, data: bytes, caption: str) -> str:
        assert data[:2] == b"\xff\xd8"
        image = Image.open(io.BytesIO(data))
        assert image.format == "JPEG"
        app.archived_caption = caption
        return "signed-file-id"

    monkeypatch.setattr(app.storage, "upload_signed", upload_signed)
    return bot


@pytest.mark.asyncio
async def test_finalize_edits_message_and_keeps_state(app, finished):
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
    # Kept until TTL so stale buttons answer "уже подписан".
    assert contract.state == STATE_SIGNED
    assert app.store.get(contract.token) is contract


@pytest.mark.asyncio
async def test_archive_caption_has_profile_links(app, finished):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = "inline-1"
    app.store.add_signature(
        contract,
        field_key="party1",
        text="Иванов И.И.",
        user_id=11,
        user_name="Иван Иванов",
        user_username="ivan",
    )
    app.store.add_signature(
        contract,
        field_key="party2",
        text="Петров П.П.",
        user_id=22,
        user_name="Пётр Петров",
        user_username="",
    )
    template = app.config.templates.get("classic")
    assert await app.finalize(contract, template) is True

    caption = app.archived_caption or ""
    assert "https://t.me/ivan" in caption
    assert "tg://user?id=22" in caption
    assert "Иванов И.И." in caption
    assert "Сторона 1" in caption


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
    assert app.config.text("guest.self_sign_note") in finished.edits[0]["media"].caption


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


@pytest.mark.asyncio
async def test_cleanup_forgets_signed_contract_silently(app, monkeypatch):
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.state = STATE_SIGNED
    contract.updated_at -= app.store.ttl_seconds + 1

    async def fail(**kwargs):
        raise AssertionError("подписанный контракт не должен редактироваться")

    monkeypatch.setattr(app.bot, "edit_message_media", fail)
    await app.cleanup_once()
    assert app.store.get(contract.token) is None


@pytest.mark.asyncio
async def test_stale_prompt_restores_chat_view(app, monkeypatch):
    """Просроченная подпись возвращает кнопки в чат, без сообщений в личку."""
    contract = app.store.create(
        chat_key="g1", template_key="classic", initiator_id=1, initiator_name="A"
    )
    contract.inline_message_id = "inline-1"
    contract.state = STATE_OPEN
    app.store.set_pending(55, contract, "party1")
    app.store._pending[55].deadline -= app.store.pending_seconds + 1

    edits: list[dict] = []
    sent: list[tuple[int, str]] = []

    async def edit_message_media(**kwargs):
        edits.append(kwargs)

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(app.bot, "edit_message_media", edit_message_media)
    monkeypatch.setattr(app.bot, "send_message", send_message)
    monkeypatch.setattr(app.storage, "file_id", lambda key: "file-id")

    await app.cleanup_once()

    assert app.store.pending_for(55) is None
    assert edits, "сообщение в чате должно вернуться к кнопкам"
    assert "Нажми кнопку" in edits[-1]["media"].caption
    assert sent == [], "в личку ничего писать не нужно"
