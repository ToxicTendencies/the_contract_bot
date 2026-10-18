"""Telegram handlers: everything happens inside the chat.

The bot is summoned with ``@bot`` in any chat, posts the blank right there and
edits its own message as the contract moves along.  A side button sets the
pending signature for that member, and the signature itself is sent as a reply
to the bot's message.

Two ways to be in a chat are supported: **guest mode** (the bot is not a member
and answers the summon) and **member mode** (the bot was added to the group and
sends a normal message).  Both share the same scenario.  The owner's DM is only
used for commands and the archive.

Templates may have a free-form ``terms`` block: then the bot first asks for the
contract text and prints it on the blank before collecting signatures.
"""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, User

from .config import TemplateConfig
from .render import validate_signature, validate_terms
from .store import (
    STATE_PREVIEW,
    STATE_SIGNED,
    STATE_TERMS,
    Contract,
    PendingSignature,
    chat_key,
)

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)

Say = Callable[[str], Awaitable[None]]


def _sender(message: Message) -> User | None:
    return message.from_user or message.guest_bot_caller_user


def split_mention(text: str, username: str) -> tuple[str, bool]:
    """Strip ``@bot`` from the text and report whether it was there."""
    if not username:
        return text.strip(), False
    pattern = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
    found = bool(pattern.search(text))
    return pattern.sub(" ", text).strip(), found


def _chat_key(message: Message) -> str:
    return chat_key(message.chat.id, message.message_thread_id)


def _reply_contract(app: "App", message: Message, key: str) -> Contract | None:
    """Контракт, на сообщение которого ответили (в режиме участника)."""
    reply = message.reply_to_message
    if reply is None:
        return None
    author = getattr(reply, "from_user", None)
    if author is None or getattr(author, "id", None) != app.bot_id:
        return None
    return app.store.for_message(key, getattr(reply, "message_id", None))


def _user_name(user: User) -> str:
    return user.full_name or (user.username or str(user.id))


def _error_text(app: "App", error: str | None, check, field_label: str) -> str:
    key = {
        "empty": "guest.empty",
        "too_long": "guest.too_long",
        "does_not_fit": "guest.does_not_fit",
    }.get(error or "", "guest.does_not_fit")
    return app.config.text(
        key,
        length=check.length,
        max_length=check.max_length,
        label=html.escape(field_label),
    )


NEW_CONTRACT_WORDS = {
    "новый",
    "новый контракт",
    "заново",
    "с начала",
    "сброс",
    "сбросить",
    "new",
    "reset",
}


def _wants_new_contract(text: str) -> bool:
    return text.strip().lower() in NEW_CONTRACT_WORDS


def _max_terms_length(app: "App") -> int:
    limits = [
        template.terms.style.max_length
        for template in app.config.templates.ordered()
        if template.terms is not None
    ]
    return max(limits) if limits else 0


def _terms_error_text(app: "App", error: str | None, check) -> str:
    key = {
        "empty": "guest.terms_empty",
        "too_long": "guest.terms_too_long",
        "does_not_fit": "guest.terms_does_not_fit",
    }.get(error or "", "guest.terms_does_not_fit")
    return app.config.text(
        key,
        length=check.length,
        max_length=check.max_length,
    )


