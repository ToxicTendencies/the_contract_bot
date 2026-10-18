"""Full chat flow with real App methods and a fake Telegram bot.

No inline mode: the bot is summoned with a mention, replies with a photo right
in the chat, and every edit happens on that same message.
"""

from __future__ import annotations

import types

import pytest

from app.handlers import handle_callback, handle_chat_message
from app.store import STATE_OPEN, STATE_SIGNED, STATE_TERMS

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
    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.archive: list[dict] = []

    def file_id(self, key: str):
        return f"file-{key}"

    def is_ready(self, key: str) -> bool:
        return True

    async def upload_image(self, bot, data: bytes, caption: str, *, filename="blank.jpg"):
        self.uploads.append({"data": data, "caption": caption})
        return f"blank-with-terms-{len(self.uploads)}"

    async def upload_signed(self, bot, data: bytes, caption: str) -> str:
        self.archive.append({"data": data, "caption": caption})
        return "signed-file-id"


class FakeSent:
    def __init__(self, inline_message_id: str) -> None:
        self.inline_message_id = inline_message_id


class FakeGuestMessage:
    """A message with a mention (or a reply) delivered by guest mode."""

    def __init__(
        self,
        text: str,
        user: FakeUser,
        chat: FakeChat | None = None,
        *,
        query_id: str = "q1",
        reply_to: object | None = None,
    ) -> None:
        self.text = text
        self.from_user = user
        self.chat = chat or FakeChat(-100500)
        self.guest_query_id = query_id
        self.guest_bot_caller_user = user
        self.guest_bot_caller_chat = self.chat
        self.message_thread_id = None
        self.reply_to_message = reply_to
        self.answers: list[object] = []

    async def answer_guest_query(self, *, result):
        self.answers.append(result)
        return FakeSent(f"guest-{id(self)}-{len(self.answers)}")


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


def photo_result(message: FakeGuestMessage):
    """The photo the bot sent in reply to a summon."""
    assert message.answers, "бот должен ответить в чат"
    return message.answers[-1]


def buttons(result) -> list[str]:
    markup = result.reply_markup
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def press(app, data: str, user: FakeUser, inline_id: str | None = None):
    query = FakeCallback(data, user, inline_id)
    query.message = None
    await handle_callback(app, query)
    return query


def reply_to_bot(
    text: str,
    user: FakeUser,
    contract,
    *,
    query_id: str = "reply",
    chat: FakeChat | None = None,
    message_id: int | None = None,
):
    """A guest message that answers the bot's contract message."""
    reply = types.SimpleNamespace(
        from_user=FakeUser(999, first_name="Bot", is_bot=True, username="test_bot")
    )
    if message_id is not None:
        reply.message_id = message_id
    return FakeGuestMessage(text, user, chat=chat, reply_to=reply, query_id=query_id)


@pytest.mark.asyncio
async def test_full_chat_flow(wired):
    app, bot, storage = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис", username="")

    # 1. @бот -> превью-карусель прямо в чате
    summon = FakeGuestMessage("@test_bot", anna)
    await handle_chat_message(app, summon)
    preview = photo_result(summon)
    contract = app.store.for_chat("g-100500")
    assert contract is not None
    assert preview.photo_file_id == "file-classic"
    assert contract.inline_message_id.startswith("guest-")

    # 2. «Выбрать» превращает превью в контракт с кнопками сторон
    pick = await press(app, f"pk:{contract.token}:0", anna, contract.inline_message_id)
    assert pick.answers[-1] == ""
    assert contract.state == STATE_OPEN
    assert bot.edits[-1]["inline_message_id"] == contract.inline_message_id

    # 3. Анна жмёт свою сторону -> бот просит подпись в этом же чате
    side = await press(
        app, f"sg:{contract.token}:classic:party1", anna, contract.inline_message_id
    )
    assert side.answers[-1] == ""
    assert app.store.pending_for(10).field_key == "party1"
    assert "Аня" in bot.edits[-1]["media"].caption

    # 4. Анна отвечает на сообщение бота
    await handle_chat_message(app, reply_to_bot("Анна А.", anna, contract))
    assert contract.signatures["party1"].text == "Анна А."
    assert app.store.pending_for(10) is None
    assert "Осталось" in bot.edits[-1]["media"].caption

    # 5. Борис подписывает вторую сторону
    press2 = await press(
        app, f"sg:{contract.token}:classic:party2", boris, contract.inline_message_id
    )
    assert press2.answers[-1] == ""
    await handle_chat_message(app, reply_to_bot("Борис Б.", boris, contract))

    # 6. Финал: картинка с подписями, кнопок нет
    assert contract.state == STATE_SIGNED
    final = bot.edits[-1]
    assert final["inline_message_id"] == contract.inline_message_id
    assert final["media"].media == "signed-file-id"
    assert final["reply_markup"].inline_keyboard == []
    assert "Подписано" in final["media"].caption

    # 7. Архив владельцу: файл + ссылки на подписантов
    assert storage.archive
    caption = storage.archive[-1]["caption"]
    assert "https://t.me/anna" in caption
    assert "tg://user?id=20" in caption
    assert "Сторона 1" in caption and "Сторона 2" in caption
    assert storage.archive[-1]["data"][:2] == b"\xff\xd8"

    # 8. Позднее нажатие на старую кнопку не запускает новую подпись
    late = await press(
        app, f"sg:{contract.token}:classic:party1", FakeUser(30), contract.inline_message_id
    )
    assert late.answers[-1] == app.config.text("callback.done")


