#!/usr/bin/env python3
"""Offline debugging for blanks and signatures — no token, no Telegram.

    python generate_samples.py --make-blanks     # create demo blanks
    python generate_samples.py --debug-boxes     # boxes + coordinates on blanks
    python generate_samples.py --check           # validate config
    python generate_samples.py                   # render sample contracts
    python generate_samples.py --list            # show loaded templates
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

from app.config import (
    CONFIG_DIR,
    DEFAULT_FONT,
    ConfigError,
    Limits,
    RenderSettings,
    TemplateConfig,
    load_templates,
    required_text_keys,
)
from app.render import (
    RenderError,
    count_graphemes,
    debug_image,
    layout_text,
    load_template_image,
    render_contract,
    split_graphemes,
)

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = BASE_DIR / "samples_output"


# ---------------------------------------------------------------------------
# demo blanks
# ---------------------------------------------------------------------------

CLASSIC_BODY = (
    "Настоящий договор заключён между сторонами, указанными ниже, и определяет "
    "порядок оказания услуг, сроки, стоимость и ответственность сторон.",

    "Исполнитель обязуется оказать услуги в полном объёме и в согласованный "
    "срок, а Заказчик обязуется принять результат и оплатить его.",
    "Все споры решаются путём переговоров. Договор вступает в силу с момента "
    "подписания обеими сторонами и действует до полного исполнения обязательств.",
    "Стороны подтверждают, что ознакомлены с условиями и принимают их без "
    "замечаний. Изменения оформляются письменно и подписываются обеими сторонами.",
)

MINIMAL_BODY = (
    "Стороны договариваются о нижеследующем: работа выполняется качественно, "
    "в срок и на условиях, согласованных в переписке.",
    "Оплата производится после приёмки результата. Претензии принимаются "
    "в течение семи дней с момента сдачи работы.",
    "Соглашение действительно с даты подписания. Любые правки — только "
    "с письменного согласия обеих сторон.",
)

DARK_BODY = (
    "Предмет соглашения: передача результата работ и прав на его использование.",
    "Стороны действуют добросовестно, соблюдают конфиденциальность и сроки.",
    "Подпись на бланке подтверждает согласие с условиями без дополнительных "
    "оговорок.",
)

TERMS_SAMPLE = (
    "Исполнитель обязуется подготовить и передать Заказчику макет договора "
    "до 30 ноября, а Заказчик — принять работу и оплатить её в течение "
    "пяти рабочих дней после приёмки. Правки согласуются в переписке."
)

BUTTON_CASES = ("default", "long", "self_signed")


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.truetype(str(DEFAULT_FONT), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _paragraph(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    width: int,
    font,
    fill,
    line_gap: float = 1.55,
) -> int:
    x, y = xy
    step = int(font.size * line_gap)
    for line in _wrap(draw, text, font, width):
        draw.text((x, y), line, font=font, fill=fill)
        y += step
    return y


def _centered(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill, width: int = 1240) -> None:
    text_width = draw.textlength(text, font=font)
    draw.text(((width - text_width) / 2, y), text, font=font, fill=fill)


def make_classic(path: Path, fonts: dict[str, Path], force: bool) -> bool:
    if path.exists() and not force:
        return False
    width, height = 1240, 1754
    ink = "#1b2a4a"
    muted = "#5c6880"
    image = Image.new("RGB", (width, height), "#fdfbf7")
    draw = ImageDraw.Draw(image)

    draw.rectangle([56, 56, width - 56, height - 56], outline=ink, width=3)
    draw.rectangle([70, 70, width - 70, height - 70], outline=ink, width=1)

    _centered(draw, "ДОГОВОР", 140, _font(fonts["display"], 72), ink, width)
    _centered(draw, "об оказании услуг", 226, _font(fonts["serif"], 32), muted, width)
    draw.line([(420, 290), (820, 290)], fill=ink, width=2)

    body_font = _font(fonts["serif"], 30)
    y = 360
    for paragraph in CLASSIC_BODY:
        y = _paragraph(draw, paragraph, (110, y), width - 220, body_font, ink) + 34

    label_font = _font(fonts["serif"], 26)
    hint_font = _font(fonts["serif"], 20)
    _centered(draw, "ПОДПИСИ СТОРОН", 1180, _font(fonts["display"], 30), ink, width)

    for x, label in ((78, "Сторона 1"), (662, "Сторона 2")):
        draw.text((x, 1232), label, font=label_font, fill=muted)
        draw.line([(x, 1420), (x + 500, 1420)], fill=ink, width=2)
        draw.text((x, 1432), "подпись", font=hint_font, fill=muted)

    image.save(path)
    return True


def make_minimal(path: Path, fonts: dict[str, Path], force: bool) -> bool:
    if path.exists() and not force:
        return False
    width, height = 1240, 1754
    ink = "#20232a"
    accent = "#7a6a54"
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    draw.text((110, 150), "СОГЛАШЕНИЕ", font=_font(fonts["display"], 96), fill=ink)
    draw.line([(110, 292), (1130, 292)], fill=accent, width=2)
    draw.text(
        (110, 316),
        "простое соглашение сторон",
        font=_font(fonts["serif"], 30),
        fill=accent,
    )

    body_font = _font(fonts["serif"], 31)
    y = 430
    for paragraph in MINIMAL_BODY:
        y = _paragraph(draw, paragraph, (110, y), width - 220, body_font, ink, 1.7) + 48

    label_font = _font(fonts["serif"], 25)
    hint_font = _font(fonts["serif"], 20)
    for x, label in ((90, "Сторона 1"), (670, "Сторона 2")):
        draw.text((x, 1112), label, font=label_font, fill=accent)
        draw.line([(x, 1300), (x + 480, 1300)], fill=ink, width=2)
        draw.text((x, 1312), "подпись", font=hint_font, fill=accent)

    image.save(path)
    return True


def make_dark(path: Path, fonts: dict[str, Path], force: bool) -> bool:
    if path.exists() and not force:
        return False
    width, height = 1240, 1754
    background = "#12151d"
    gold = "#d4a24c"
    text_color = "#cfd6e4"
    muted = "#8b93a7"
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    draw.rectangle([50, 50, width - 50, height - 50], outline=gold, width=2)
    draw.text((100, 140), "КОНТРАКТ", font=_font(fonts["display"], 80), fill=gold)
    draw.line([(100, 268), (1140, 268)], fill=gold, width=1)
    draw.text(
        (100, 292), "без лишних формальностей", font=_font(fonts["serif"], 29), fill=muted
    )

    body_font = _font(fonts["serif"], 31)
    y = 400
    for paragraph in DARK_BODY:
        y = _paragraph(draw, paragraph, (100, y), width - 200, body_font, text_color, 1.7) + 46

    label_font = _font(fonts["serif"], 26)
    hint_font = _font(fonts["serif"], 20)
    for x, label in ((80, "Сторона 1"), (660, "Сторона 2")):
        draw.text((x, 1222), label, font=label_font, fill=muted)
        draw.line([(x, 1420), (x + 500, 1420)], fill=gold, width=2)
        draw.text((x, 1432), "подпись", font=hint_font, fill=muted)

    draw.text(
        (80, 1600),
        "подписано в Telegram",
        font=_font(fonts["serif"], 22),
        fill=muted,
    )
    image.save(path)
    return True


MEDIEVAL_BODY = (
    "Сим документом стороны, поименованные ниже, заключают соглашение о добрых "
    "делах, честном слове и своевременной плате.",
    "Первая сторона обязуется исполнить работу в срок и без лукавства, вторая — "
    "принять её и вознаградить по договорённости.",
    "Кто слово не сдержит, тот ответит по совести. Споры решаются переговорами, "
    "а не поединком — времена нынче спокойные.",
    "Договор скрепляется подписями обеих сторон и вступает в силу с сего дня.",
)


def make_medieval(path: Path, fonts: dict[str, Path], force: bool) -> bool:
    if path.exists() and not force:
        return False
    import random

    width, height = 1240, 1754
    parchment = "#e9d7b4"
    ink = "#3b2415"
    accent = "#6b4a2f"
    seal = "#8b2f1d"

    image = Image.new("RGB", (width, height), parchment)
    draw = ImageDraw.Draw(image)

    # Age spots: deterministic so repeated runs look identical.
    rng = random.Random(42)
    for _ in range(2600):
        x = rng.randint(0, width - 1)
        y = rng.randint(0, height - 1)
        tone = rng.choice(("#dfcaa2", "#e3d0aa", "#d8c096"))
        draw.point((x, y), fill=tone)

    # Ornate border: thick outer, thin inner, corner squares.
    draw.rectangle([46, 46, width - 46, height - 46], outline=ink, width=5)
    draw.rectangle([64, 64, width - 64, height - 64], outline=ink, width=2)
    for cx, cy in ((46, 46), (width - 46, 46), (46, height - 46), (width - 46, height - 46)):
        draw.rectangle([cx - 12, cy - 12, cx + 12, cy + 12], fill=ink)

    title_font = _font(fonts["serif_bold"], 82)
    _centered(draw, "ДОГОВОРЪ", 132, title_font, ink, width)
    _centered(
        draw,
        "о добрых делах и честном слове",
        240,
        _font(fonts["serif"], 32),
        accent,
        width,
    )
    draw.line([(380, 306), (860, 306)], fill=ink, width=2)

    body_font = _font(fonts["serif"], 30)
    y = 372
    for paragraph in MEDIEVAL_BODY:
        y = _paragraph(draw, paragraph, (112, y), width - 224, body_font, ink, 1.62) + 36

    label_font = _font(fonts["serif"], 27)
    hint_font = _font(fonts["serif"], 20)
    for x, label in ((80, "Сторона 1"), (660, "Сторона 2")):
        draw.text((x, 1214), label, font=label_font, fill=accent)
        draw.line([(x, 1410), (x + 500, 1410)], fill=ink, width=2)
        draw.text((x, 1422), "подпись", font=hint_font, fill=accent)
        # Wax seal stamped on the end of the signature line.
        draw.ellipse([x + 470, 1380, x + 530, 1440], fill=seal)
        draw.ellipse([x + 480, 1390, x + 520, 1430], outline="#c9a227", width=2)

    _centered(
        draw,
        "скреплено честным словом",
        1600,
        _font(fonts["serif"], 22),
        accent,
        width,
    )

    image.save(path)
    return True


def make_free(path: Path, fonts: dict[str, Path], force: bool) -> bool:
    """Blank with an empty area for free-form terms."""
    if path.exists() and not force:
        return False
    width, height = 1240, 1754
    ink = "#1b2a4a"
    muted = "#5c6880"
    frame = "#d9d2c4"
    image = Image.new("RGB", (width, height), "#fffdf8")
    draw = ImageDraw.Draw(image)

    draw.rectangle([56, 56, width - 56, height - 56], outline=ink, width=3)
    draw.rectangle([70, 70, width - 70, height - 70], outline=ink, width=1)

    _centered(draw, "ДОГОВОР", 140, _font(fonts["display"], 72), ink, width)
    _centered(draw, "на условиях сторон", 226, _font(fonts["serif"], 32), muted, width)
    draw.line([(420, 290), (820, 290)], fill=ink, width=2)

    # Область свободных условий: тонкая рамка и заголовок блока.
    draw.rectangle([90, 352, width - 90, 1068], outline=frame, width=2)
    draw.text((110, 366), "УСЛОВИЯ", font=_font(fonts["serif"], 24), fill=muted)

    label_font = _font(fonts["serif"], 26)
    hint_font = _font(fonts["serif"], 20)
    _centered(draw, "ПОДПИСИ СТОРОН", 1180, _font(fonts["display"], 30), ink, width)

    for x, label in ((78, "Сторона 1"), (662, "Сторона 2")):
        draw.text((x, 1232), label, font=label_font, fill=muted)
        draw.line([(x, 1420), (x + 500, 1420)], fill=ink, width=2)
        draw.text((x, 1432), "подпись", font=hint_font, fill=muted)

    image.save(path)
    return True


BLANK_PAINTERS = {
    "classic": make_classic,
    "minimal": make_minimal,
    "dark": make_dark,
    "medieval": make_medieval,
    "free": make_free,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def load_everything(templates_path: Path, texts_path: Path):
    settings_raw = {}
    settings_path = templates_path.parent / "settings.yaml"
    if settings_path.exists():
        settings_raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    render_settings = RenderSettings(
        jpeg_quality=int((settings_raw.get("render") or {}).get("jpeg_quality", 92)),
        max_photo_side=int((settings_raw.get("render") or {}).get("max_photo_side", 2560)),
    )
    limits = Limits(
        signature_max_length=int((settings_raw.get("limits") or {}).get("signature_max_length", 32)),
        signature_hard_cap=int((settings_raw.get("limits") or {}).get("signature_hard_cap", 64)),
        max_lines=int((settings_raw.get("limits") or {}).get("max_lines", 2)),
        min_font_scale=float((settings_raw.get("limits") or {}).get("min_font_scale", 0.6)),
    )
    warnings: list[str] = []
    templates = load_templates(templates_path, limits, warnings=warnings)
    texts = {}
    if texts_path.exists():
        texts = yaml.safe_load(texts_path.read_text(encoding="utf-8")) or {}
    return templates, limits, render_settings, texts, warnings


def sample_text(field, mode: str, limits: Limits) -> str:
    if mode == "default":
        return field.sample or field.label
    if mode == "long":
        base = "ООО «Рога и Копыта» 🐐❤️🔥"
        cap = min(field.style.max_length, limits.signature_hard_cap)
        graphemes = split_graphemes(base)
        return "".join(graphemes[:cap]).strip()
    return field.sample or field.label


def signature_set(template: TemplateConfig, mode: str, limits: Limits) -> dict[str, str]:
    result: dict[str, str] = {}
    shared: str | None = None
    for field in template.fields.values():
        text = sample_text(field, mode, limits)
        if mode == "self_signed":
            if shared is None:
                shared = text
            text = shared
        result[field.key] = text
    return result


def self_sign_note(texts: dict) -> str:
    value = ((texts.get("guest") or {}).get("self_sign_note")) or "принято в одностороннем порядке"
    return str(value)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _resolve(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _raw_templates(path: Path) -> tuple[dict[str, Path], dict[str, dict]]:
    """Read templates.yaml without validating files (for --make-blanks)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fonts = {
        str(alias): _resolve(str(value))
        for alias, value in (raw.get("fonts") or {}).items()
    }
    templates = {
        str(key): value
        for key, value in (raw.get("templates") or {}).items()
        if isinstance(value, dict)
    }
    return fonts, templates


