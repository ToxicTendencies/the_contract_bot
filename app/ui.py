"""Keyboards and captions, built from texts.yaml."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import ConfigManager, FieldConfig, TemplateConfig
from .store import Contract, Signature

MAX_BUTTON_TEXT = 40

# Telegram: подпись к сообщению/картинке — не больше 1024 символов.
CAPTION_LIMIT = 1000

# Меньше этого цитату в подпись не вставляем: обрезанная до пары слов
# цитата только путает.
TERMS_MIN_QUOTE = 80


def _q(cfg: ConfigManager, key: str, **kwargs) -> str:
    return cfg.text(key, **kwargs)


def _trim(text: str, limit: int = MAX_BUTTON_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sign_button_text(cfg: ConfigManager, label: str) -> str:
    return _trim(_q(cfg, "buttons.sign", label=label))


# ---------------------------------------------------------------------------
# free-form terms: always shown as a quote inside the message
# ---------------------------------------------------------------------------

def terms_quote(text: str, *, budget: int | None = None) -> str:
    """Текст условий цитатой: Telegram рисует ``<blockquote>`` в HTML."""
    if not text:
        return ""
    escaped = html.escape(text)
    if budget is not None and budget > 0 and len(escaped) > budget:
        cut = escaped[: max(0, budget - 1)]
        # не рвать HTML-сущность (&amp; и т.п.) посередине
        amp = cut.rfind("&")
        if amp != -1 and cut.find(";", amp) == -1:
            cut = cut[:amp]
        escaped = cut.rstrip() + "…"
    return f"<blockquote>{escaped}</blockquote>"


def with_terms(
    cfg: ConfigManager,
    caption: str,
    terms: str,
    *,
    author: str = "",
    limit: int = CAPTION_LIMIT,
) -> str:
    """Дописать к подписи условия цитатой (и автора редакции)."""
    if not terms:
        return caption
    budget = limit - len(caption) - 2
    if author:
        budget -= 80
    if budget < TERMS_MIN_QUOTE:
        return caption
    caption = caption.rstrip() + "\n\n" + terms_quote(terms, budget=budget)
    if author:
        caption += "\n" + _q(cfg, "guest.terms_author", name=html.escape(author))
    return caption


# ---------------------------------------------------------------------------
# keyboards (everything happens in the chat)
# ---------------------------------------------------------------------------


def _column(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Каждая кнопка — на своей строке.

    Telegram растягивает строку на всю ширину клавиатуры и делит её между
    кнопками: две кнопки в строке — половинки, одна — вся ширина. Как только
    число кнопок в строке меняется (превью → бланк → подписано), кнопки
    «скачут» по ширине. Поэтому в сценарии контракта все строки — из одной
    кнопки: ширина не меняется никогда.
    """
    return [[button] for button in buttons]


def date_button(cfg: ConfigManager, token: str, date_text: str) -> InlineKeyboardButton | None:
    """Кнопка «Дата: 18.10.2026» — открывает ручной ввод."""
    if not date_text:
        return None
    label = _trim(_q(cfg, "buttons.date", date=date_text), 60)
    return InlineKeyboardButton(text=label, callback_data=f"dt:{token}")


def preview_markup(
    cfg: ConfigManager,
    token: str,
    index: int,
    total: int,
    *,
    date: str = "",
) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    buttons = [
        InlineKeyboardButton(text=_q(cfg, "buttons.prev"), callback_data=f"pv:{token}:{prev_index}"),
        InlineKeyboardButton(text=_q(cfg, "buttons.pick"), callback_data=f"pk:{token}:{index}"),
        InlineKeyboardButton(text=_q(cfg, "buttons.next"), callback_data=f"pn:{token}:{next_index}"),
    ]
    date_btn = date_button(cfg, token, date)
    if date_btn is not None:
        buttons.append(date_btn)
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def contract_markup(
    cfg: ConfigManager,
    token: str,
    template: TemplateConfig,
    *,
    date: str = "",
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=_sign_button_text(cfg, field.label),
            callback_data=f"sg:{token}:{template.key}:{field.key}",
        )
        for field in template.fields.values()
    ]
    date_btn = date_button(cfg, token, date)
    if date_btn is not None:
        buttons.append(date_btn)
    buttons.append(
        InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")
    )
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def terms_prompt_markup(
    cfg: ConfigManager, token: str, *, date: str = ""
) -> InlineKeyboardMarkup:
    """Пока условий нет, их может предложить любой — кнопка видна сразу."""
    buttons = [
        InlineKeyboardButton(
            text=_trim(_q(cfg, "buttons.propose_terms"), 60),
            callback_data=f"ds:{token}",
        )
    ]
    date_btn = date_button(cfg, token, date)
    if date_btn is not None:
        buttons.append(date_btn)
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def signed_buttons_markup(
    cfg: ConfigManager,
    token: str,
    template: TemplateConfig,
    signed: set[str],
    *,
    dispute: bool = False,
    date: str = "",
) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    for field in template.fields.values():
        if field.key in signed:
            label = _trim(_q(cfg, "buttons.signed", label=field.label))
            callback_data = f"no:{token}"
        else:
            label = _sign_button_text(cfg, field.label)
            callback_data = f"sg:{token}:{template.key}:{field.key}"
        buttons.append(InlineKeyboardButton(text=label, callback_data=callback_data))
    if dispute:
        buttons.append(
            InlineKeyboardButton(
                text=_trim(_q(cfg, "buttons.dispute"), 60),
                callback_data=f"ds:{token}",
            )
        )
    date_btn = date_button(cfg, token, date)
    if date_btn is not None:
        buttons.append(date_btn)
    buttons.append(
        InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")
    )
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def dispute_markup(cfg: ConfigManager, token: str) -> InlineKeyboardMarkup:
    """Пока человек печатает новую редакцию, подписывать нечего."""
    buttons = [
        InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")
    ]
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def date_markup(cfg: ConfigManager, token: str) -> InlineKeyboardMarkup:
    """Пока человек вписывает дату, остальные действия отменены."""
    buttons = [
        InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")
    ]
    return InlineKeyboardMarkup(inline_keyboard=_column(buttons))


