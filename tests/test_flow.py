from __future__ import annotations

import pytest

from app.handlers import handle_callback, handle_chat_message, split_mention
from app.store import STATE_SIGNED

from .helpers import FakeCallback, FakeChat, FakeMessage, FakeUser


@pytest.fixture()
def patched(app, monkeypatch):
    """Replace Telegram side effects with recorders."""
    calls = {"text": [], "preview": [], "contract": [], "prompt": [], "progress": [], "final": []}

    async def post_text(message, text):
        calls["text"].append(text)

    async def send_preview(message, contract, template):
        calls["preview"].append(template.key)
        contract.inline_message_id = "inline-1"
        return "inline-1"

    async def show_preview_page(contract, index):
        calls["preview"].append(index)
        return True

    async def show_contract(contract, template):
        calls["contract"].append(template.key)
        return True

    async def show_prompt(contract, template, field_key, user_name):
        calls["prompt"].append((field_key, user_name))
        return True

    async def show_signed_progress(contract, template, field_key):
        calls["progress"].append(field_key)
        return True

    async def finalize(contract, template):
        calls["final"].append(dict(contract.signatures))
        return True

    async def restore(contract, template):
        calls["restore"] = True

    async def mark_expired(contract):
        calls["expired"] = True

    monkeypatch.setattr(app, "post_text", post_text)
    monkeypatch.setattr(app, "send_preview", send_preview)
    monkeypatch.setattr(app, "show_preview_page", show_preview_page)
    monkeypatch.setattr(app, "show_contract", show_contract)
    monkeypatch.setattr(app, "show_prompt", show_prompt)
    monkeypatch.setattr(app, "show_signed_progress", show_signed_progress)
    monkeypatch.setattr(app, "finalize", finalize)
    monkeypatch.setattr(app, "restore_contract_view", restore)
    monkeypatch.setattr(app, "mark_expired", mark_expired)
    monkeypatch.setattr(app.storage, "is_ready", lambda key: True)
    return calls


def guest(text: str, user_id: int = 1, chat_id: int = -100500) -> FakeMessage:
    return FakeMessage(text=text, user=FakeUser(user_id), chat=FakeChat(chat_id))


VALID = "Иванов И.И."
LONG = "а" * 40


def test_split_mention():
    assert split_mention("@test_bot", "test_bot") == ("", True)
    assert split_mention("@test_bot минимализм", "test_bot") == ("минимализм", True)
    assert split_mention("@test_bot, классика", "test_bot") == (", классика", True)
    assert split_mention("просто текст", "test_bot") == ("просто текст", False)
    assert split_mention("@another_bot hi", "test_bot") == ("@another_bot hi", False)