@pytest.mark.asyncio
async def test_any_chat_member_can_press_both_sides(wired):
    app, _, _ = wired
    summon = FakeGuestMessage("@test_bot", FakeUser(1, first_name="Автор"))
    await handle_chat_message(app, summon)
    contract = app.store.for_chat("g-100500")

    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис", username="")

    await press(app, f"sg:{contract.token}:classic:party1", anna, contract.inline_message_id)
    assert app.store.pending_for(10) is not None
    await handle_chat_message(app, reply_to_bot("Анна А.", anna, contract))

    await press(app, f"sg:{contract.token}:classic:party2", boris, contract.inline_message_id)
    assert app.store.pending_for(20) is not None
    await handle_chat_message(app, reply_to_bot("Борис Б.", boris, contract))

    assert contract.state == STATE_SIGNED
    assert contract.signatures["party1"].user_id == 10
    assert contract.signatures["party2"].user_id == 20


@pytest.mark.asyncio
async def test_buttons_survive_restart(wired, config):
    """После перезапуска бота кнопки продолжают работать."""
    app, _, _ = wired
    from app.app import App

    await handle_chat_message(app, FakeGuestMessage("@test_bot", FakeUser(1)))
    contract = app.store.for_chat("g-100500")
    assert contract is not None

    fresh, fresh_bot, _ = _wire(App(config))
    fresh.bot_username = "test_bot"
    fresh.bot_id = 999
    assert fresh.store.get(contract.token) is None

    member = FakeUser(77, first_name="Гость")
    reopened = await press(
        fresh, f"sg:{contract.token}:classic:party2", member, contract.inline_message_id
    )
    assert reopened.answers[-1] == "", reopened.answers
    restored = fresh.store.get(contract.token)
    assert restored is not None
    assert restored.template_key == "classic"
    assert fresh.store.pending_for(77) is not None
    assert fresh_bot.edits and fresh_bot.edits[-1]["inline_message_id"] == contract.inline_message_id


def _wire(app):
    bot = FakeBot()
    storage = FakeStorage()
    app.bot = bot  # type: ignore[assignment]
    app.storage = storage  # type: ignore[assignment]
    return app, bot, storage


@pytest.mark.asyncio
async def test_new_mention_does_not_kill_old_buttons(wired):
    """Второе упоминание бота в чате не ломает кнопки первого сообщения."""
    app, _, _ = wired
    chat = FakeChat(-100500)
    owner = FakeUser(1)
    contracts = []

    for index in (1, 2):
        message = FakeGuestMessage("@test_bot", owner, chat, query_id=f"q{index}")
        await handle_chat_message(app, message)
        contracts.append(app.store.for_chat("g-100500"))

    assert len(contracts) == 2
    assert contracts[0] is not contracts[1]
    first = contracts[0]
    assert app.store.get(first.token) is first

    member = FakeUser(66, first_name="Второй")
    query = await press(
        app, f"sg:{first.token}:classic:party1", member, first.inline_message_id
    )
    assert query.answers[-1] == ""
    assert app.store.pending_for(66).field_key == "party1"


