#!/usr/bin/env python3
"""Entry point: long polling, no domain or webhook required."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.app import App
from app.config import ConfigError, ConfigManager

log = logging.getLogger("the_contract_bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def watch_config(config: ConfigManager) -> None:
    """Pick up edits in config/*.yaml without a restart."""
    while True:
        await asyncio.sleep(1.0)
        try:
            config.maybe_reload()
        except Exception:  # noqa: BLE001 - never kill the watcher
            log.exception("Ошибка наблюдения за конфигом")


async def run() -> None:
    config = ConfigManager()
    setup_logging(config.settings.log_level)
    app = App(config)

    await app.startup()

    allowed = app.dp.resolve_used_update_types()
    log.info("Подписки на апдейты: %s", ", ".join(sorted(allowed)))
    if "guest_message" not in allowed:
        log.error("guest_message нет в allowed_updates — @бот не увидит упоминания")
    if not app.guest_mode_enabled:
        log.warning(
            "Guest Mode выключен: бот ответит только в тех чатах, где он участник. "
            "Для работы по @упоминанию без добавления включи Guest Mode в @BotFather"
        )

    watcher = asyncio.create_task(watch_config(config), name="config-watcher")
    try:
        await app.dp.start_polling(app.bot, allowed_updates=allowed)
    finally:
        watcher.cancel()
        await app.shutdown()


async def check() -> int:
    """Validate the token and Guest Mode without starting polling."""
    config = ConfigManager()
    setup_logging(config.settings.log_level)
    app = App(config)
    try:
        me = await app.bot.me()
        guest = bool(getattr(me, "supports_guest_queries", False))
        inline = bool(getattr(me, "supports_inline_queries", False))
        print(f"Бот: @{me.username} (id={me.id})")
        guest_text = (
            "включён"
            if guest
            else "выключен — бот будет работать только в чатах, где он участник"
        )
        print(f"Guest Mode: {guest_text}")
        print(f"Inline mode: {'включён (боту не нужен)' if inline else 'выключен'}")
        print(f"Бланков в конфиге: {len(config.templates)}")
        for template in config.templates.ordered():
            terms = (
                f", условия до {template.terms.style.max_length} символов"
                if template.terms is not None
                else ""
            )
            print(
                f"  • {template.key}: {template.title} "
                f"({len(template.fields)} поля{terms})"
            )
        print(f"STORAGE_CHAT_ID: {config.settings.storage_chat_id}")
        print(f"OWNER_ID: {config.settings.owner_id}")
        if not guest:
            print(
                "Подсказка: чтобы @бот работал в любом чате без добавления, "
                "включи Guest Mode в @BotFather (Bot Settings → Guest Mode)."
            )
        return 0
    finally:
        await app.bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram contract bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить токен и Guest Mode, не запуская polling",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            return asyncio.run(check())
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name in {"TelegramUnauthorizedError", "UnauthorizedError"}:
            print(
                "Telegram отклонил токен. Проверь BOT_TOKEN в .env "
                "(токен мог быть отозван в @BotFather).",
                file=sys.stderr,
            )
            return 3
        if name in {"TelegramNetworkError", "ClientConnectorError", "ConnectionResetError"}:
            print(
                "Не удалось связаться с api.telegram.org. Проверь интернет, "
                "прокси или доступ к Telegram с этого сервера.",
                file=sys.stderr,
            )
            return 4
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
