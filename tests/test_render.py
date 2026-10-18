from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageChops

from app.config import Box, default_stamp, load_templates
from app.render import (
    build_canvas,
    count_graphemes,
    debug_image,
    moment_text,
    draw_stamp,
    format_date,
    layout_text,
    normalize_text,
    render_contract,
    validate_date,
    validate_signature,
    validate_terms,
)


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


def test_validate_terms_accepts_text(config):
    free = config.templates.get("free")
    check = validate_terms(
        "Исполнитель делает макет, Заказчик платит 1000 ₽ до пятницы.", free.terms
    )
    assert check.ok, check.error
    assert check.layout is not None


def test_validate_terms_rejects_empty_and_long(config):
    free = config.templates.get("free")
    assert validate_terms("   ", free.terms).error == "empty"

    too_long = validate_terms("а" * (free.terms.style.max_length + 1), free.terms)
    assert not too_long.ok
    assert too_long.error == "too_long"


def test_terms_are_drawn_on_the_blank(config):
    free = config.templates.get("free")
    without = render_contract(
        free,
        {"party1": "Иванов И.И.", "party2": "Петров П.П."},
        settings=config.settings.render,
    )
    with_terms = render_contract(
        free,
        {"party1": "Иванов И.И.", "party2": "Петров П.П."},
        terms="Оплата 1000 ₽ до пятницы.",
        settings=config.settings.render,
    )
    assert with_terms[:2] == b"\xff\xd8"
    assert without != with_terms
    image = Image.open(io.BytesIO(with_terms))
    assert image.size == (1240, 1754)


# ---------------------------------------------------------------------------
# dates, background and frame
# ---------------------------------------------------------------------------


def _custom_template(tmp_path):
    background = Path(__file__).resolve().parent.parent / "templates" / "classic.png"
    source = tmp_path / "custom.yaml"
    source.write_text(
        f"""
fonts:
  serif: {Path(__file__).resolve().parent.parent / "assets/fonts/PTSerif-Regular.ttf"}

templates:
  my_blank:
    title: "Мой бланк"
    background:
      image: {background}
      fit: cover
    frame:
      color: "#334455"
      width: 6
      inset: 30
    canvas: [1000, 1400]
    dates:
      box: [560, 40, 360, 60]
      font_size: 26
      align: right
      format: "Дата: {{date}}"
    fields:
      party1:
        label: "Сторона 1"
        box: [80, 1000, 380, 120]
      party2:
        label: "Сторона 2"
        box: [540, 1000, 380, 120]
""",
        encoding="utf-8",
    )
    templates = load_templates(extra_paths=[source], strict=True)
    return templates.get("my_blank")


def test_validate_date(config):
    dates = config.templates.get("classic").dates
    assert validate_date("18.10.2026", dates).ok
    assert validate_date("   ", dates).error == "empty"
    assert validate_date("1" * 80, dates).error == "too_long"


def test_render_draws_date(config):
    template = config.templates.get("classic")
    plain = Image.open(
        io.BytesIO(render_contract(template, {}, settings=config.settings.render))
    )
    dated = Image.open(
        io.BytesIO(
            render_contract(template, {}, date="01.02.2027", settings=config.settings.render)
        )
    )
    box = template.dates.box
    region = (box.x, box.y, box.right, box.bottom)
    difference = ImageChops.difference(plain.crop(region), dated.crop(region))
    assert difference.getbbox() is not None

    manual = Image.open(
        io.BytesIO(
            render_contract(template, {}, date="31.12.2030", settings=config.settings.render)
        )
    )
    assert manual.tobytes() != dated.tobytes()


def test_moment_text_formats_the_moment(config):
    moment = datetime(2026, 10, 18, 23, 30)
    value = moment_text(moment, config.settings.dates)
    assert value == "18.10.2026"
    assert format_date(config.templates.get("classic").dates, value) == f"Дата: {value}"


def test_moment_text_converts_utc_to_local(config):
    """Telegram присылает время в UTC: печатаем по часам машины."""
    moment = datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc)
    expected = moment.astimezone().strftime(config.settings.dates.format)
    assert moment_text(moment, config.settings.dates) == expected


def test_moment_text_without_moment_is_today(config):
    assert moment_text(None, config.settings.dates) == datetime.now().strftime(
        config.settings.dates.format
    )


def test_custom_canvas_uses_background_and_frame(tmp_path):
    template = _custom_template(tmp_path)
    assert template is not None
    image = build_canvas(template)
    assert image.size == (1000, 1400)
    # Рамка нарисована поверх фона.
    assert image.getpixel((30, 700))[:3] == (0x33, 0x44, 0x55)
    # Фон вписан в холст, а не остался белым.
    assert image.getpixel((500, 700)) != (255, 255, 255)


def test_render_custom_template_with_date(tmp_path):
    template = _custom_template(tmp_path)
    data = render_contract(
        template,
        {"party1": "Аня", "party2": "Боря"},
        date="01.02.2027",
    )
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    assert image.size == (1000, 1400)


def test_build_canvas_returns_isolated_copy(tmp_path):
    template = _custom_template(tmp_path)
    first = build_canvas(template)
    first.paste((1, 2, 3), (0, 0, 20, 20))
    second = build_canvas(template)
    assert second.getpixel((5, 5)) != (1, 2, 3)


def test_debug_image_works_for_custom_template(tmp_path):
    template = _custom_template(tmp_path)
    image = debug_image(template, draw_samples=False)
    assert image.size == (1000, 1400)


# ---------------------------------------------------------------------------
# печать
# ---------------------------------------------------------------------------


def test_stamped_contract_differs_from_plain_one():
    template = load_templates(strict=True).get("classic")
    stamp = template.stamp_rule
    assert stamp is not None
    signatures = {"party1": "Иванов И.И.", "party2": "Петров П.П."}
    plain = Image.open(io.BytesIO(render_contract(template, signatures, date="18.10.2026")))
    sealed = Image.open(
        io.BytesIO(render_contract(template, signatures, date="18.10.2026", stamped=True))
    )
    # Разница есть, и именно там, где стоит печать.
    diff = ImageChops.difference(plain.convert("RGB"), sealed.convert("RGB"))
    left, top, right, bottom = diff.getbbox()
    cx, cy = stamp.center
    assert left <= cx + stamp.radius and right >= cx - stamp.radius
    assert top <= cy + stamp.radius and bottom >= cy - stamp.radius


def test_stamp_draws_ring_and_text():
    canvas = Image.new("RGB", (600, 600), "white")
    stamp = default_stamp(600, 600)
    draw_stamp(canvas, stamp, date_text="18.10.2026")
    # Кольцо нарисовано: на радиусе пиксель не белый, внутри — белый.
    cx, cy = stamp.center
    ring = canvas.getpixel((cx + stamp.radius, cy))
    middle = canvas.getpixel((cx, cy))
    assert ring != (255, 255, 255)
    assert middle == (255, 255, 255), "середина печати не закрашена"


def test_stamp_is_not_drawn_without_flag():
    template = load_templates(strict=True).get("classic")
    data = render_contract(template, {}, date="18.10.2026")
    image = Image.open(io.BytesIO(data))
    assert image.size == template.canvas


def test_disabled_stamp_is_not_drawn():
    template = load_templates(strict=True).get("classic")
    import dataclasses

    disabled = dataclasses.replace(template, stamp=None, stamp_off=True)
    data = render_contract(disabled, {}, date="18.10.2026", stamped=True)
    image = Image.open(io.BytesIO(data))
    assert image.size == template.canvas