# ---------------------------------------------------------------------------
# свободные условия
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_terms_flow(wired):
    app, bot, storage = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис", username="")

    # 1. @бот условия -> бот просит текст условий
    summon = FakeGuestMessage("@test_bot условия", anna)
    await handle_chat_message(app, summon)
    contract = app.store.for_chat("g-100500")
    assert contract is not None
    assert contract.template_key == "free"
    assert contract.state == STATE_TERMS
    assert contract.terms_user_id == 10
    prompt = photo_result(summon)
    assert "Ответь на это сообщение условиями" in prompt.caption

    # 2. Чужой текст не подхватывается
    stranger = FakeUser(33, first_name="Мимо")
    await handle_chat_message(app, reply_to_bot("не мои условия", stranger, contract))
    assert contract.terms == ""

    # 3. Автор присылает условия -> бланк перерисовывается с текстом
    terms = "Исполнитель делает макет, Заказчик платит 1000 ₽ до пятницы."
    await handle_chat_message(app, reply_to_bot(terms, anna, contract))
    assert contract.terms == terms
    assert contract.state == STATE_OPEN
    assert storage.uploads, "бланк с условиями должен быть загружен"
    assert storage.uploads[-1]["data"][:2] == b"\xff\xd8"
    assert contract.blank_file_id == "blank-with-terms-1"
    assert bot.edits[-1]["media"].media == "blank-with-terms-1"
    assert bot.edits[-1]["reply_markup"] is not None

    # 4. Подписи собираются как обычно
    await press(app, f"sg:{contract.token}:free:party1", anna, contract.inline_message_id)
    await handle_chat_message(app, reply_to_bot("Анна А.", anna, contract))
    await press(app, f"sg:{contract.token}:free:party2", boris, contract.inline_message_id)
    await handle_chat_message(app, reply_to_bot("Борис Б.", boris, contract))

    assert contract.state == STATE_SIGNED
    final = bot.edits[-1]
    assert final["media"].media == "signed-file-id"
    assert final["reply_markup"].inline_keyboard == []
    # Условия попадают и в архивную подпись
    assert "Условия:" in storage.archive[-1]["caption"]
    assert "1000" in storage.archive[-1]["caption"]


@pytest.mark.asyncio
async def test_terms_reject_too_long_text(wired):
    app, _, _ = wired
    anna = FakeUser(10, first_name="Аня")
    await handle_chat_message(app, FakeGuestMessage("@test_bot условия", anna))
    contract = app.store.for_chat("g-100500")

    too_long = "а" * 900
    reply = reply_to_bot(too_long, anna, contract)
    await handle_chat_message(app, reply)
    assert contract.terms == ""
    assert contract.state == STATE_TERMS
    assert reply.answers, "должна быть подсказка про длину"
    assert "Сократи" in reply.answers[-1].input_message_content.message_text


@pytest.mark.asyncio
async def test_terms_can_be_chosen_from_carousel(wired):
    app, bot, _ = wired
    anna = FakeUser(10, first_name="Аня")
    summon = FakeGuestMessage("@test_bot", anna)
    await handle_chat_message(app, summon)
    contract = app.store.for_chat("g-100500")

    # «Свободный договор» — пятый бланк (индекс 4)
    pick = await press(app, f"pk:{contract.token}:4", anna, contract.inline_message_id)
    assert pick.answers[-1] == ""
    assert contract.template_key == "free"
    assert contract.state == STATE_TERMS
    assert "Ответь на это сообщение условиями" in bot.edits[-1]["media"].caption


# ---------------------------------------------------------------------------
# бот добавлен в чат (member mode) — всё так же в чате
# ---------------------------------------------------------------------------


