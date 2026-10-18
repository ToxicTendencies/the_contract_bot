"""Telegram handlers.

Two ways to start a contract:

* **inline mode** — ``@bot`` or ``@bot средневековый`` in any chat.  Choosing a
  result posts the blank with buttons; a button press opens the bot's DM where
  the signature is typed.  The inline message is edited when everyone signed.
* **guest mode** — a plain ``@bot`` mention message.  Everything, including the
  signature, happens in the chat.

Owner commands live in the DM as well.
"""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    ForceReply,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    Message,
    User,
)

from . import ui
from .config import TemplateConfig
from .render import validate_signature
from .store import STATE_SIGNED, Contract, chat_key, new_token

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)

Say = Callable[[str], Awaitable[None]]

# /start payload prefix for "open signature prompt for <token>_<field>"
DEEPLINK_PREFIX = "s_"

# Telegram caps start parameters at 64 chars: "s_" + 8 token + "_" + 16 field.
MAX_START_PAYLOAD = 64


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


def build_router(app: "App") -> Router:
    router = Router(name="contracts")

    @router.inline_query()
    async def on_inline(inline_query: InlineQuery) -> None:
        try:
            await handle_inline_query(app, inline_query)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки inline-запроса")

    @router.chosen_inline_result()
    async def on_chosen(result: ChosenInlineResult) -> None:
        try:
            await handle_chosen_inline_result(app, result)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки выбранного inline-результата")

    @router.guest_message()
    async def on_guest(message: Message) -> None:
        try:
            await handle_guest(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки guest-сообщения")
            await app.post_guest_text(message, app.config.text("errors.generic"))

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
# inline mode
# ---------------------------------------------------------------------------


async def handle_inline_query(app: "App", inline_query: InlineQuery) -> None:
    templates = app.config.templates.find_all(inline_query.query)
    results: list[object] = []
    max_results = 50  # Telegram's hard limit

    for template in templates[:max_results]:
        file_id = app.storage.file_id(template.key)
        if not file_id:
            continue
        token = new_token()
        results.append(
            InlineQueryResultCachedPhoto(
                id=f"{token}-{template.key}",
                photo_file_id=file_id,
                title=template.inline_name,
                description=template.inline_hint,
                caption=ui.inline_caption(
                    app.config, template, app.max_length_for(template)
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=ui.inline_sign_markup(app.config, token, template),
            )
        )

    if not results:
        names = ", ".join(template.inline_name for template in app.templates())
        title = (
            app.config.text("inline.nothing_found_title")
            if inline_query.query.strip()
            else app.config.text("inline.uploading_title")
        )
        description = (
            app.config.text("inline.nothing_found_description", names=names)
            if inline_query.query.strip()
            else app.config.text("inline.uploading_description")
        )
        results.append(
            InlineQueryResultArticle(
                id="no-results",
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=description, parse_mode=ParseMode.HTML
                ),
            )
        )

    await inline_query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


async def handle_chosen_inline_result(app: "App", result: ChosenInlineResult) -> None:
    """Bind the sent inline message to a contract.

    Inline feedback is optional: the same binding also happens on the first
    button press, which always carries ``inline_message_id``.
    """
    if not result.inline_message_id:
        return
    token, _, template_key = result.result_id.partition("-")
    template = app.config.templates.get(template_key)
    if template is None or not token:
        return

    contract = app.store.get(token)
    if contract is None:
        app.store.create(
            template_key=template_key,
            inline_message_id=result.inline_message_id,
            initiator_id=result.from_user.id,
            initiator_name=_user_name(result.from_user),
            token=token,
        )
    elif not contract.inline_message_id:
        app.store.bind_inline(contract, result.inline_message_id)


async def _handle_inline_sign_press(
    app: "App",
    query: CallbackQuery,
    token: str,
    template_key: str,
    field_key: str,
) -> None:
    """Кнопка стороны на inline-сообщении: открыть личку с подсказкой.

    Callback всегда несёт ``inline_message_id``, поэтому контракт можно
    восстановить, даже если бот перезапускался.
    """
    contract = _resolve_contract(app, query, token, template_key, query.from_user)
    if contract is None:
        await query.answer(_callback_expired(app))
        return

    template = _template_for(app, contract, template_key)
    field = template.fields.get(field_key) if template else None
    if template is None or field is None:
        await query.answer(_callback_expired(app))
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

    payload = f"{DEEPLINK_PREFIX}{contract.token}_{field_key}"
    url = f"https://t.me/{app.bot_username}?start={payload}"
    await query.answer(app.config.text("inline.opening_dm"), url=url)


# ---------------------------------------------------------------------------
# signatures (shared by DM and guest flows)
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
        await say(
            app.config.text("dm.sign_complete" if ok else "guest.upload_failed")
        )
        return

    remaining = [
        field.label for key, field in template.fields.items() if key not in contract.signatures
    ]
    if contract.inline_message_id:
        await app.show_signed_progress(contract, template, field_key)
    await say(
        app.config.text(
            "dm.sign_saved",
            label=html.escape(field.label),
            remaining=html.escape(", ".join(remaining)),
            bot=app.bot_username,
        )
    )


async def handle_pending_text(
    app: "App", message: Message, user: User, text: str, say: Say
) -> None:
    pending = app.store.pending_for(user.id)
    if pending is None:
        return
    contract = app.store.get(pending.token)
    if contract is None:
        app.store.clear_pending(user.id)
        await say(app.config.text("guest.contract_expired", bot=app.bot_username))
        return
    if contract.state == STATE_SIGNED:
        app.store.clear_pending(user.id)
        return
    await apply_signature(app, contract, user, text, say, pending_field=pending.field_key)


# ---------------------------------------------------------------------------
# guest mode (everything in the chat)
# ---------------------------------------------------------------------------


async def handle_guest(app: "App", message: Message) -> None:
    if not message.guest_query_id:
        return
    user = _sender(message)
    if user is None or getattr(user, "is_bot", False):
        return

    text = message.text or ""
    query, had_mention = split_mention(text, app.bot_username)
    key = _chat_key(message)
    contract = app.store.for_chat(key)

    async def say(reply: str) -> None:
        await app.post_guest_text(message, reply)

    if not had_mention:
        reply_author = message.reply_to_message.from_user if message.reply_to_message else None
        if reply_author is not None and reply_author.is_bot and reply_author.id != app.bot_id:
            return  # someone is talking to another bot
        pending = app.store.pending_for(user.id)
        if pending is not None:
            await handle_pending_text(app, message, user, text, say)
            return
        if contract is not None and contract.state != STATE_SIGNED:
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
                )
            )
            return
        query = ""

    templates = app.templates()
    if not templates:
        await say(app.config.text("guest.no_templates"))
        return

    template = None
    if query:
        template = app.config.templates.find(query)
        if template is None:
            pending = app.store.pending_for(user.id)
            if pending is not None:
                await handle_pending_text(app, message, user, query, say)
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
    if template is None:
        template = templates[0]

    # Не затираем предыдущий контракт чата: у каждого сообщения свой токен,
    # иначе кнопки у второго участника отвечают «неактуально».
    contract = app.store.create(
        chat_key=key,
        template_key=template.key,
        initiator_id=user.id,
        initiator_name=_user_name(user),
        page=app.index_of(template.key),
    )
    inline_id = await app.send_preview(message, contract, template)
    if inline_id is None:
        app.store.drop(contract)


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

    if action == "sg":
        template_key, field_key = _split_side(argument)
        await _handle_sign_press(app, query, token, template_key, field_key)
        return

    if action == "isg":
        template_key, field_key = _split_side(argument)
        await _handle_inline_sign_press(
            app, query, token, template_key, field_key
        )
        return

    if action == "cx":
        await _handle_cancel(app, query, token)
        return

    if action == "no":
        contract = app.store.for_inline(query.inline_message_id) or app.store.get(token)
        if contract is not None and contract.state == STATE_SIGNED:
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


