from __future__ import annotations

import io

from PIL import Image

from app.config import Box, Limits
from app.render import (
    count_graphemes,
    layout_text,
    normalize_text,
    render_contract,
    validate_signature,
)
from app.config import load_templates


def test_grapheme_counting():
    assert count_graphemes("Иванов И.И.") == 11
    assert count_graphemes("❤️") == 1
    assert count_graphemes("👨‍👩‍👧‍👦") == 1
    assert count_graphemes("🐐🔥") == 2


def test_normalize_text():
    assert normalize_text("  Иван   Иванов \n") == "Иван Иванов"
    assert normalize_text("текст\x00с\u0007символами") == "текстссимволами"
    assert normalize_text("") == ""


def test_layout_finds_size_and_wraps(config):
    template = config.templates.get("classic")
    field = template.fields["party1"]
    layout = layout_text("ООО «Рога и Копыта»", field.box, field.style)
    assert layout is not None
    assert 8 <= layout.font_size <= field.style.font_size
    assert layout.width <= field.box.w + 0.5


def test_validate_rejects_long_text(config):
    template = config.templates.get("classic")
    field = template.fields["party1"]
    limits = config.settings.limits
    check = validate_signature("а" * (field.style.max_length + 1), field, limits)
    assert not check.ok
    assert check.error == "too_long"
    assert check.max_length == min(field.style.max_length, limits.signature_hard_cap)


def test_validate_rejects_empty(config):
    template = config.templates.get("classic")
    check = validate_signature("   ", template.fields["party1"], config.settings.limits)
    assert not check.ok
    assert check.error == "empty"


def test_validate_accepts_emoji(config):
    template = config.templates.get("minimal")
    check = validate_signature("Анна ❤️🔥", template.fields["party1"], config.settings.limits)
    assert check.ok, check.error


def test_render_produces_jpeg(config):
    template = config.templates.get("classic")
    data = render_contract(
        template,
        {"party1": "Иванов И.И.", "party2": "Петров П.П."},
        self_sign_note=None,
        settings=config.settings.render,
    )
    assert data[:2] == b"\xff\xd8"
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    assert image.size == (1240, 1754)


def test_render_self_sign_note(config):
    template = config.templates.get("classic")
    plain = render_contract(
        template, {"party1": "Иванов И.И.", "party2": "Иванов И.И."}, self_sign_note=None
    )
    noted = render_contract(
        template,
        {"party1": "Иванов И.И.", "party2": "Иванов И.И."},
        self_sign_note=config.text("guest.self_sign_note"),
    )
    assert plain != noted


def test_layout_rejects_huge_text():
    from app.config import TextStyle

    templates = load_templates()
    base = templates.get("classic").fields["party1"]
    style = TextStyle(
        font=base.style.font,
        emoji_font=base.style.emoji_font,
        font_size=base.style.font_size,
        color=base.style.color,
        align=base.style.align,
        valign=base.style.valign,
        max_length=base.style.max_length,
        max_lines=1,
        min_font_scale=0.6,
        line_spacing=base.style.line_spacing,
    )
    tiny = Box(0, 0, 80, 40)
    assert layout_text("Очень длинная подпись", tiny, style) is None