class FakeChatMessage:
    """Обычное сообщение в группе, где бот состоит участником."""

    def __init__(
        self,
        text: str,
        user: FakeUser,
        chat: FakeChat | None = None,
        *,
        reply_to: object | None = None,
        message_id_base: int = 900,
    ) -> None:
        self.text = text
        self.from_user = user
        self.chat = chat or FakeChat(-100777)
        self.message_id_base = message_id_base
        self.guest_query_id = None
        self.guest_bot_caller_user = None
        self.guest_bot_caller_chat = None
        self.message_thread_id = None
        self.reply_to_message = reply_to
        self.replies: list[str] = []
        self.photos: list[dict] = []

    async def answer_photo(self, photo, caption="", parse_mode=None, reply_markup=None):
        message = types.SimpleNamespace(
            chat=self.chat, message_id=self.message_id_base + len(self.photos)
        )
        self.photos.append(
            {
                "photo": photo,
                "caption": caption,
                "reply_markup": reply_markup,
                "message_id": message.message_id,
            }
        )
        return message

    async def reply(self, text, parse_mode=None, **_: object):
        self.replies.append(text)
        return None


@pytest.mark.asyncio
async def test_member_mode_keeps_everything_in_chat(wired):
    app, bot, storage = wired
    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис", username="")

    summon = FakeChatMessage("@test_bot", anna, chat)
    await handle_chat_message(app, summon)

    contract = app.store.for_chat("g-100777")
    assert contract is not None
    assert summon.photos, "бот должен прислать бланк обычным сообщением"
    assert contract.chat_id == -100777
    assert contract.message_id == 900
    assert summon.photos[0]["photo"] == "file-classic"

    # Выбор бланка и подпись — всё по message_id, без inline_message_id
    pick = await press(app, f"pk:{contract.token}:0", anna)
    assert pick.answers[-1] == ""
    assert bot.edits[-1]["chat_id"] == -100777
    assert bot.edits[-1]["message_id"] == 900

    side = await press(app, f"sg:{contract.token}:classic:party1", anna)
    assert side.answers[-1] == ""
    assert app.store.pending_for(10) is not None

    bot_user = FakeUser(999, first_name="Bot", is_bot=True, username="test_bot")
    await handle_chat_message(
        app, FakeChatMessage("Анна А.", anna, chat, reply_to=types.SimpleNamespace(from_user=bot_user))
    )
    assert contract.signatures["party1"].text == "Анна А."

    await press(app, f"sg:{contract.token}:classic:party2", boris)
    await handle_chat_message(
        app, FakeChatMessage("Борис Б.", boris, chat, reply_to=types.SimpleNamespace(from_user=bot_user))
    )

    assert contract.state == STATE_SIGNED
    final = bot.edits[-1]
    assert final["chat_id"] == -100777
    assert final["media"].media == "signed-file-id"
    assert final["reply_markup"].inline_keyboard == []
    assert storage.archive


@pytest.mark.asyncio
async def test_member_mode_ignores_chatter_without_reply(wired):
    """В чате-участнике обычная болтовня не превращается в подпись."""
    app, _, _ = wired
    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня")

    await handle_chat_message(app, FakeChatMessage("@test_bot", anna, chat))
    contract = app.store.for_chat("g-100777")
    await press(app, f"sg:{contract.token}:classic:party1", anna)
    assert app.store.pending_for(10) is not None

    chatter = FakeChatMessage("просто болтаю в чате", anna, chat)
    await handle_chat_message(app, chatter)
    assert contract.signatures == {}
    assert app.store.pending_for(10) is not None
    assert chatter.replies == []


@pytest.mark.asyncio
async def test_member_mode_terms_flow(wired):
    app, bot, storage = wired
    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня", username="anna")
    bot_user = FakeUser(999, first_name="Bot", is_bot=True, username="test_bot")

    summon = FakeChatMessage("@test_bot условия", anna, chat)
    await handle_chat_message(app, summon)
    contract = app.store.for_chat("g-100777")
    assert contract is not None and contract.state == STATE_TERMS
    assert "Ответь на это сообщение условиями" in summon.photos[0]["caption"]

    await handle_chat_message(
        app,
        FakeChatMessage(
            "Оплата 500 ₽ после сдачи.",
            anna,
            chat,
            reply_to=types.SimpleNamespace(from_user=bot_user),
        ),
    )
    assert contract.terms == "Оплата 500 ₽ после сдачи."
    assert contract.state == STATE_OPEN
    assert storage.uploads
    assert bot.edits[-1]["chat_id"] == -100777
    assert bot.edits[-1]["media"].media == "blank-with-terms-1"


