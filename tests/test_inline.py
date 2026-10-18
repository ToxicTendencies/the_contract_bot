from __future__ import annotations

import pytest

from app.handlers import (
    DEEPLINK_PREFIX,
    handle_callback,
    handle_chosen_inline_result,
    handle_dm,
    handle_inline_query,
)
from app.store import STATE_SIGNED

from .helpers import FakeCallback, FakeChat, FakeUser


class FakeInlineQuery:
    def __init__(self, text: str = "", user: FakeUser | None = None):
        self.query = text
        self.from_user = user or FakeUser(1)
        self.id = "inline-query-id"
        self.answers: list[dict] = []

    async def answer(self, results, **kwargs):
        self.answers.append({"results": results, **kwargs})


class ReadyStorage:
    def file_id(self, key: str):
        return f"file-{key}"

    def is_ready(self, key: str) -> bool:
        return True


@pytest.fixture()
def inline_app(app, monkeypatch):
    app.storage = ReadyStorage()  # type: ignore[assignment]
    calls = {"text": [], "progress": [], "final": [], "prompt": []}

    async def post_text(message, text):
        calls["text"].append(text)

    async def show_progress(contract, template, field_key):
        calls["progress"].append(field_key)
        return True

    async def finalize(contract, template):
        calls["final"].append(contract.token)
        contract.state = STATE_SIGNED
        return True

    monkeypatch.setattr(app, "post_guest_text", post_text)
    monkeypatch.setattr(app, "show_signed_progress", show_progress)
    monkeypatch.setattr(app, "finalize", finalize)
    return calls


# ---------------------------------------------------------------------------
# inline queries: carousel + keyword search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_empty_query_returns_all(inline_app, app):
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    answer = query.answers[0]
    assert answer["cache_time"] == 0
    assert answer["is_personal"] is True
    assert len(answer["results"]) == len(app.templates())


@pytest.mark.asyncio
async def test_inline_keyword_filters(inline_app, app):
    query = FakeInlineQuery("средневековый")
    await handle_inline_query(app, query)
    results = query.answers[0]["results"]
    assert len(results) == 1
    assert results[0].title == "Средневековый контракт"


@pytest.mark.asyncio
async def test_inline_keyword_alias(inline_app, app):
    query = FakeInlineQuery("рыцарь")
    await handle_inline_query(app, query)
    results = query.answers[0]["results"]
    assert [result.title for result in results] == ["Средневековый контракт"]


@pytest.mark.asyncio
async def test_inline_multiword(inline_app, app):
    query = FakeInlineQuery("ночной контракт")
    await handle_inline_query(app, query)
    results = query.answers[0]["results"]
    assert len(results) == 1
    assert results[0].title == "Ночной контракт"


@pytest.mark.asyncio
async def test_inline_unknown_query_returns_article(inline_app, app):
    query = FakeInlineQuery("ерунда")
    await handle_inline_query(app, query)
    results = query.answers[0]["results"]
    assert len(results) == 1
    assert results[0].type == "article"


@pytest.mark.asyncio
async def test_inline_results_carry_sign_buttons(inline_app, app):
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    result = query.answers[0]["results"][0]
    buttons = result.reply_markup.inline_keyboard[0]
    assert buttons[0].callback_data.startswith("isg:")
    assert len(buttons[0].callback_data.encode()) <= 64
    assert "Нажми свою сторону" in result.caption


# ---------------------------------------------------------------------------
# chosen result binds the message
# ---------------------------------------------------------------------------


class FakeChosen:
    def __init__(self, result_id: str, inline_message_id: str = "inline-42", user=None):
        self.result_id = result_id
        self.inline_message_id = inline_message_id
        self.from_user = user or FakeUser(1)


@pytest.mark.asyncio
async def test_chosen_result_creates_contract(inline_app, app):
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    chosen_id = query.answers[0]["results"][0].id
    await handle_chosen_inline_result(app, FakeChosen(chosen_id))
    contract = app.store.for_inline("inline-42")
    assert contract is not None
    assert contract.template_key == "classic"


@pytest.mark.asyncio
async def test_chosen_result_without_feedback_is_ignored(inline_app, app):
    await handle_chosen_inline_result(app, FakeChosen("token-classic", inline_message_id=""))
    assert app.store.active_count == 0


