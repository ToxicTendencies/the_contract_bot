"""Telegram handlers: guest queries in any chat and owner commands in DM."""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, User

from .render import validate_signature
from .store import STATE_PREVIEW, STATE_SIGNED, chat_key

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# guest router
# ---------------------------------------------------------------------------


def build_guest_router(app: "App") -> Router:
    router = Router(name="guest")

    @router.guest_message()
    async def on_guest(message: Message) -> None:
        try:
            await handle_guest(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки guest сообщения")
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

    return router


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

    if not had_mention:
        reply_author = message.reply_to_message.from_user if message.reply_to_message else None
        if reply_author is not None and reply_author.is_bot and reply_author.id != app.bot_id:
            return  # someone is talking to another bot
        await handle_signature_reply(app, message, contract, user, text)
        return

    # A mention that is also a reply to our own message and arrives while the
    # user has a pending signature is treated as that signature.
    reply_author = message.reply_to_message.from_user if message.reply_to_message else None
    replied_to_us = reply_author is not None and reply_author.id == app.bot_id
    if (
        replied_to_us
        and contract is not None
        and contract.pending is not None
        and contract.pending.user_id == user.id
        and query
        and not query.startswith("/")
    ):
        await execute_signature(app, message, contract, user, query)
        return

    if query.startswith("/"):
        command = query.split()[0].lower()
        if command.startswith("/help"):
            await app.post_guest_text(
                message,
                app.config.text(
                    "guest.help",
                    bot=app.bot_username,
                    max_length=app.settings.limits.signature_max_length,
                    ttl_minutes=app.settings.contract.ttl_minutes,
                ),
            )
            return
        query = ""

    templates = app.templates()
    if not templates:
        await app.post_guest_text(message, app.config.text("guest.no_templates"))
        return

    template = None
    if query:
        template = app.config.templates.find(query)
        if template is None:
            # Someone might just type "@bot Иванов И.И." instead of replying.
            # If they have an open slot, treat the text as the signature.
            if (
                contract is not None
                and contract.pending is not None
                and contract.pending.user_id == user.id
            ):
                await execute_signature(app, message, contract, user, query)
                return
            names = ", ".join(item.key for item in templates)
            await app.post_guest_text(
                message,
                app.config.text(
                    "guest.unknown_query",
                    query=html.escape(query),
                    templates=html.escape(names),
                    bot=app.bot_username,
                ),
            )
            return
    if template is None:
        template = templates[0]

    if contract is not None:
        # Replacing an old contract: drop it silently. Its buttons will answer
        # "контракт неактуален" because the token is gone.
        app.store.drop(contract)

    contract = app.store.create(
        chat_key=key,
        template_key=template.key,
        initiator_id=user.id,
        initiator_name=user.full_name,
        page=app.index_of(template.key),
    )
    inline_id = await app.send_preview(message, contract, template)
    if inline_id is None:
        app.store.drop(contract)


async def handle_signature_reply(
    app: "App",
    message: Message,
    contract,
    user: User,
    text: str,
) -> None:
    if contract is None:
        # A plain reply that has nothing to do with us: stay quiet.
        return
    if contract.state == STATE_SIGNED:
        return

    pending = contract.pending
    if pending is None:
        await app.post_guest_text(message, app.config.text("guest.not_pending"))
        return
    if pending.expired():
        app.store.clear_pending(contract)
        template = app.template_or_first(contract.template_key)
        if template is not None:
            await app.restore_contract_view(contract, template)
        await app.post_guest_text(message, app.config.text("guest.pending_expired"))
        return
    if pending.user_id != user.id:
        await app.post_guest_text(message, app.config.text("guest.pending_other"))
        return

    await execute_signature(app, message, contract, user, text)


async def execute_signature(
    app: "App",
    message: Message,
    contract,
    user: User,
    text: str,
) -> None:
    template = app.template_or_first(contract.template_key)
    if template is None or contract.pending is None:
        return
    field = template.fields.get(contract.pending.field_key)
    if field is None:
        app.store.clear_pending(contract)
        return

    if not text or not text.strip():
        await app.post_guest_text(message, app.config.text("guest.text_only"))
        return
    if text.lstrip().startswith("/"):
        await app.post_guest_text(message, app.config.text("guest.no_commands"))
        return

    check = validate_signature(text, field, app.settings.limits)
    if not check.ok:
        error_key = {
            "empty": "guest.empty",
            "too_long": "guest.too_long",
            "does_not_fit": "guest.does_not_fit",
        }.get(check.error or "", "guest.does_not_fit")
        await app.post_guest_text(
            message,
            app.config.text(
                error_key,
                length=check.length,
                max_length=check.max_length,
                label=html.escape(field.label),
            ),
        )
        return

    app.store.add_signature(
        contract,
        field_key=field.key,
        text=check.text,
        user_id=user.id,
        user_name=user.full_name or str(user.id),
    )

    if len(contract.signatures) >= len(template.fields):
        ok = await app.finalize(contract, template)
        if not ok:
            await app.post_guest_text(message, app.config.text("guest.upload_failed"))
        return

    await app.show_signed_progress(contract, template, field.key)


# ---------------------------------------------------------------------------
# callbacks
# ---------------------------------------------------------------------------


def _callback_expired(app: "App") -> str:
    return app.config.text("callback.expired", bot=app.bot_username)


def _stale(app: "App", query: CallbackQuery, contract) -> bool:
    if contract is None:
        return True
    if contract.state == STATE_SIGNED:
        return True
    if (
        query.inline_message_id
        and contract.inline_message_id
        and query.inline_message_id != contract.inline_message_id
    ):
        return True
    return False


async def handle_callback(app: "App", query: CallbackQuery) -> None:
    action, _, rest = (query.data or "").partition(":")
    token, _, argument = rest.partition(":")

    if action == "pv" or action == "pn" or action == "pk":
        await _handle_preview(app, query, token, argument, pick=(action == "pk"))
        return

    contract = app.store.get(token)
    if _stale(app, query, contract):
        await query.answer(_callback_expired(app))
        return

    if action == "sg":
        await _handle_sign_press(app, query, contract, argument)
        return

    if action == "cx":
        await _handle_cancel(app, query, contract)
        return

    if action == "no":
        await query.answer(app.config.text("callback.signed"))
        return

    await query.answer(app.config.text("errors.generic"))


async def _handle_preview(
    app: "App", query: CallbackQuery, token: str, argument: str, *, pick: bool
) -> None:
    contract = app.store.get(token)
    if _stale(app, query, contract):
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
    contract.state = STATE_PREVIEW
    ok = await app.show_contract(contract, template)
    await query.answer("" if ok else app.config.text("errors.no_templates"))


async def _handle_sign_press(
    app: "App", query: CallbackQuery, contract, field_key: str
) -> None:
    template = app.template_or_first(contract.template_key)
    if template is None:
        await query.answer(app.config.text("errors.no_templates"))
        return
    field = template.fields.get(field_key)
    if field is None:
        await query.answer(app.config.text("errors.generic"))
        return

    if field_key in contract.signatures:
        await query.answer(app.config.text("callback.already_signed", label=field.label))
        return

    pending = contract.pending
    if pending is not None and not pending.expired() and pending.user_id != query.from_user.id:
        await query.answer(app.config.text("callback.waiting"))
        return

    app.store.set_pending(contract, field_key, query.from_user.id)
    await query.answer()
    user_name = query.from_user.full_name or None
    await app.show_prompt(contract, template, field_key, user_name)


async def _handle_cancel(app: "App", query: CallbackQuery, contract) -> None:
    pending = contract.pending
    allowed = query.from_user.id in {
        contract.initiator_id,
        pending.user_id if pending else -1,
    }
    if not allowed:
        await query.answer(app.config.text("callback.not_yours"))
        return

    app.store.clear_pending(contract)
    template = app.template_or_first(contract.template_key)
    if template is not None:
        await app.restore_contract_view(contract, template)
    await query.answer(app.config.text("callback.cancelled"))


# ---------------------------------------------------------------------------
# DM router
# ---------------------------------------------------------------------------


def build_dm_router(app: "App") -> Router:
    router = Router(name="dm")

    @router.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def on_dm(message: Message) -> None:
        try:
            await handle_dm(app, message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения в ЛС")
            await message.answer(app.config.text("errors.generic"))

    return router


async def handle_dm(app: "App", message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    is_owner = user.id == app.settings.owner_id
    text = (message.text or "").strip()
    command = text.split()[0].lower() if text.startswith("/") else ""
    command = command.split("@", 1)[0]

    if command in {"/start", "/help"}:
        if command == "/start" and is_owner:
            ready, total = await app.ensure_templates_ready()
            if ready < total:
                await message.answer(
                    app.config.text("dm.warmup_partial", ok=ready, total=total)
                )
        await message.answer(
            app.config.text(
                "dm.greeting" if command == "/start" else "dm.help",
                bot=app.bot_username,
                max_length=app.settings.limits.signature_max_length,
                ttl_minutes=app.settings.contract.ttl_minutes,
            )
        )
        return

    if command in {"/templates", "/status", "/reload"}:
        if not is_owner:
            await message.answer(app.config.text("dm.owner_only"))
            return
        if command == "/templates":
            if not app.templates():
                await message.answer(app.config.text("dm.no_templates"))
                return
            await message.answer(
                app.config.text(
                    "dm.templates_ok", list="\n".join(app.describe_templates())
                )
            )
            return
        if command == "/reload":
            try:
                app.config.maybe_reload(force=True)
            except Exception as exc:  # noqa: BLE001
                await message.answer(app.config.text("dm.reload_failed", error=exc))
                return
            ready, total = await app.ensure_templates_ready()
            await message.answer(
                app.config.text("dm.reloaded", count=ready, total=total)
            )
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
        return

    await message.answer(
        app.config.text("dm.unknown_dm", bot=app.bot_username)
    )