def _chat_key_from(query: CallbackQuery) -> str | None:
    message = getattr(query, "message", None)
    if message is None or getattr(message, "chat", None) is None:
        return None
    return chat_key(message.chat.id, getattr(message, "message_thread_id", None))


def _resolve_contract(
    app: "App",
    query: CallbackQuery,
    token: str,
    template_key: str,
    user: User,
) -> Contract | None:
    """Найти контракт за кнопкой, при необходимости собрав его заново.

    Состояние живёт только в RAM, поэтому кнопка — это ещё и инструкция,
    как восстановить контракт: ``token`` + ``template_key`` + сторона.
    Так старые сообщения продолжают работать после перезапуска бота.
    """
    inline_id = query.inline_message_id

    if inline_id:
        bound = app.store.for_inline(inline_id)
        if bound is not None:
            return bound

    contract = app.store.get(token)

    if contract is not None:
        if not inline_id:
            return contract
        if contract.inline_message_id == inline_id:
            return contract
        if not contract.inline_message_id:
            app.store.bind_inline(contract, inline_id)
            return contract
        # Тот же inline-результат отправлен во второй чат: у копии своя жизнь.
        contract = None

    template = app.config.templates.get(template_key) if template_key else None
    if template is None:
        template = _fallback_template(app, query)

    if template is None:
        return None

    message = getattr(query, "message", None)
    log.info(
        "восстанавливаю контракт по кнопке: token=%s template=%s inline=%s",
        token or "-",
        template.key,
        (inline_id or "-")[:14],
    )
    return app.store.create(
        template_key=template.key,
        chat_key=None if inline_id else _chat_key_from(query),
        inline_message_id=inline_id,
        chat_id=getattr(getattr(message, "chat", None), "id", None),
        message_id=getattr(message, "message_id", None),
        initiator_id=user.id,
        initiator_name=_user_name(user),
        token=token if token and app.store.get(token) is None else None,
    )


