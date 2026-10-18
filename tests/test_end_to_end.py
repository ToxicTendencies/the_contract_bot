"""Full inline flow with real App methods and a fake Telegram bot."""

from __future__ import annotations

import types

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


# ---------------------------------------------------------------------------
# регрессии: кнопки должны работать у всех участников и переживать рестарт
# ---------------------------------------------------------------------------


class FakeGuestMessage:
    """Сообщение-упоминание в guest-режиме."""

    def __init__(self, text: str, user: FakeUser, chat: FakeChat | None = None):
        self.text = text
        self.from_user = user
        self.chat = chat or FakeChat(-100500)
        self.guest_query_id = f"guest-{user.id}"
        self.guest_bot_caller_user = user
        self.guest_bot_caller_chat = self.chat
        self.message_thread_id = None
        self.reply_to_message = None


class FakeSent:
    def __init__(self, inline_message_id: str):
        self.inline_message_id = inline_message_id


def wire(app):
    bot = FakeBot()
    storage = FakeStorage()
    app.bot = bot  # type: ignore[assignment]
    app.storage = storage  # type: ignore[assignment]
    return app, bot, storage


async def press(app, data: str, user: FakeUser, inline_id: str | None = None):
    query = FakeCallback(data, user, inline_id) if inline_id else FakeCallback(data, user)
    query.message = None
    await handle_callback(app, query)
    return query


@pytest.mark.asyncio
async def test_any_chat_member_can_press_both_sides(wired):
    """Не только владелец: любые два участника чата подписывают контракт."""
    app, _, _ = wired

    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    result = query.answers[0]["results"][0]
    token = result.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1]
    await handle_chosen_inline_result(app, FakeChosen(result.id, "inline-shared"))

    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис", username="")

    first = await press(app, f"isg:{token}:classic:party1", anna, "inline-shared")
    assert first.urls[-1] == f"https://t.me/test_bot?start=s_{token}_party1"

    await handle_dm(app, FakeDM(f"/start s_{token}_party1", anna))
    await handle_dm(app, FakeDM("Анна А.", anna))

    second = await press(app, f"isg:{token}:classic:party2", boris, "inline-shared")
    assert second.urls[-1] == f"https://t.me/test_bot?start=s_{token}_party2"
    await handle_dm(app, FakeDM(f"/start s_{token}_party2", boris))
    await handle_dm(app, FakeDM("Борис Б.", boris))

    contract = app.store.for_inline("inline-shared")
    assert contract.state == STATE_SIGNED
    assert contract.signatures["party1"].user_id == 10
    assert contract.signatures["party2"].user_id == 20


@pytest.mark.asyncio
async def test_buttons_survive_restart(wired, config):
    """После перезапуска бота кнопки продолжают работать."""
    app, bot, _ = wired
    from app.app import App

    query = FakeInlineQuery("рыцарь")
    await handle_inline_query(app, query)
    result = query.answers[0]["results"][0]
    token = result.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1]
    await handle_chosen_inline_result(app, FakeChosen(result.id, "inline-restart"))

    # «перезапуск»: новый App, пустое хранилище
    fresh = App(config)
    fresh.bot_username = "test_bot"
    fresh.bot_id = 999
    fresh, fresh_bot, _ = wire(fresh)
    assert fresh.store.get(token) is None

    user = FakeUser(77, first_name="Гость")
    reopened = await press(fresh, f"isg:{token}:medieval:party2", user, "inline-restart")
    assert reopened.urls[-1] == f"https://t.me/test_bot?start=s_{token}_party2"

    contract = fresh.store.for_inline("inline-restart")
    assert contract is not None
    assert contract.template_key == "medieval"

    await handle_dm(fresh, FakeDM(f"/start s_{token}_party2", user))
    await handle_dm(fresh, FakeDM("Гость Г.", user))
    assert contract.signatures["party2"].text == "Гость Г."
    assert fresh_bot.edits and fresh_bot.edits[-1]["inline_message_id"] == "inline-restart"


@pytest.mark.asyncio
async def test_legacy_guest_buttons_keep_working(wired):
    """Кнопки, отправленные до обновления (без ключа шаблона), тоже живые."""
    app, bot, _ = wired

    async def answer_guest_query(*, result):
        return FakeSent("guest-inline-1")

    app.post_guest_text = _noop  # type: ignore[assignment]
    original = FakeGuestMessage("@test_bot", FakeUser(1))
    original.answer_guest_query = answer_guest_query  # type: ignore[attr-defined]

    from app.handlers import handle_guest

    await handle_guest(app, original)
    contract = app.store.for_chat("g-100500")
    assert contract is not None

    # старый формат callback_data: без ключа шаблона
    member = FakeUser(55, first_name="Член")
    query = FakeCallback(f"sg:{contract.token}:party1", member, "guest-inline-1")
    await handle_callback(app, query)
    assert query.answers[-1] == ""
    assert app.store.pending_for(55) is not None
    assert app.store.pending_for(55).field_key == "party1"


@pytest.mark.asyncio
async def test_new_mention_does_not_kill_old_buttons(wired):
    """Второе упоминание бота в чате не ломает кнопки первого сообщения."""
    app, bot, _ = wired
    from app.handlers import handle_guest

    counter = {"n": 0}

    async def answer_guest_query(*, result):
        counter["n"] += 1
        return FakeSent(f"guest-inline-{counter['n']}")

    chat = FakeChat(-100500)
    owner = FakeUser(1)
    contracts = []

    for index in (1, 2):
        message = FakeGuestMessage("@test_bot", owner, chat)
        message.guest_query_id = f"guest-{index}"
        message.answer_guest_query = answer_guest_query  # type: ignore[attr-defined]
        await handle_guest(app, message)
        contracts.append(app.store.for_chat("g-100500"))

    assert len(contracts) == 2
    assert contracts[0] is not contracts[1]
    first = contracts[0]
    assert app.store.get(first.token) is first

    member = FakeUser(66, first_name="Второй")
    query = FakeCallback(f"sg:{first.token}:classic:party1", member, first.inline_message_id)
    await handle_callback(app, query)
    assert query.answers[-1] == "", query.answers
    assert app.store.pending_for(66).field_key == "party1"


async def _noop(*_args, **_kwargs) -> None:
    return None


class ChatCallback(FakeCallback):
    """Нажатие на обычное сообщение бота в чате (без inline_message_id)."""

    def __init__(self, data: str, user: FakeUser, chat_id: int = -100500, message_id: int = 500):
        super().__init__(data, user, None)
        self.message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=chat_id), message_id=message_id
        )


@pytest.mark.asyncio
async def test_guest_button_recovers_after_restart(wired, config):
    """Кнопки guest-сообщения в чате тоже переживают перезапуск."""
    from app.app import App

    app, _, _ = wired
    token = "abc12345"

    fresh, bot, _ = wire(App(config))
    fresh.bot_username = "test_bot"

    query = ChatCallback(f"sg:{token}:classic:party2", FakeUser(88, first_name="Член"))
    await handle_callback(fresh, query)

    assert query.answers[-1] == "", query.answers
    contract = fresh.store.get(token)
    assert contract is not None
    assert contract.chat_key == "g-100500"
    assert fresh.store.pending_for(88) is not None
    assert bot.edits and bot.edits[-1]["chat_id"] == -100500
    assert bot.edits[-1]["message_id"] == 500