def cmd_make_blanks(args: argparse.Namespace) -> int:
    try:
        fonts, templates_raw = _raw_templates(args.templates)
    except FileNotFoundError:
        print(f"✗ Не найден {args.templates}", file=sys.stderr)
        return 2

    created = 0
    for key, painter in BLANK_PAINTERS.items():
        entry = templates_raw.get(key)
        if not entry or not entry.get("file"):
            continue
        target = _resolve(str(entry["file"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            made = painter(target, fonts, args.force)
        except KeyError as exc:
            print(f"  ! {key}: нет шрифта {exc} в блоке fonts")
            continue
        if made:
            try:
                shown = target.relative_to(BASE_DIR)
            except ValueError:
                shown = target
            print(f"  + {shown}")
            created += 1
        else:
            print(f"  = {target.name} уже существует (--force чтобы перезаписать)")
    if created == 0:
        print("Новых бланков не создано.")
    else:
        print(f"Готово: {created}. Теперь запусти --debug-boxes и проверь координаты.")
    return 0


def cmd_debug(args: argparse.Namespace) -> int:
    templates, _, _, _, warnings = load_everything(args.templates, args.texts)
    for warning in warnings:
        print(f"  ! {warning}")
    out_dir = Path(args.out) if args.out else SAMPLES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for template in templates.ordered():
        if args.template and template.key != args.template:
            continue
        image = debug_image(template, draw_samples=not args.no_samples)
        target = out_dir / f"{template.key}__boxes.png"
        image.save(target)
        print(f"  + {target}")
        for key, field in template.fields.items():
            print(
                f"    {key}: box=[{field.box.x}, {field.box.y}, {field.box.w}, {field.box.h}] "
                f"font={field.style.font.name} size={field.style.font_size} "
                f"max={field.style.max_length}"
            )
        if template.terms is not None:
            terms = template.terms
            print(
                f"    terms: box=[{terms.box.x}, {terms.box.y}, {terms.box.w}, {terms.box.h}] "
                f"font={terms.style.font.name} size={terms.style.font_size} "
                f"max={terms.style.max_length} строк={terms.style.max_lines}"
            )
        if template.note:
            note = template.note
            print(
                f"    self_sign_note: box=[{note.box.x}, {note.box.y}, {note.box.w}, {note.box.h}] "
                f"font={note.style.font.name} size={note.style.font_size}"
            )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    problems: list[str] = []
    try:
        templates, limits, _, texts, warnings = load_everything(args.templates, args.texts)
    except ConfigError as exc:
        print(f"✗ {exc}")
        return 1

    for warning in warnings:
        problems.append(warning)

    for template in templates.ordered():
        try:
            image = load_template_image(template.file)
        except RenderError as exc:
            problems.append(str(exc))
            continue
        width, height = image.size
        print(f"• {template.key}: {width}x{height}, полей {len(template.fields)}")
        total = 0
        for key, field in template.fields.items():
            total += 1
            box = field.box
            if box.x < 0 or box.y < 0 or box.right > width or box.bottom > height:
                problems.append(f"{template.key}.{key}: box выходит за изображение")
            text = field.sample or field.label
            layout = layout_text(text, box, field.style)
            if layout is None:
                problems.append(
                    f"{template.key}.{key}: образец {text!r} не помещается в box"
                )
            else:
                print(
                    f"    {key}: {text!r} → кегль {layout.font_size} "
                    f"(базовый {field.style.font_size}), строк {len(layout.lines)}"
                )
            check_len = count_graphemes(text)
            limit = min(field.style.max_length, limits.signature_hard_cap)
            if check_len > limit:
                problems.append(
                    f"{template.key}.{key}: образец длиннее лимита ({check_len} > {limit})"
                )
        if total < 2:
            print(f"  ! {template.key}: подписантов меньше двух — это допустимо")
        if template.terms is not None:
            layout = layout_text(
                TERMS_SAMPLE,
                template.terms.box,
                template.terms.style,
                max_lines=template.terms.style.max_lines,
            )
            if layout is None:
                problems.append(
                    f"{template.key}.terms: образец условий не помещается в box"
                )
            else:
                print(
                    f"    terms: {count_graphemes(TERMS_SAMPLE)} симв. → "
                    f"кегль {layout.font_size}, строк {len(layout.lines)}"
                )
        if template.note is not None:
            note_text = self_sign_note(texts)
            if layout_text(note_text, template.note.box, template.note.style) is None:
                problems.append(
                    f"{template.key}.self_sign_note: текст {note_text!r} не помещается"
                )
            else:
                print(f"    self_sign_note: {note_text!r} помещается")

    print("• texts.yaml:")
    missing = []
    for key in required_text_keys():
        current = texts
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                missing.append(key)
                break
            current = current[part]
    if missing:
        problems.extend(f"texts.yaml: нет ключа {key}" for key in missing)
    else:
        print("    все обязательные ключи на месте")

    if problems:
        print("\nНайдены проблемы:")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1

    print("\n✓ Всё в порядке.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    templates, limits, _, _, _ = load_everything(args.templates, args.texts)
    print(f"Бланков: {len(templates)}")
    for template in templates.ordered():
        print(f"• {template.key}: {template.title}")
        print(f"  файл: {template.file}")
        print(f"  теги: {', '.join(template.tags) or '—'}")
        print(f"  поля: {', '.join(template.fields) or '—'}")
        print(f"  пометка: {'да' if template.note else 'нет'}")
        if template.terms is not None:
            print(f"  условия: до {template.terms.style.max_length} символов, печатаются на бланке")
        print(f"  лимит: {min(f.style.max_length for f in template.fields.values())} символов")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    templates, limits, render_settings, texts, warnings = load_everything(
        args.templates, args.texts
    )
    for warning in warnings:
        print(f"  ! {warning}")
    out_dir = Path(args.out) if args.out else SAMPLES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    note = self_sign_note(texts)
    created = 0

    for template in templates.ordered():
        if args.template and template.key != args.template:
            continue
        modes = [args.mode] if args.mode else list(BUTTON_CASES)
        for mode in modes:
            signatures = signature_set(template, mode, limits)
            try:
                data = render_contract(
                    template,
                    signatures,
                    self_sign_note=note if mode == "self_signed" else None,
                    terms=TERMS_SAMPLE if template.terms is not None else None,
                    settings=render_settings,
                )
            except RenderError as exc:
                print(f"  ✗ {template.key} [{mode}]: {exc}")
                continue
            target = out_dir / f"{template.key}__{mode}.jpg"
            target.write_bytes(data)
            created += 1
            print(f"  + {target} ({len(data) // 1024} КБ)")

    print(f"Готово: {created} файлов в {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Генератор образцов и отладка бланков контрактов",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--templates", type=Path, default=CONFIG_DIR / "templates.yaml")
    parser.add_argument("--texts", type=Path, default=CONFIG_DIR / "texts.yaml")
    parser.add_argument("--out", help="куда сохранять образцы (по умолчанию samples_output/)")
    parser.add_argument("--template", help="только один бланк по ключу")
    parser.add_argument(
        "--mode",
        choices=BUTTON_CASES,
        help="какой сценарий отрисовать (по умолчанию все)",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--make-blanks", action="store_true", help="создать демо-бланки в templates/"
    )
    group.add_argument(
        "--debug-boxes", action="store_true", help="нарисовать рамки полей и координаты"
    )
    group.add_argument("--check", action="store_true", help="проверить конфиг и вёрстку")
    group.add_argument("--list", action="store_true", help="показать загруженные бланки")

    parser.add_argument("--force", action="store_true", help="перезаписать существующие бланки")
    parser.add_argument(
        "--no-samples", action="store_true", help="в режиме --debug-boxes не рисовать образцы"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.CRITICAL)
    args = build_parser().parse_args(argv)
    try:
        if args.make_blanks:
            return cmd_make_blanks(args)
        if args.debug_boxes:
            return cmd_debug(args)
        if args.check:
            return cmd_check(args)
        if args.list:
            return cmd_list(args)
        return cmd_render(args)
    except ConfigError as exc:
        print(f"✗ Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