def build_router(app: "App") -> Router:
    router = Router(name="contracts")

    @router.guest_message()
    async def on_guest(message: Message) -> None:
        try:
            await handle_chat_message(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки guest-сообщения")
            await app.post_text(message, app.config.text("errors.generic"))

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_group_message(message: Message) -> None:
        try:
            await handle_chat_message(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения в чате")
            await app.post_text(message, app.config.text("errors.generic"))

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        try:
            await handle_callback(app, query)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки callback")
            try:
                await query.answer(app.config.text("errors.generic"))
            except Exception:  # noqa: BLE001
                pass

    @router.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def on_dm(message: Message) -> None:
        try:
            await handle_dm(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения в ЛС")
            await message.answer(app.config.text("errors.generic"))

    return router


# ---------------------------------------------------------------------------
# signatures and free-form terms (collected in the chat)
# ---------------------------------------------------------------------------


async def apply_signature(
    app: "App",
    contract: Contract,
    user: User,
    text: str,
    say: Say,
    *,
    pending_field: str | None = None,
) -> None:
    template = app.template_or_first(contract.template_key)
    if template is None:
        await say(app.config.text("errors.no_templates"))
        return

    field_key = pending_field
    if field_key is None:
        field_key = next(
            (key for key in template.fields if key not in contract.signatures), None
        )
    if field_key is None:
        await say(app.config.text("callback.done"))
        return

    field = template.fields.get(field_key)
    if field is None:
        await say(app.config.text("errors.generic"))
        return
    if field_key in contract.signatures:
        await say(app.config.text("guest.already_signed", label=html.escape(field.label)))
        return

    check = validate_signature(text, field, app.settings.limits)
    if not check.ok:
        await say(_error_text(app, check.error, check, field.label))
        return

    app.store.add_signature(
        contract,
        field_key=field_key,
        text=check.text,
        user_id=user.id,
        user_name=_user_name(user),
        user_username=user.username or "",
    )

    if len(contract.signatures) >= len(template.fields):
        ok = await app.finalize(contract, template)
        await say(app.config.text("guest.sign_complete" if ok else "guest.upload_failed"))
        return

    remaining = [
        field.label for key, field in template.fields.items() if key not in contract.signatures
    ]
    if contract.has_message:
        await app.show_signed_progress(contract, template, field_key)
    await say(
        app.config.text(
            "guest.sign_saved",
            label=html.escape(field.label),
            remaining=html.escape(", ".join(remaining)),
        )
    )


async def handle_pending_text(
    app: "App",
    message: Message,
    user: User,
    text: str,
    say: Say,
    pending: PendingSignature | None = None,
) -> None:
    key = _chat_key(message)
    pending = pending if pending is not None else app.store.pending_for(user.id, key)
    if pending is None:
        return
    contract = app.store.get(pending.token)
    if contract is None:
        app.store.clear_pending(user.id, pending.token)
        await say(app.config.text("guest.contract_expired", bot=app.bot_username))
        return
    if contract.chat_key is not None and contract.chat_key != key:
        # Ответ пришёл в другой чат: чужой контракт не подписываем,
        # но ожидание сохраняем — человек ещё может ответить там, где нажал.
        return
    if contract.state == STATE_SIGNED:
        app.store.clear_pending(user.id, pending.token)
        return
    await apply_signature(app, contract, user, text, say, pending_field=pending.field_key)


async def handle_terms_text(
    app: "App",
    message: Message,
    user: User,
    text: str,
    say: Say,
    contract: Contract,
) -> None:
    """Validate the free-form terms and print them on the blank."""
    template = app.template_or_first(contract.template_key)
    if template is None or template.terms is None:
        app.store.drop(contract)
        await say(app.config.text("guest.contract_expired", bot=app.bot_username))
        return

    check = validate_terms(text, template.terms)
    if not check.ok:
        await say(_terms_error_text(app, check.error, check))
        return

    app.store.set_terms(contract, check.text)
    ok = await app.show_terms_contract(contract, template)
    if not ok:
        # Give the sender another chance: keep waiting for the terms.
        contract.state = STATE_TERMS
        contract.terms_user_id = user.id
        contract.touch()


# ---------------------------------------------------------------------------
# guest mode: the whole flow in the chat
# ---------------------------------------------------------------------------


async def handle_chat_message(app: "App", message: Message) -> None:
    """Единый вход: и guest-ответ, и обычное сообщение в чате-участнике."""
    user = message.from_user
    if user is None or getattr(user, "is_bot", False):
        return

    text = message.text or ""
    query, had_mention = split_mention(text, app.bot_username)
    key = _chat_key(message)

    reply = message.reply_to_message
    reply_author = reply.from_user if reply is not None else None
    is_reply_to_bot = bool(reply_author is not None and reply_author.id == app.bot_id)
    # В guest-режиме бот получает только упоминания и ответы на свои сообщения,
    # в чате-участнике — всё подряд, поэтому там принимаем текст только ответом.
    accept_input = bool(message.guest_query_id) or is_reply_to_bot

    async def say(reply_text: str) -> None:
        await app.post_text(message, reply_text)

    if not had_mention:
        if reply_author is not None and reply_author.is_bot and not is_reply_to_bot:
            return  # someone is talking to another bot

        # Ответ на конкретный бланк важнее «последнего действия»: подпись
        # не должна уехать в соседний бланк или в другой чат.
        target = _reply_contract(app, message, key)
        pending: PendingSignature | None = None
        terms_contract: Contract | None = None
        if target is not None:
            pending = app.store.pending_for(user.id, token=target.token)
            if (
                pending is None
                and target.state == STATE_TERMS
                and target.terms_user_id == user.id
            ):
                terms_contract = target
        if pending is None and terms_contract is None:
            pending = app.store.pending_for(user.id, key)
        if pending is None and terms_contract is None:
            terms_contract = app.store.terms_for_user(user.id, key)

        if pending is not None:
            if not accept_input:
                return
            if not text:
                await say(app.config.text("guest.text_only"))
                return
            await handle_pending_text(app, message, user, text, say, pending=pending)
            return

        if terms_contract is not None:
            if not accept_input:
                return
            if not text:
                await say(app.config.text("guest.text_only"))
                return
            await handle_terms_text(app, message, user, text, say, terms_contract)
            return

        if not accept_input:
            return
        contract = app.store.for_chat(key)
        if contract is None:
            return
        if app.store.pending_for(user.id) is not None:
            # Подпись уже начата в другом чате — не подписываем этот молча.
            await say(app.config.text("guest.pending_elsewhere"))
            return
        if contract.state == STATE_TERMS:
            await say(app.config.text("guest.terms_not_yours"))
            return
        if contract.state != STATE_SIGNED:
            await say(app.config.text("guest.not_pending"))
        return

    if query.startswith("/"):
        command = query.split()[0].lower()
        if command.startswith("/help"):
            await say(
                app.config.text(
                    "guest.help",
                    bot=app.bot_username,
                    max_length=app.settings.limits.signature_max_length,
                    ttl_minutes=app.settings.contract.ttl_minutes,
                    terms_max=_max_terms_length(app),
                )
            )
            return
        query = ""

    # «@бот новый» — забыть прежний бланк. Разбор отложен до создания:
    # неизвестное имя шаблона не должно стирать текущий контракт.
    reset = bool(query) and _wants_new_contract(query)
    if reset:
        query = ""

    templates = app.templates()
    if not templates:
        await say(app.config.text("guest.no_templates"))
        return

    template = None
    explicit = False
    if query:
        template = app.config.templates.find(query)
        if template is None:
            pending = app.store.pending_for(user.id, key)
            if pending is not None and accept_input:
                await handle_pending_text(app, message, user, query, say, pending=pending)
                return
            terms_contract = app.store.terms_for_user(user.id, key)
            if terms_contract is not None and accept_input:
                await handle_terms_text(app, message, user, query, say, terms_contract)
                return
            names = ", ".join(item.key for item in templates)
            await say(
                app.config.text(
                    "guest.unknown_query",
                    query=html.escape(query),
                    templates=html.escape(names),
                    bot=app.bot_username,
                )
            )
            return
        explicit = True
    if template is None:
        template = templates[0]

    # В чате живёт один открытый контракт. Повторное упоминание не плодит
    # второй бланк (иначе второй участник подписывает свою копию), а переносит
    # текущий контракт вниз — к нему же, с теми же подписями и условиями.
    open_contract = None if reset else app.store.open_for_chat(key)
    if open_contract is not None:
        current = app.template_or_first(open_contract.template_key) or template
        untouched = (
            open_contract.state == STATE_PREVIEW
            and not open_contract.signatures
            and not open_contract.terms
        )
        if explicit and untouched:
            current = template
            open_contract.template_key = template.key
            open_contract.page = app.index_of(template.key)
        # Не получилось — контракт остаётся жить на прежней карточке:
        # подписи терять из-за неудачной пересылки нельзя.
        if not await app.repost_contract(message, open_contract, current):
            await say(app.config.text("errors.generic"))
        return

    contract = app.store.create(
        chat_key=key,
        template_key=template.key,
        initiator_id=user.id,
        initiator_name=_user_name(user),
        page=app.index_of(template.key),
    )
    if template.has_terms:
        contract.terms_user_id = user.id
        ok = await app.send_terms_prompt(message, contract, template)
    else:
        ok = await app.send_preview(message, contract, template)
    if not ok:
        app.store.drop(contract)
        return
    # Прежние бланки чата забываем: работать будем с этим.
    app.store.drop_chat(key, keep=contract.token)


# ---------------------------------------------------------------------------
# callbacks
# ---------------------------------------------------------------------------


async def handle_callback(app: "App", query: CallbackQuery) -> None:
    action, _, rest = (query.data or "").partition(":")
    token, _, argument = rest.partition(":")
    user = query.from_user

    log.info(
        "callback %s token=%s user=%s inline=%s",
        action,
        token or "-",
        user.id,
        (query.inline_message_id or "-")[:14],
    )

    if action in {"pv", "pn", "pk"}:
        await _handle_preview(app, query, token, argument, pick=(action == "pk"))
        return

    if action == "isg":
        # Кнопки старого inline-сценария: он больше не используется.
        await query.answer(_callback_expired(app))
        return

    if action == "sg":
        template_key, field_key = _split_side(argument)
        await _handle_sign_press(app, query, token, template_key, field_key)
        return

    if action == "cx":
        await _handle_cancel(app, query, token)
        return

    if action == "no":
        contract = app.store.get(token)
        if contract is None or not _is_current_card(contract, query):
            await query.answer(_stale_notice(app, query, token))
            return
        if contract.state == STATE_SIGNED:
            await query.answer(app.config.text("callback.done"))
        else:
            await query.answer(app.config.text("callback.signed"))
        return

    await query.answer(app.config.text("errors.generic"))


def _callback_expired(app: "App") -> str:
    return app.config.text("callback.expired", bot=app.bot_username)


def _template_for(
    app: "App", contract: Contract, template_key: str
) -> TemplateConfig | None:
    """Шаблон контракта: он важнее того, что написано в кнопке."""
    template = app.config.templates.get(contract.template_key)
    if template is not None:
        return template
    template = app.config.templates.get(template_key) if template_key else None
    if template is not None:
        return template
    ordered = app.config.templates.ordered()
    return ordered[0] if ordered else None


def _split_side(argument: str) -> tuple[str, str]:
    """``template:field`` (new) or just ``field`` (buttons sent before v2)."""
    head, _, tail = argument.partition(":")
    if tail:
        return head, tail
    return "", head


def _is_current_card(contract: Contract, query: CallbackQuery) -> bool:
    """Кнопка нажата на актуальном сообщении контракта, а не на старом."""
    if query.inline_message_id:
        return contract.inline_message_id == query.inline_message_id
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is not None and contract.message_id is not None:
        return message_id == contract.message_id
    return True


def _stale_notice(app: "App", query: CallbackQuery, token: str) -> str:
    """Что ответить на кнопку: контракт жив, но карточка уже старая."""
    contract = app.store.get(token)
    if contract is not None and not _is_current_card(contract, query):
        return app.config.text("callback.continued")
    return _callback_expired(app)


def _resolve_contract(
    app: "App", query: CallbackQuery, token: str
) -> Contract | None:
    """Контракт за кнопкой.

    Состояние живёт только в RAM, поэтому заново контракт не собирается:
    после перезапуска старые кнопки честно отвечают «неактуально», а не
    начинают подпись с пустого бланка. Кнопки старой карточки, которую
    перенесли новым сообщением, отвечают «продолжение ниже».
    """
    contract = app.store.get(token)
    if contract is None:
        return None
    if not _is_current_card(contract, query):
        return None
    return contract


async def _handle_preview(
    app: "App", query: CallbackQuery, token: str, argument: str, *, pick: bool
) -> None:
    contract = app.store.get(token)
    if contract is None:
        # Превью не восстановить: в callback_data только токен.
        await query.answer(_callback_expired(app))
        return
    if not _is_current_card(contract, query):
        await query.answer(app.config.text("callback.continued"))
        return
    if contract.state == STATE_SIGNED:
        await query.answer(app.config.text("callback.done"))
        return

    templates = app.templates()
    if not templates:
        await query.answer(app.config.text("errors.no_templates"))
        return

    try:
        index = int(argument)
    except ValueError:
        index = contract.page
    index = app.clamp_index(index, len(templates))

    if not pick:
        ok = await app.show_preview_page(contract, index)
        await query.answer("" if ok else app.config.text("errors.generic"))
        return

    template = templates[index]
    if not app.storage.is_ready(template.key):
        await query.answer(app.config.text("errors.no_templates"))
        return

    contract.template_key = template.key
    contract.page = index
    if template.has_terms:
        contract.terms = ""
        contract.terms_user_id = query.from_user.id
        contract.state = STATE_TERMS
        ok = await app.edit_terms_prompt(contract, template)
        await query.answer("" if ok else app.config.text("errors.generic"))
        return

    ok = await app.show_contract(contract, template)
    await query.answer("" if ok else app.config.text("errors.no_templates"))


async def _handle_sign_press(
    app: "App",
    query: CallbackQuery,
    token: str,
    template_key: str,
    field_key: str,
) -> None:
    contract = _resolve_contract(app, query, token)
    if contract is None:
        await query.answer(_stale_notice(app, query, token))
        return

    template = _template_for(app, contract, template_key)
    if template is None:
        await query.answer(app.config.text("errors.no_templates"))
        return
    field = template.fields.get(field_key)
    if field is None:
        await query.answer(app.config.text("errors.generic"))
        return

    if contract.state == STATE_TERMS:
        await query.answer(app.config.text("callback.terms_first"))
        return
    if contract.state == STATE_SIGNED:
        await query.answer(app.config.text("callback.done"))
        return
    if field_key in contract.signatures:
        await query.answer(app.config.text("callback.already_signed", label=field.label))
        return

    holder = app.store.pending_holder(contract.token, field_key)
    if holder is not None and holder != query.from_user.id:
        await query.answer(app.config.text("callback.waiting"))
        return

    app.store.set_pending(query.from_user.id, contract, field_key)
    ok = await app.show_prompt(
        contract, template, field_key, _user_name(query.from_user) or None
    )
    await query.answer("" if ok else app.config.text("errors.generic"))


async def _handle_cancel(app: "App", query: CallbackQuery, token: str) -> None:
    contract = app.store.get(token)
    if contract is None or not _is_current_card(contract, query):
        await query.answer(_stale_notice(app, query, token))
        return

    holders = {pending.user_id for pending in app.store.pending_for_contract(contract.token)}
    allowed = query.from_user.id in {contract.initiator_id, *holders}
    if not allowed:
        await query.answer(app.config.text("callback.not_yours"))
        return

    app.store.clear_pending(token=contract.token)
    template = app.template_or_first(contract.template_key)
    if template is not None:
        await app.restore_contract_view(contract, template)
    await query.answer(app.config.text("callback.cancelled"))


# ---------------------------------------------------------------------------
# DM: commands and the archive only
# ---------------------------------------------------------------------------


async def handle_dm(app: "App", message: Message) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    text = (message.text or "").strip()

    async def say(reply: str) -> None:
        await message.answer(reply)

    if text.startswith("/"):
        command, _, _argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await _dm_greeting(app, message, user, command)
            return
        await _dm_owner_command(app, message, user, command)
        return

    await say(app.config.text("dm.nothing_pending", bot=app.bot_username))


async def _dm_greeting(app: "App", message: Message, user: User, command: str) -> None:
    is_owner = user.id == app.settings.owner_id
    if command == "/start" and is_owner:
        ready, total = await app.ensure_templates_ready()
        if ready < total:
            await message.answer(app.config.text("dm.warmup_partial", ok=ready, total=total))
    await message.answer(
        app.config.text(
            "dm.greeting" if command == "/start" else "dm.help",
            bot=app.bot_username,
            max_length=app.settings.limits.signature_max_length,
            ttl_minutes=app.settings.contract.ttl_minutes,
        )
    )


async def _dm_owner_command(
    app: "App", message: Message, user: User, command: str
) -> None:
    if command not in {"/templates", "/status", "/reload"}:
        await message.answer(app.config.text("dm.unknown_dm", bot=app.bot_username))
        return
    if user.id != app.settings.owner_id:
        await message.answer(app.config.text("dm.owner_only"))
        return

    if command == "/templates":
        if not app.templates():
            await message.answer(app.config.text("dm.no_templates"))
            return
        await message.answer(
            app.config.text("dm.templates_ok", list="\n".join(app.describe_templates()))
        )
        return

    if command == "/reload":
        try:
            app.config.maybe_reload(force=True)
        except Exception as exc:  # noqa: BLE001
            await message.answer(app.config.text("dm.reload_failed", error=exc))
            return
        ready, total = await app.ensure_templates_ready()
        await message.answer(app.config.text("dm.reloaded", count=ready, total=total))
        return

    await message.answer(
        app.config.text(
            "dm.status",
            guest_mode="включён" if app.guest_mode_enabled else "выключен",
            count=len(app.templates()),
            active=app.store.active_count,
            pending=app.store.pending_count,
            uptime=app.uptime(),
        )
    )