def empty_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


def preview_caption(cfg: ConfigManager, template: TemplateConfig, index: int, total: int) -> str:
    data = {
        "title": html.escape(template.title),
        "description": html.escape(template.description),
        "index": index + 1,
        "total": total,
    }
    if template.description:
        return _q(cfg, "guest.preview_caption", **data)
    return _q(cfg, "guest.preview_caption_no_description", **data)


def contract_caption(
    cfg: ConfigManager,
    template: TemplateConfig,
    max_length: int,
    *,
    terms: str = "",
    author: str = "",
) -> str:
    caption = _q(
        cfg,
        "guest.contract_caption",
        title=html.escape(template.title),
        max_length=max_length,
    )
    return with_terms(cfg, caption, terms, author=author)


def date_prompt(
    cfg: ConfigManager,
    template: TemplateConfig,
    date: str,
    max_length: int,
) -> str:
    return _q(
        cfg,
        "guest.date_prompt",
        title=html.escape(template.title),
        date=html.escape(date),
        max_length=max_length,
    )


def terms_prompt(cfg: ConfigManager, template: TemplateConfig, max_length: int) -> str:
    return _q(
        cfg,
        "guest.terms_prompt",
        title=html.escape(template.title),
        max_length=max_length,
    )


def sign_prompt_caption(
    cfg: ConfigManager,
    field: FieldConfig,
    user_name: str | None,
    max_length: int,
    *,
    terms: str = "",
    author: str = "",
) -> str:
    if user_name:
        caption = _q(
            cfg,
            "guest.sign_prompt",
            label=html.escape(field.label),
            user=html.escape(user_name),
            max_length=max_length,
        )
    else:
        caption = _q(
            cfg,
            "guest.sign_prompt_anonymous",
            label=html.escape(field.label),
            max_length=max_length,
        )
    return with_terms(cfg, caption, terms, author=author)


def dispute_prompt(
    cfg: ConfigManager,
    template: TemplateConfig,
    user_name: str | None,
    max_length: int,
    *,
    terms: str = "",
    contract_author: str = "",
) -> str:
    """Просьба прислать свою редакцию условий."""
    caption = _q(
        cfg,
        "guest.dispute_prompt",
        title=html.escape(template.title),
        user=html.escape(user_name or ""),
        max_length=max_length,
    )
    return with_terms(cfg, caption, terms, author=contract_author)


def done_caption(
    cfg: ConfigManager,
    template: TemplateConfig,
    self_signed: bool,
    note: str,
    *,
    terms: str = "",
    author: str = "",
) -> str:
    title = html.escape(template.title)
    if self_signed:
        caption = _q(cfg, "guest.done_self_caption", title=title, note=html.escape(note))
    else:
        caption = _q(cfg, "guest.done_caption", title=title)
    return with_terms(cfg, caption, terms, author=author)


# ---------------------------------------------------------------------------
# owner archive with profile links
# ---------------------------------------------------------------------------


def user_link(signature: Signature) -> str:
    """Clickable name: username when available, otherwise tg://user?id=."""
    name = html.escape(signature.user_name or signature.user_username or str(signature.user_id))
    if signature.user_username:
        return f'<a href="https://t.me/{html.escape(signature.user_username)}">{name}</a>'
    return f'<a href="tg://user?id={signature.user_id}">{name}</a>'


def owner_caption(
    cfg: ConfigManager,
    template: TemplateConfig,
    contract: Contract,
    *,
    source: str = "",
) -> str:
    """Caption for the copy archived in the owner's chat."""
    lines: list[str] = []
    for field_key, signature in contract.signatures.items():
        field = template.fields.get(field_key)
        label = field.label if field else field_key
        lines.append(
            _q(
                cfg,
                "archive.signer_line",
                label=html.escape(label),
                link=user_link(signature),
                signature=html.escape(signature.text),
            )
        )
    signers = "\n".join(lines) if lines else _q(cfg, "archive.no_signers")
    caption = _q(
        cfg,
        "archive.caption",
        title=html.escape(template.title),
        signers=signers,
        source=html.escape(source),
    )
    if contract.date_text:
        caption = caption.rstrip() + "\n" + _q(
            cfg, "archive.date_line", date=html.escape(contract.date_text)
        )
    if contract.terms:
        caption = caption.rstrip() + "\n\n" + _q(
            cfg,
            "archive.terms_line",
            terms=html.escape(contract.terms[:400]),
        )
        if contract.terms_author_name:
            caption += "\n" + _q(
                cfg,
                "archive.terms_author_line",
                name=html.escape(contract.terms_author_name),
            )
    return caption