@pytest.mark.asyncio
async def test_member_mode_buttons_survive_restart(wired, config):
    """В режиме участника кнопки тоже восстанавливаются после перезапуска."""
    app, _, _ = wired
    from app.app import App

    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня")
    await handle_chat_message(app, FakeChatMessage("@test_bot", anna, chat))
    contract = app.store.for_chat("g-100777")
    assert contract is not None and contract.message_id

    fresh, fresh_bot, _ = _wire(App(config))
    fresh.bot_username = "test_bot"
    fresh.bot_id = 999

    # Callback на обычное сообщение в чате: message присутствует, inline нет
    query = FakeCallback(f"sg:{contract.token}:classic:party1", FakeUser(55))
    query.inline_message_id = None
    query.message = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=-100777), message_id=contract.message_id
    )
    await handle_callback(fresh, query)

    assert query.answers[-1] == "", query.answers
    restored = fresh.store.get(contract.token)
    assert restored is not None
    assert restored.chat_id == -100777
    assert restored.message_id == contract.message_id
    assert fresh_bot.edits[-1]["chat_id"] == -100777


# ---------------------------------------------------------------------------
# несколько чатов и несколько бланков одновременно
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_user_signs_in_two_chats(wired):
    """Подписи в двух чатах не перетирают друг друга."""
    app, _, _ = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    chat_a, chat_b = FakeChat(-100500), FakeChat(-100777)

    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat_a))
    first = app.store.for_chat("g-100500")
    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat_b))
    second = app.store.for_chat("g-100777")
    assert first is not None and second is not None

    await press(app, f"sg:{first.token}:classic:party1", anna, first.inline_message_id)
    await press(app, f"sg:{second.token}:classic:party1", anna, second.inline_message_id)

    # Нажатие во втором чате не должно забирать ожидание из первого
    assert app.store.pending_for(10, "g-100500").token == first.token
    assert app.store.pending_for(10, "g-100777").token == second.token

    await handle_chat_message(
        app, reply_to_bot("Анна в A", anna, first, chat=chat_a)
    )
    await handle_chat_message(
        app, reply_to_bot("Анна в B", anna, second, chat=chat_b)
    )

    assert first.signatures["party1"].text == "Анна в A"
    assert second.signatures["party1"].text == "Анна в B"


@pytest.mark.asyncio
async def test_reply_in_other_chat_does_not_sign(wired):
    """Ответ не в том чате не подписывает контракт и честно об этом говорит."""
    app, _, _ = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    chat_a, chat_b = FakeChat(-100500), FakeChat(-100777)

    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat_a))
    first = app.store.for_chat("g-100500")
    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat_b))
    second = app.store.for_chat("g-100777")
    await press(app, f"sg:{first.token}:classic:party1", anna, first.inline_message_id)

    reply = reply_to_bot("Анна А.", anna, second, chat=chat_b)
    await handle_chat_message(app, reply)

    assert first.signatures == {}
    assert second.signatures == {}
    assert "другом чате" in reply.answers[-1].input_message_content.message_text

    # Ожидание не потерялось: ответ в правильном чате по-прежнему подписывает
    assert app.store.pending_for(10, "g-100500").token == first.token
    await handle_chat_message(
        app, reply_to_bot("Анна А.", anna, first, chat=chat_a)
    )
    assert first.signatures["party1"].text == "Анна А."


@pytest.mark.asyncio
async def test_older_contract_keeps_its_signature(wired):
    """Второй бланк в том же чате не крадёт подпись первого."""
    app, _, _ = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    chat = FakeChat(-100500)

    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat))
    first = app.store.for_chat("g-100500")
    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna, chat))
    second = app.store.for_chat("g-100500")
    assert first.token != second.token

    await press(app, f"sg:{first.token}:classic:party1", anna, first.inline_message_id)
    assert first.signatures == {}
    await handle_chat_message(app, reply_to_bot("Анна А.", anna, first, chat=chat))

    assert first.signatures["party1"].text == "Анна А."
    assert second.signatures == {}