@pytest.mark.asyncio
async def test_mention_creates_preview(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    assert contract is not None
    assert patched["preview"] == ["classic"]
    assert contract.inline_message_id == "inline-1"


@pytest.mark.asyncio
async def test_mention_with_template_name(app, patched):
    await handle_chat_message(app, guest("@test_bot минимализм"))
    contract = app.store.for_chat("g-100500")
    assert contract.template_key == "minimal"


@pytest.mark.asyncio
async def test_unknown_query(app, patched):
    await handle_chat_message(app, guest("@test_bot ерунда"))
    assert app.store.for_chat("g-100500") is None
    assert patched["text"], "должно быть сообщение об ошибке"


@pytest.mark.asyncio
async def test_callback_paging(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    callback = FakeCallback(data=f"pn:{contract.token}:1", user=FakeUser(1))
    await handle_callback(app, callback)
    assert patched["preview"][-1] == 1
    assert callback.answers == [""]


@pytest.mark.asyncio
async def test_callback_pick_starts_contract(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    callback = FakeCallback(data=f"pk:{contract.token}:2", user=FakeUser(1))
    await handle_callback(app, callback)
    assert contract.template_key == "dark"
    assert patched["contract"] == ["dark"]


@pytest.mark.asyncio
async def test_callback_sign_sets_pending(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    callback = FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    await handle_callback(app, callback)
    pending = app.store.pending_for(7)
    assert pending is not None
    assert pending.field_key == "party1"
    assert pending.token == contract.token
    assert patched["prompt"][0][0] == "party1"


@pytest.mark.asyncio
async def test_signature_from_pending_user(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    await handle_callback(
        app, FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    )

    reply = FakeMessage(text=VALID, user=FakeUser(7), guest_query_id="new-query")
    await handle_chat_message(app, reply)
    assert contract.signatures["party1"].text == VALID
    assert patched["progress"] == ["party1"]
    assert patched["final"] == []


@pytest.mark.asyncio
async def test_signature_from_stranger_is_rejected(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    await handle_callback(
        app, FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    )
    await handle_chat_message(
        app, FakeMessage(text=VALID, user=FakeUser(8), guest_query_id="new-query")
    )
    assert contract.signatures == {}
    assert patched["text"], "должно быть предупреждение"


@pytest.mark.asyncio
async def test_long_signature_rejected(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    await handle_callback(
        app, FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    )
    await handle_chat_message(
        app, FakeMessage(text=LONG, user=FakeUser(7), guest_query_id="new-query")
    )
    assert contract.signatures == {}
    assert "Слишком длинно" in patched["text"][-1]


@pytest.mark.asyncio
async def test_two_users_finish_contract(app, patched):
    await handle_chat_message(app, guest("@test_bot", user_id=1))
    contract = app.store.for_chat("g-100500")

    for user_id, field_key in ((7, "party1"), (8, "party2")):
        await handle_callback(
            app,
            FakeCallback(data=f"sg:{contract.token}:{field_key}", user=FakeUser(user_id)),
        )
        await handle_chat_message(
            app,
            FakeMessage(text=VALID, user=FakeUser(user_id), guest_query_id=f"q{user_id}"),
        )

    assert len(contract.signatures) == 2
    assert patched["final"] and len(patched["final"][0]) == 2
    assert not contract.same_signers()


@pytest.mark.asyncio
async def test_same_user_signs_both(app, patched):
    await handle_chat_message(app, guest("@test_bot", user_id=5))
    contract = app.store.for_chat("g-100500")
    for field_key in ("party1", "party2"):
        await handle_callback(
            app, FakeCallback(data=f"sg:{contract.token}:{field_key}", user=FakeUser(5))
        )
        await handle_chat_message(
            app,
            FakeMessage(text=VALID, user=FakeUser(5), guest_query_id=f"q-{field_key}"),
        )
    assert contract.same_signers()
    assert patched["final"]


@pytest.mark.asyncio
async def test_cancel_by_initiator(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    callback = FakeCallback(data=f"cx:{contract.token}", user=FakeUser(1))
    await handle_callback(app, callback)
    assert patched.get("restore")
    assert app.store.pending_for(1) is None


@pytest.mark.asyncio
async def test_cancel_by_stranger_rejected(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    callback = FakeCallback(data=f"cx:{contract.token}", user=FakeUser(99))
    await handle_callback(app, callback)
    assert callback.answers and "другой" in callback.answers[-1].lower()


@pytest.mark.asyncio
async def test_stale_token(app, patched):
    callback = FakeCallback(data="sg:deadbeef:party1", user=FakeUser(1))
    await handle_callback(app, callback)
    assert callback.answers and "неактуален" in callback.answers[-1]


@pytest.mark.asyncio
async def test_signed_contract_rejects_callbacks(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    contract.state = STATE_SIGNED
    callback = FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(1))
    await handle_callback(app, callback)
    assert callback.answers[-1] == app.config.text("callback.done")


@pytest.mark.asyncio
async def test_mention_with_signature_text(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    await handle_callback(
        app, FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    )
    # User types the signature together with a mention instead of replying.
    await handle_chat_message(app, guest("@test_bot Иванов И.И.", user_id=7))
    assert contract.signatures["party1"].text == "Иванов И.И."


@pytest.mark.asyncio
async def test_template_name_wins_over_signature(app, patched):
    await handle_chat_message(app, guest("@test_bot"))
    contract = app.store.for_chat("g-100500")
    await handle_callback(
        app, FakeCallback(data=f"sg:{contract.token}:party1", user=FakeUser(7))
    )
    await handle_chat_message(app, guest("@test_bot минимализм", user_id=7))
    # A known tag starts a new contract instead of becoming a signature.
    assert contract.signatures == {}


class FakeDM:
    def __init__(self, text: str, user_id: int = 42, chat_id: int = 42):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id, type="private")
        self.answers: list[str] = []

    async def answer(self, text: str, **_: object) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_dm_start(app, monkeypatch):
    from app.handlers import handle_dm

    async def ready():
        return (3, 3)

    monkeypatch.setattr(app, "ensure_templates_ready", ready)
    message = FakeDM("/start")
    await handle_dm(app, message)
    assert message.answers and "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_dm_owner_only(app):
    from app.handlers import handle_dm

    message = FakeDM("/status", user_id=777)
    await handle_dm(app, message)
    assert message.answers == [app.config.text("dm.owner_only")]


@pytest.mark.asyncio
async def test_dm_status_owner(app):
    from app.handlers import handle_dm

    message = FakeDM("/status", user_id=app.settings.owner_id)
    await handle_dm(app, message)
    assert message.answers and "Guest Mode" in message.answers[0]


@pytest.mark.asyncio
async def test_dm_debug_owner_only(app):
    from app.handlers import handle_dm

    stranger = FakeDM("/debug", user_id=777)
    await handle_dm(app, stranger)
    assert stranger.answers == [app.config.text("dm.owner_only")]

    owner = FakeDM("/debug", user_id=app.settings.owner_id)
    await handle_dm(app, owner)
    assert owner.answers
    assert "Отладка" in owner.answers[0]
    assert "Последние события" in owner.answers[0]
