"""Keyboards and captions, built from texts.yaml."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import ConfigManager, FieldConfig, TemplateConfig

MAX_FIELD_KEY = 24


def _q(cfg: ConfigManager, key: str, **kwargs) -> str:
    return cfg.text(key, **kwargs)


def preview_markup(cfg: ConfigManager, token: str, index: int, total: int) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    row = [
        InlineKeyboardButton(
            text=_q(cfg, "buttons.prev"), callback_data=f"pv:{token}:{prev_index}"
        ),
        InlineKeyboardButton(
            text=_q(cfg, "buttons.pick"), callback_data=f"pk:{token}:{index}"
        ),
        InlineKeyboardButton(
            text=_q(cfg, "buttons.next"), callback_data=f"pn:{token}:{next_index}"
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def contract_markup(cfg: ConfigManager, token: str, template: TemplateConfig) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for field in template.fields.values():
        label = field.label
        button_text = _q(cfg, "buttons.sign", label=label)
        if len(button_text) > 40:
            button_text = button_text[:39] + "…"
        row.append(
            InlineKeyboardButton(text=button_text, callback_data=f"sg:{token}:{field.key}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def empty_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


def signed_buttons_markup(
    cfg: ConfigManager, token: str, template: TemplateConfig, signed: set[str]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field in template.fields.values():
        if field.key in signed:
            label = _q(cfg, "buttons.signed", label=field.label)
            callback_data = f"no:{token}"
        else:
            label = _q(cfg, "buttons.sign", label=field.label)
            callback_data = f"sg:{token}:{field.key}"
        if len(label) > 40:
            label = label[:39] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback_data)])
    rows.append(
        [InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def contract_caption(cfg: ConfigManager, template: TemplateConfig, max_length: int) -> str:
    return _q(
        cfg,
        "guest.contract_caption",
        title=html.escape(template.title),
        max_length=max_length,
    )


def sign_prompt_caption(
    cfg: ConfigManager, field: FieldConfig, user_name: str | None, max_length: int
) -> str:
    if user_name:
        return _q(
            cfg,
            "guest.sign_prompt",
            label=html.escape(field.label),
            user=html.escape(user_name),
            max_length=max_length,
        )
    return _q(
        cfg,
        "guest.sign_prompt_anonymous",
        label=html.escape(field.label),
        max_length=max_length,
    )


def done_caption(
    cfg: ConfigManager,
    template: TemplateConfig,
    self_signed: bool,
    note: str,
) -> str:
    if self_signed:
        return _q(
            cfg,
            "guest.done_self_caption",
            title=html.escape(template.title),
            note=html.escape(note),
        )
    return _q(cfg, "guest.done_caption", title=html.escape(template.title))