@pytest.mark.asyncio
async def test_second_signer_sees_signed_side(wired):
    """После подписи первого участника чужая кнопка не «сбрасывает» бланк."""
    app, bot, _ = wired
    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис")

    await handle_chat_message(app, FakeGuestMessage("@test_bot", anna))
    contract = app.store.for_chat("g-100500")
    await press(app, f"pk:{contract.token}:0", anna, contract.inline_message_id)
    await press(app, f"sg:{contract.token}:classic:party1", anna, contract.inline_message_id)
    await handle_chat_message(app, reply_to_bot("Анна А.", anna, contract))
    assert contract.signatures["party1"].text == "Анна А."

    await press(app, f"sg:{contract.token}:classic:party2", boris, contract.inline_message_id)
    markup = bot.edits[-1]["reply_markup"].inline_keyboard
    keys = [button.callback_data for row in markup for button in row]
    texts = [button.text for row in markup for button in row]

    assert f"no:{contract.token}" in keys
    assert f"sg:{contract.token}:classic:party2" in keys
    assert "✅ Сторона 1" in texts
    assert "✍️ Сторона 1" not in texts


@pytest.mark.asyncio
async def test_member_mode_reply_target_wins(wired):
    """В режиме участника ответ на конкретный бланк важнее «самого нового»."""
    app, _, _ = wired
    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня", username="anna")
    bot_user = FakeUser(999, first_name="Bot", is_bot=True, username="test_bot")

    await handle_chat_message(app, FakeChatMessage("@test_bot", anna, chat))
    first = app.store.for_chat("g-100777")
    await handle_chat_message(
        app, FakeChatMessage("@test_bot условия", anna, chat, message_id_base=901)
    )
    second = app.store.for_chat("g-100777")
    assert first.token != second.token and second.message_id == 901

    # Ждём подпись в первом контракте, а условия шлём во второй.
    await press(app, f"sg:{first.token}:classic:party1", anna)
    first_reply = FakeChatMessage("Анна А.", anna, chat, reply_to=types.SimpleNamespace(
        from_user=bot_user, message_id=first.message_id
    ))
    terms_reply = FakeChatMessage(
        "Оплата 500 ₽ после сдачи.",
        anna,
        chat,
        reply_to=types.SimpleNamespace(from_user=bot_user, message_id=second.message_id),
    )

    await handle_chat_message(app, terms_reply)
    assert second.terms == "Оплата 500 ₽ после сдачи."
    assert first.signatures == {}
    assert app.store.pending_for(10, "g-100777").token == first.token

    await handle_chat_message(app, first_reply)
    assert first.signatures["party1"].text == "Анна А."


@pytest.mark.asyncio
async def test_member_mode_free_contract_both_sides(wired):
    """Свободный бланк в чате-участнике: условия, две подписи, финал."""
    app, bot, storage = wired
    chat = FakeChat(-100777)
    anna = FakeUser(10, first_name="Аня", username="anna")
    boris = FakeUser(20, first_name="Борис")
    bot_user = FakeUser(999, first_name="Bot", is_bot=True, username="test_bot")

    await handle_chat_message(app, FakeChatMessage("@test_bot условия", anna, chat))
    contract = app.store.for_chat("g-100777")
    terms = "Исполнитель делает макет, Заказчик платит 1000 ₽ до пятницы."
    await handle_chat_message(
        app,
        FakeChatMessage(
            terms,
            anna,
            chat,
            reply_to=types.SimpleNamespace(from_user=bot_user, message_id=contract.message_id),
        ),
    )
    assert contract.terms == terms
    assert contract.state == STATE_OPEN

    await press(app, f"sg:{contract.token}:free:party1", anna)
    await handle_chat_message(
        app,
        FakeChatMessage(
            "Анна А.",
            anna,
            chat,
            reply_to=types.SimpleNamespace(from_user=bot_user, message_id=contract.message_id),
        ),
    )
    assert contract.signatures["party1"].text == "Анна А."

    await press(app, f"sg:{contract.token}:free:party2", boris)
    await handle_chat_message(
        app,
        FakeChatMessage(
            "Борис Б.",
            boris,
            chat,
            reply_to=types.SimpleNamespace(from_user=bot_user, message_id=contract.message_id),
        ),
    )

    assert contract.state == STATE_SIGNED
    assert {key: sig.text for key, sig in contract.signatures.items()} == {
        "party1": "Анна А.",
        "party2": "Борис Б.",
    }
    assert bot.edits[-1]["media"].media == "signed-file-id"
    assert bot.edits[-1]["reply_markup"].inline_keyboard == []
    assert "Условия:" in storage.archive[-1]["caption"]
