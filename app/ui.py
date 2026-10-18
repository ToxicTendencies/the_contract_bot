"""Keyboards and captions, built from texts.yaml."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import ConfigManager, FieldConfig, TemplateConfig
from .store import Contract, Signature

MAX_BUTTON_TEXT = 40


def _q(cfg: ConfigManager, key: str, **kwargs) -> str:
    return cfg.text(key, **kwargs)


def _trim(text: str, limit: int = MAX_BUTTON_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sign_button_text(cfg: ConfigManager, label: str) -> str:
    return _trim(_q(cfg, "buttons.sign", label=label))


# ---------------------------------------------------------------------------
# keyboards (everything happens in the chat)
# ---------------------------------------------------------------------------


def preview_markup(cfg: ConfigManager, token: str, index: int, total: int) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    row = [
        InlineKeyboardButton(text=_q(cfg, "buttons.prev"), callback_data=f"pv:{token}:{prev_index}"),
        InlineKeyboardButton(text=_q(cfg, "buttons.pick"), callback_data=f"pk:{token}:{index}"),
        InlineKeyboardButton(text=_q(cfg, "buttons.next"), callback_data=f"pn:{token}:{next_index}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def contract_markup(cfg: ConfigManager, token: str, template: TemplateConfig) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for field in template.fields.values():
        row.append(
            InlineKeyboardButton(
                text=_sign_button_text(cfg, field.label),
                callback_data=f"sg:{token}:{template.key}:{field.key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signed_buttons_markup(
    cfg: ConfigManager, token: str, template: TemplateConfig, signed: set[str]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field in template.fields.values():
        if field.key in signed:
            label = _trim(_q(cfg, "buttons.signed", label=field.label))
            callback_data = f"no:{token}"
        else:
            label = _sign_button_text(cfg, field.label)
            callback_data = f"sg:{token}:{template.key}:{field.key}"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback_data)])
    rows.append([InlineKeyboardButton(text=_q(cfg, "buttons.cancel"), callback_data=f"cx:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def contract_caption(cfg: ConfigManager, template: TemplateConfig, max_length: int) -> str:
    return _q(
        cfg,
        "guest.contract_caption",
        title=html.escape(template.title),
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
    title = html.escape(template.title)
    if self_signed:
        return _q(cfg, "guest.done_self_caption", title=title, note=html.escape(note))
    return _q(cfg, "guest.done_caption", title=title)


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
    if contract.terms:
        caption += "\n\n" + _q(
            cfg,
            "archive.terms_line",
            terms=html.escape(contract.terms[:400]),
        )
    return caption
