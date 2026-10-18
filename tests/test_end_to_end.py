"""Full inline flow with real App methods and a fake Telegram bot."""

from __future__ import annotations

import pytest

from app.handlers import handle_callback, handle_chosen_inline_result, handle_dm, handle_inline_query
from app.store import STATE_SIGNED

from .helpers import FakeCallback, FakeChat, FakeUser


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.photos: list[dict] = []
        self.messages: list[dict] = []

    async def edit_message_media(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return None

    async def send_message(self, chat_id, text):
        self.messages.append({"chat_id": chat_id, "text": text})
        return None


class FakeStorage:
    def file_id(self, key: str):
        return f"file-{key}"

    def is_ready(self, key: str) -> bool:
        return True

    async def upload_signed(self, bot, data: bytes, caption: str) -> str:
        self.caption = caption
        self.bytes = data
        return "signed-file-id"


class FakeInlineQuery:
    def __init__(self, text: str = ""):
        self.query = text
        self.from_user = FakeUser(1, first_name="Аня")
        self.answers: list[dict] = []

    async def answer(self, results, **kwargs):
        self.answers.append({"results": results, **kwargs})


class FakeChosen:
    def __init__(self, result_id: str, inline_message_id: str):
        self.result_id = result_id
        self.inline_message_id = inline_message_id
        self.from_user = FakeUser(1, first_name="Аня")


class FakeDM:
    def __init__(self, text: str, user: FakeUser):
        self.text = text
        self.from_user = user
        self.chat = FakeChat(1, type="private")
        self.answers: list[str] = []

    async def answer(self, text: str, reply_markup=None, **_: object):
        self.answers.append(text)
        return None


@pytest.fixture()
def wired(app):
    bot = FakeBot()
    storage = FakeStorage()
    app.bot = bot  # type: ignore[assignment]
    app.storage = storage  # type: ignore[assignment]
    return app, bot, storage


@pytest.mark.asyncio
async def test_full_inline_flow(wired):
    app, bot, storage = wired

    # 1. empty query -> carousel with every blank
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    results = query.answers[0]["results"]
    assert len(results) == 4

    # 2. medieval by keyword -> one result
    medieval_query = FakeInlineQuery("рыцарь")
    await handle_inline_query(app, medieval_query)
    medieval_results = medieval_query.answers[0]["results"]
    assert len(medieval_results) == 1

    result = medieval_results[0]
    field_buttons = result.reply_markup.inline_keyboard[0]
    token = field_buttons[0].callback_data.split(":")[1]

    # 3. user sends it into a chat
    await handle_chosen_inline_result(app, FakeChosen(result.id, "inline-medieval"))
    contract = app.store.for_inline("inline-medieval")
    assert contract is not None
    assert contract.template_key == "medieval"

    # 4. first signer presses the button -> deep link to DM
    press = FakeCallback(
        f"isg:{token}:medieval:party1", FakeUser(10, first_name="Иван"), "inline-medieval"
    )
    await handle_callback(app, press)
    link = press.urls[-1]
    assert link.startswith("https://t.me/")

    # 5. DM deep link sets pending
    first = FakeUser(10, first_name="Иван", username="ivan")
    start = FakeDM(f"/start {link.split('?start=')[1]}", first)
    await handle_dm(app, start)
    assert app.store.pending_for(10) is not None

    # 6. signature text
    await handle_dm(app, FakeDM("Иванов И.И.", first))
    assert contract.signatures["party1"].text == "Иванов И.И."
    assert app.store.pending_for(10) is None
    assert bot.edits, "после первой подписи сообщение должно обновиться"
    assert "Осталось" in bot.edits[-1]["media"].caption

    # 7. second signer
    press2 = FakeCallback(
        f"isg:{token}:medieval:party2", FakeUser(20, first_name="Пётр"), "inline-medieval"
    )
    await handle_callback(app, press2)
    second = FakeUser(20, first_name="Пётр", username="")
    await handle_dm(app, FakeDM(f"/start {press2.urls[-1].split('?start=')[1]}", second))
    await handle_dm(app, FakeDM("Петров П.П.", second))

    # 8. finished
    assert contract.state == STATE_SIGNED
    final = bot.edits[-1]
    assert final["inline_message_id"] == "inline-medieval"
    assert final["media"].media == "signed-file-id"
    assert final["reply_markup"].inline_keyboard == []
    assert "Подписано" in final["media"].caption

    # 9. archive caption in the owner's chat has both links
    assert "https://t.me/ivan" in storage.caption
    assert "tg://user?id=20" in storage.caption
    assert "Сторона 1" in storage.caption and "Сторона 2" in storage.caption
    assert storage.bytes[:2] == b"\xff\xd8"

    # 10. stale button no longer starts a new signing
    late = FakeCallback(
        f"isg:{token}:medieval:party1", FakeUser(30), "inline-medieval"
    )
    await handle_callback(app, late)
    assert late.answers[-1] == app.config.text("callback.done")
