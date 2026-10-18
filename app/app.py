"""Application object: wiring, Telegram side effects and the signing pipeline."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from typing import Iterable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineQueryResultCachedPhoto,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    Message,
)

from .config import ConfigManager, TemplateConfig
from .render import RenderError, clear_caches, render_contract
from .store import (
    Contract,
    ContractStore,
    STATE_OPEN,
    STATE_PREVIEW,
    STATE_SIGNED,
    STATE_TERMS,
)
from .storage import TemplateStorage
from . import ui

log = logging.getLogger(__name__)


class App:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.settings = config.settings
        self.store = ContractStore(
            ttl_seconds=self.settings.contract.ttl_minutes * 60,
            pending_seconds=self.settings.contract.pending_input_minutes * 60,
            max_active=self.settings.contract.max_active,
        )
        self.storage = TemplateStorage(config)
        self.bot = Bot(
            token=self.settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher(storage=MemoryStorage())
        self.bot_username: str = ""
        self.bot_id: int = 0
        self.guest_mode_enabled = False
        self.started_at = time.time()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._warmup_lock = asyncio.Lock()

        # Imported lazily to avoid a circular import at module load time.
        from .handlers import build_router

        self.dp.include_router(build_router(self))
        config.on_reload(self._on_config_reload)

    # -- lifecycle -------------------------------------------------------

    async def startup(self) -> None:
        me = await self.bot.me()
        self.bot_username = me.username or ""
        self.bot_id = me.id
        self.guest_mode_enabled = bool(getattr(me, "supports_guest_queries", False))
        log.info(
            "Бот @%s (id=%s), guest mode: %s",
            self.bot_username,
            self.bot_id,
            "вкл" if self.guest_mode_enabled else "ВЫКЛ",
        )

        ready, total = await self.storage.warmup(self.bot)
        log.info("Бланков готово: %s из %s", ready, total)

        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="cleanup")

        if not self.guest_mode_enabled:
            await self.notify_owner(
                self.config.text("dm.guest_mode_disabled", bot=self.bot_username)
            )
        if ready < total:
            await self.notify_owner(
                self.config.text("dm.warmup_partial", ok=ready, total=total)
            )
        if ready == 0:
            await self.notify_owner(self.config.text("dm.storage_failed"))

    async def shutdown(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.bot.session.close()

    async def notify_owner(self, text: str) -> bool:
        try:
            await self.bot.send_message(self.settings.owner_id, text)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось написать владельцу: %s", exc)
            return False

    def _on_config_reload(self) -> None:
        clear_caches()
        self.settings = self.config.settings
        self.store.ttl_seconds = self.settings.contract.ttl_minutes * 60
        self.store.pending_seconds = self.settings.contract.pending_input_minutes * 60
        self.store.max_active = self.settings.contract.max_active
        loop = asyncio.get_running_loop()
        loop.create_task(self._reload_templates(), name="reload-templates")

    async def _reload_templates(self) -> None:
        async with self._warmup_lock:
            try:
                ready, total = await self.storage.warmup(self.bot)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось перезалить бланки после перезагрузки конфига")
                return
            log.info("После перезагрузки конфига готово %s из %s бланков", ready, total)

    async def ensure_templates_ready(self) -> tuple[int, int]:
        async with self._warmup_lock:
            return await self.storage.warmup(self.bot)

    # -- template helpers -------------------------------------------------

    def templates(self) -> list[TemplateConfig]:
        return self.config.templates.ordered()

    def template_or_first(self, key: str | None) -> TemplateConfig | None:
        templates = self.templates()
        if not templates:
            return None
        if key:
            found = self.config.templates.get(key)
            if found is not None:
                return found
        return templates[0]

    def index_of(self, key: str | None) -> int:
        templates = self.templates()
        for index, template in enumerate(templates):
            if template.key == key:
                return index
        return 0

    def clamp_index(self, index: int | None, total: int | None = None) -> int:
        total = total if total is not None else len(self.templates())
        if total <= 0:
            return 0
        if index is None:
            return 0
        return index % total

    def max_length_for(self, template: TemplateConfig) -> int:
        cap = self.settings.limits.signature_hard_cap
        values = [min(field.style.max_length, cap) for field in template.fields.values()]
        return min(values) if values else min(self.settings.limits.signature_max_length, cap)

    # -- guest replies ----------------------------------------------------

    async def post_text(self, message: Message, text: str) -> None:
        """Reply with text: a guest answer or a normal chat message."""
        if message.guest_query_id:
            result = InlineQueryResultArticle(
                id=secrets.token_hex(8),
                title="Сообщение",
                input_message_content=InputTextMessageContent(
                    message_text=text, parse_mode=ParseMode.HTML
                ),
            )
            try:
                await message.answer_guest_query(result=result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось ответить на guest query: %s", exc)
            return
        try:
            await message.reply(text, parse_mode=ParseMode.HTML)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось ответить в чат: %s", exc)

    async def reply_with_photo(
        self,
        message: Message,
        contract: Contract,
        template: TemplateConfig,
        caption: str,
        markup: InlineKeyboardMarkup | None,
        *,
        file_id: str | None = None,
    ) -> bool:
        """Send the blank with buttons and remember how to edit it later.

        Guest bots answer the summon (``answerGuestQuery``), member bots send a
        normal photo message.  Either way the contract remembers the handle:
        ``inline_message_id`` or ``chat_id`` + ``message_id``.
        """
        file_id = file_id or self.storage.file_id(template.key)
        if not file_id:
            await self.post_text(message, self.config.text("guest.no_templates"))
            return False

        if message.guest_query_id:
            result = InlineQueryResultCachedPhoto(
                id=secrets.token_hex(8),
                photo_file_id=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            try:
                sent = await message.answer_guest_query(result=result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось отправить бланк в чат: %s", exc)
                return False
            inline_id = getattr(sent, "inline_message_id", None)
            if not inline_id:
                log.warning("Guest-ответ пришёл без inline_message_id")
                return False
            contract.inline_message_id = inline_id
            contract.touch()
            return True

        try:
            sent = await message.answer_photo(
                photo=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить бланк в чат: %s", exc)
            return False
        chat = getattr(sent, "chat", None)
        if chat is not None:
            contract.chat_id = chat.id
        contract.message_id = getattr(sent, "message_id", None)
        contract.touch()
        return True

    async def send_preview(
        self, message: Message, contract: Contract, template: TemplateConfig
    ) -> bool:
        """Show the blank with the ◀ Выбрать ▶ carousel."""
        total = max(1, len(self.templates()))
        index = self.clamp_index(contract.page, total)
        contract.page = index
        return await self.reply_with_photo(
            message,
            contract,
            template,
            ui.preview_caption(self.config, template, index, total),
            ui.preview_markup(self.config, contract.token, index, total),
        )

    async def send_terms_prompt(
        self, message: Message, contract: Contract, template: TemplateConfig
    ) -> bool:
        """Post the blank with the "send me the terms" caption."""
        if template.terms is None:
            return False
        contract.state = STATE_TERMS
        contract.touch()
        return await self.reply_with_photo(
            message,
            contract,
            template,
            ui.terms_prompt(self.config, template, template.terms_max_length),
            None,
        )

    async def edit_terms_prompt(
        self, contract: Contract, template: TemplateConfig
    ) -> bool:
        """Turn the current blank into the terms prompt (carousel pick)."""
        if template.terms is None:
            return False
        contract.state = STATE_TERMS
        return await self.edit_contract(
            contract,
            template,
            ui.terms_prompt(self.config, template, template.terms_max_length),
            None,
        )

    async def show_terms_contract(
        self, contract: Contract, template: TemplateConfig
    ) -> bool:
        """Draw the free-form terms on the blank and show the side buttons."""
        if template.terms is None or not contract.terms:
            return False
        try:
            data = render_contract(
                template,
                {},
                terms=contract.terms,
                settings=self.settings.render,
            )
        except RenderError:
            log.exception("Не удалось нарисовать условия контракта %s", contract.token)
            return False
        try:
            file_id = await self.storage.upload_image(
                self.bot,
                data,
                f"Бланк: {html.escape(template.title)}",
                filename="blank.jpg",
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось загрузить бланк с условиями %s", contract.token)
            return False

        contract.blank_file_id = file_id
        contract.state = STATE_OPEN
        return await self.edit_contract(
            contract,
            template,
            ui.contract_caption(
                self.config, template, self.max_length_for(template)
            ),
            ui.signed_buttons_markup(
                self.config, contract.token, template, set(contract.signatures)
            ),
        )

    # -- editing ----------------------------------------------------------

    async def edit_contract(
        self,
        contract: Contract,
        template: TemplateConfig,
        caption: str,
        markup: InlineKeyboardMarkup | None,
    ) -> bool:
        file_id = contract.blank_file_id or self.storage.file_id(template.key)
        if not file_id:
            log.warning("Нет file_id для бланка %s", template.key)
            return False
        if not contract.has_message:
            log.warning("Контракт %s не привязан к сообщению", contract.token)
            return False
        target: dict[str, object] = {"inline_message_id": contract.inline_message_id}
        if not contract.inline_message_id:
            target = {"chat_id": contract.chat_id, "message_id": contract.message_id}
        try:
            await self.bot.edit_message_media(
                **target,
                media=InputMediaPhoto(
                    media=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("Не удалось отредактировать сообщение %s: %s", contract.token, exc)
            return False
        contract.touch()
        return True

    async def show_preview_page(self, contract: Contract, index: int) -> bool:
        templates = self.templates()
        if not templates:
            return False
        total = len(templates)
        index = self.clamp_index(index, total)
        template = templates[index]
        contract.page = index
        return await self.edit_contract(
            contract,
            template,
            ui.preview_caption(self.config, template, index, total),
            ui.preview_markup(self.config, contract.token, index, total),
        )

    async def show_contract(self, contract: Contract, template: TemplateConfig) -> bool:
        contract.state = STATE_OPEN
        return await self.edit_contract(
            contract,
            template,
            ui.contract_caption(
                self.config, template, self.max_length_for(template)
            ),
            ui.contract_markup(self.config, contract.token, template),
        )

    async def show_prompt(
        self,
        contract: Contract,
        template: TemplateConfig,
        field_key: str,
        user_name: str | None,
    ) -> bool:
        field = template.fields.get(field_key)
        if field is None:
            return False
        contract.state = STATE_OPEN
        return await self.edit_contract(
            contract,
            template,
            ui.sign_prompt_caption(
                self.config, field, user_name, self.max_length_for(template)
            ),
            # Уже подписанные стороны остаются с галочкой: иначе второму
            # участнику кажется, что прогресс сбросился.
            ui.signed_buttons_markup(
                self.config, contract.token, template, set(contract.signatures)
            ),
        )

    async def show_signed_progress(
        self, contract: Contract, template: TemplateConfig, field_key: str
    ) -> bool:
        signed = set(contract.signatures)
        remaining = [field.label for key, field in template.fields.items() if key not in signed]
        field = template.fields.get(field_key)
        caption = self.config.text(
            "guest.one_signed",
            label=html.escape(field.label if field else field_key),
            remaining=html.escape(", ".join(remaining)),
        )
        markup = ui.signed_buttons_markup(self.config, contract.token, template, signed)
        return await self.edit_contract(contract, template, caption, markup)

    async def mark_expired(self, contract: Contract) -> None:
        template = self.template_or_first(contract.template_key)
        if template is None:
            return
        await self.edit_contract(
            contract,
            template,
            self.config.text("guest.contract_expired", bot=self.bot_username),
            ui.empty_markup(),
        )

    async def restore_contract_view(self, contract: Contract, template: TemplateConfig) -> None:
        if contract.state == STATE_PREVIEW:
            await self.show_preview_page(contract, contract.page)
        else:
            await self.edit_contract(
                contract,
                template,
                ui.contract_caption(
                    self.config, template, self.max_length_for(template)
                ),
                ui.signed_buttons_markup(
                    self.config, contract.token, template, set(contract.signatures)
                ),
            )

    # -- finishing ---------------------------------------------------------

    async def finalize(self, contract: Contract, template: TemplateConfig) -> bool:
        signature_texts = {key: sig.text for key, sig in contract.signatures.items()}
        self_signed = contract.same_signers()
        note = self.config.text("guest.self_sign_note") if self_signed else None
        try:
            data = render_contract(
                template,
                signature_texts,
                self_sign_note=note,
                terms=contract.terms or None,
                settings=self.settings.render,
            )
        except RenderError as exc:
            log.error("Ошибка рендера контракта %s: %s", contract.token, exc)
            return False
        except Exception:  # noqa: BLE001
            log.exception("Неожиданная ошибка рендера контракта %s", contract.token)
            return False

        archive_caption = ui.owner_caption(self.config, template, contract)
        try:
            file_id = await self.storage.upload_signed(self.bot, data, archive_caption)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось загрузить подписанный контракт %s", contract.token)
            return False

        caption = ui.done_caption(
            self.config,
            template,
            self_signed,
            note or "",
        )
        if not contract.has_message:
            log.warning("Контракт %s подписан, но сообщение уже недоступно", contract.token)
            self.store.drop(contract)
            return True
        if contract.inline_message_id:
            target: dict[str, object] = {"inline_message_id": contract.inline_message_id}
        else:
            target = {"chat_id": contract.chat_id, "message_id": contract.message_id}
        try:
            await self.bot.edit_message_media(
                **target,
                media=InputMediaPhoto(
                    media=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=ui.empty_markup(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось показать финал для %s: %s", contract.token, exc)

        # Keep the finished contract in RAM until TTL so stale buttons answer
        # "уже подписан" instead of silently starting a new one. Only signature
        # text is stored — the image is rendered on demand.
        contract.state = STATE_SIGNED
        contract.touch()
        return True

    # -- housekeeping ------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                interval = max(5, self.settings.contract.cleanup_interval_seconds)
                await asyncio.sleep(interval)
                await self.cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в задаче очистки")

    async def cleanup_once(self) -> None:
        now = time.time()
        expired = self.store.expire_pending(now)
        touched = {pending.token for pending in expired}
        for token in touched:
            contract = self.store.get(token)
            if contract is None or contract.chat_key is None:
                continue
            template = self.template_or_first(contract.template_key)
            if template is not None and contract.state != STATE_SIGNED:
                await self.restore_contract_view(contract, template)
        for contract in self.store.pop_stale(now):
            if contract.state == STATE_SIGNED:
                continue  # finished long ago, forget it silently
            if contract.inline_message_id:
                await self.mark_expired(contract)

    # -- misc -------------------------------------------------------------

    def describe_templates(self) -> Iterable[str]:
        for template in self.templates():
            status = (
                self.config.text("dm.template_status_ready")
                if self.storage.is_ready(template.key)
                else self.config.text("dm.template_status_missing")
            )
            yield self.config.text(
                "dm.template_line", title=template.title, key=template.key, status=status
            )

    def uptime(self) -> str:
        seconds = int(time.time() - self.started_at)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours} ч {minutes} мин"
        if minutes:
            return f"{minutes} мин {secs} с"
        return f"{secs} с"
