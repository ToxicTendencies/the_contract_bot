"""Template file_id cache and the archive chat.

Telegram refuses to upload new files when editing an inline message, so every
blank and every finished contract must first exist on Telegram servers.  We
upload them to the owner's chat (``STORAGE_CHAT_ID``), remember the ``file_id``
and reuse it for previews and edits.

The cache holds only ``file_id`` values — no user data.
"""

from __future__ import annotations

import html
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile, Message

from .config import ConfigManager

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedFile:
    file_id: str
    mtime_ns: int
    size: int


class TemplateStorage:
    def __init__(self, config: ConfigManager):
        self.config = config
        storage = config.settings.storage
        self.chat_id = config.settings.storage_chat_id
        self.cache_path = storage.cache_dir / storage.file_ids_file
        self._cache: dict[str, CachedFile] = {}
        self._load()

    # -- cache -----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать кэш file_id: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            try:
                self._cache[str(key)] = CachedFile(
                    file_id=str(value["file_id"]),
                    mtime_ns=int(value.get("mtime_ns", 0)),
                    size=int(value.get("size", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {"file_id": entry.file_id, "mtime_ns": entry.mtime_ns, "size": entry.size}
            for key, entry in self._cache.items()
        }
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.cache_path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.cache_path)
        except OSError as exc:
            log.warning("Не удалось сохранить кэш file_id: %s", exc)

    def _stat(self, path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError:
            return 0, 0
        return stat.st_mtime_ns, stat.st_size

    def file_id(self, template_key: str) -> str | None:
        template = self.config.templates.get(template_key)
        if template is None:
            return None
        entry = self._cache.get(template_key)
        if entry is None:
            return None
        mtime_ns, size = self._stat(template.file)
        if (entry.mtime_ns, entry.size) != (mtime_ns, size):
            return None
        return entry.file_id

    def is_ready(self, template_key: str) -> bool:
        return self.file_id(template_key) is not None

    def forget(self, template_key: str) -> None:
        if self._cache.pop(template_key, None) is not None:
            self._save()

    # -- uploads ---------------------------------------------------------

    async def warmup(self, bot: Bot) -> tuple[int, int]:
        """Upload missing or changed blanks.  Returns (ready, total)."""
        templates = self.config.templates
        ready = 0
        for key in templates.items:
            if self.file_id(key):
                ready += 1
                continue
            template = templates.get(key)
            if template is None or not template.file.exists():
                continue
            try:
                message = await bot.send_photo(
                    chat_id=self.chat_id,
                    photo=FSInputFile(template.file, filename=template.file.name),
                    caption=f"Бланк: {html.escape(template.title)}",
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось загрузить бланк %s: %s", key, exc)
                continue
            file_id = _photo_file_id(message)
            if not file_id:
                log.error("Telegram не вернул file_id для бланка %s", key)
                continue
            mtime_ns, size = self._stat(template.file)
            self._cache[key] = CachedFile(file_id, mtime_ns, size)
            ready += 1
        self._save()
        return ready, len(templates)

    async def upload_signed(self, bot: Bot, data: bytes, caption: str) -> str:
        """Upload a finished contract and return its file_id (also archives it).

        ``caption`` is HTML and already contains the signers' links.
        """
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        message: Message = await bot.send_photo(
            chat_id=self.chat_id,
            photo=BufferedInputFile(data, filename="contract.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
        file_id = _photo_file_id(message)
        if not file_id:
            raise RuntimeError("Telegram не вернул file_id подписанного контракта")
        return file_id


def _photo_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    document = message.document
    if document is not None and document.file_id:
        return document.file_id
    return None