# ---------------------------------------------------------------------------
# button press -> deep link to DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_button_answers_with_deeplink(inline_app, app):
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    result = query.answers[0]["results"][0]
    token = result.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1]

    callback = FakeCallback(
        data=f"isg:{token}:classic:party1",
        user=FakeUser(7),
        inline_message_id="inline-42",
    )
    await handle_callback(app, callback)

    assert callback.answers == [app.config.text("inline.opening_dm")]
    assert callback.urls
    url = callback.urls[-1]
    assert url.startswith(f"https://t.me/{app.bot_username}?start={DEEPLINK_PREFIX}")
    contract = app.store.for_inline("inline-42")
    assert contract is not None
    assert contract.token == token


@pytest.mark.asyncio
async def test_same_result_in_two_chats_gets_two_contracts(inline_app, app):
    query = FakeInlineQuery("")
    await handle_inline_query(app, query)
    token = (
        query.answers[0]["results"][0]
        .reply_markup.inline_keyboard[0][0]
        .callback_data.split(":")[1]
    )

    first = FakeCallback(f"isg:{token}:classic:party1", FakeUser(1), inline_message_id="A")
    second = FakeCallback(f"isg:{token}:classic:party1", FakeUser(2), inline_message_id="B")
    await handle_callback(app, first)
    await handle_callback(app, second)

    contract_a = app.store.for_inline("A")
    contract_b = app.store.for_inline("B")
    assert contract_a is not contract_b
    assert contract_a.token != contract_b.token


@pytest.mark.asyncio
async def test_button_rejects_already_signed_side(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    app.store.add_signature(contract, field_key="party1", text="A", user_id=1)
    callback = FakeCallback(
        f"isg:{contract.token}:classic:party1", FakeUser(8), inline_message_id="inline-42"
    )
    await handle_callback(app, callback)
    assert "уже подписана" in callback.answers[-1]


# ---------------------------------------------------------------------------
# deep link -> DM prompt -> signature
# ---------------------------------------------------------------------------


class FakeDM:
    def __init__(self, text: str, user_id: int = 7):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(1, type="private")
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None, **_: object):
        self.answers.append((text, reply_markup))


@pytest.mark.asyncio
async def test_deeplink_sets_pending_and_prompts(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    message = FakeDM(f"/start {DEEPLINK_PREFIX}{contract.token}_party1")
    await handle_dm(app, message)

    assert app.store.pending_for(7) is not None
    text, markup = message.answers[-1]
    assert "Сторона 1" in text
    assert markup is not None


@pytest.mark.asyncio
async def test_deeplink_expired(inline_app, app):
    message = FakeDM(f"/start {DEEPLINK_PREFIX}deadbeef_party1")
    await handle_dm(app, message)
    assert "устарела" in message.answers[-1][0].lower()


@pytest.mark.asyncio
async def test_dm_signature_completes_contract(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    app.store.set_pending(7, contract, "party1")
    message = FakeDM("Иванов И.И.")
    await handle_dm(app, message)

    assert contract.signatures["party1"].text == "Иванов И.И."
    assert contract.signatures["party1"].user_name == "Тест"
    assert "Записал" in message.answers[-1][0]

    app.store.set_pending(8, contract, "party2")
    second = FakeDM("Петров П.П.", user_id=8)
    await handle_dm(app, second)
    assert len(contract.signatures) == 2
    assert inline_app["final"] == [contract.token]
    assert "Готово" in second.answers[-1][0]


@pytest.mark.asyncio
async def test_dm_rejects_long_signature(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    app.store.set_pending(7, contract, "party1")
    message = FakeDM("а" * 80)
    await handle_dm(app, message)
    assert contract.signatures == {}
    assert "Слишком длинно" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_dm_without_pending(inline_app, app):
    message = FakeDM("просто текст")
    await handle_dm(app, message)
    assert "нечего подписывать" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_two_users_can_type_at_once(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    app.store.set_pending(7, contract, "party1")
    app.store.set_pending(8, contract, "party2")
    assert app.store.pending_count == 2

    await handle_dm(app, FakeDM("Иванов И.И.", user_id=7))
    assert "party1" in contract.signatures
    assert app.store.pending_for(8) is not None

    await handle_dm(app, FakeDM("Петров П.П.", user_id=8))
    assert len(contract.signatures) == 2


@pytest.mark.asyncio
async def test_deeplink_blocks_second_user_on_same_side(inline_app, app):
    contract = app.store.create(
        template_key="classic", inline_message_id="inline-42", initiator_id=1
    )
    app.store.set_pending(7, contract, "party1")
    message = FakeDM(f"/start {DEEPLINK_PREFIX}{contract.token}_party1", user_id=8)
    await handle_dm(app, message)
    assert app.store.pending_for(8) is None
    assert "другой" in message.answers[-1][0].lower()