def _fallback_template(app: "App", query: CallbackQuery) -> TemplateConfig | None:
    """Шаблон для кнопок, отправленных до того, как в callback попал ключ."""
    key = _chat_key_from(query)
    if not key:
        return None
    contract = app.store.for_chat(key)
    if contract is None:
        return None
    return app.config.templates.get(contract.template_key)


async def _handle_preview(
    app: "App", query: CallbackQuery, token: str, argument: str, *, pick: bool
) -> None:
    contract = app.store.get(token)
    if contract is None:
        # Превью не восстановить: в callback_data только токен.
        await query.answer(_callback_expired(app))
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
    ok = await app.show_contract(contract, template)
    await query.answer("" if ok else app.config.text("errors.no_templates"))


async def _handle_sign_press(
    app: "App",
    query: CallbackQuery,
    token: str,
    template_key: str,
    field_key: str,
) -> None:
    contract = _resolve_contract(app, query, token, template_key, query.from_user)
    if contract is None:
        await query.answer(_callback_expired(app))
        return

    template = _template_for(app, contract, template_key)
    if template is None:
        await query.answer(app.config.text("errors.no_templates"))
        return
    field = template.fields.get(field_key)
    if field is None:
        await query.answer(app.config.text("errors.generic"))
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
    contract = app.store.for_inline(query.inline_message_id) or app.store.get(token)
    if contract is None:
        await query.answer(_callback_expired(app))
        return

    holders = {pending.user_id for pending in app.store.pending_for_contract(contract.token)}
    allowed = query.from_user.id in {contract.initiator_id, *holders}
    if not allowed:
        await query.answer(app.config.text("callback.not_yours"))
        return

    for pending in app.store.pending_for_contract(contract.token):
        app.store.clear_pending(pending.user_id)
    template = app.template_or_first(contract.template_key)
    if template is not None:
        await app.restore_contract_view(contract, template)
    await query.answer(app.config.text("callback.cancelled"))


# ---------------------------------------------------------------------------
# DM
# ---------------------------------------------------------------------------


async def handle_dm(app: "App", message: Message) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    text = (message.text or "").strip()

    async def say(reply: str) -> None:
        await message.answer(reply)

    if text.startswith("/"):
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/start" and argument.startswith(DEEPLINK_PREFIX):
            await handle_sign_deeplink(app, message, argument, user)
            return
        if command in {"/start", "/help"}:
            await _dm_greeting(app, message, user, command)
            return
        await _dm_owner_command(app, message, user, command)
        return

    pending = app.store.pending_for(user.id)
    if pending is not None:
        await handle_pending_text(app, message, user, text, say)
        return

    await say(app.config.text("dm.nothing_pending", bot=app.bot_username))


async def handle_sign_deeplink(
    app: "App", message: Message, payload: str, user: User
) -> None:
    body = payload[len(DEEPLINK_PREFIX):]
    token, _, field_key = body.partition("_")
    contract = app.store.get(token)
    template = app.template_or_first(contract.template_key) if contract else None
    field = template.fields.get(field_key) if template and field_key else None

    if contract is None or template is None or field is None:
        await message.answer(app.config.text("dm.link_expired", bot=app.bot_username))
        return
    if contract.state == STATE_SIGNED:
        await message.answer(app.config.text("callback.done"))
        return
    if field_key in contract.signatures:
        await message.answer(
            app.config.text("guest.already_signed", label=html.escape(field.label))
        )
        return

    holder = app.store.pending_holder(contract.token, field_key)
    if holder is not None and holder != user.id:
        await message.answer(
            app.config.text("guest.pending_other", label=html.escape(field.label))
        )
        return

    app.store.set_pending(user.id, contract, field_key)
    await message.answer(
        ui.dm_sign_prompt(
            app.config,
            template,
            field,
            app.max_length_for(template),
            app.bot_username,
        ),
        reply_markup=ForceReply(
            selective=True, input_field_placeholder=app.config.text("dm.reply_placeholder")
        ),
    )


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
            inline_mode="включён" if app.inline_mode_enabled else "выключен",
            count=len(app.templates()),
            active=app.store.active_count,
            pending=app.store.pending_count,
            uptime=app.uptime(),
        )
    )
