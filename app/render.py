"""Image rendering: signatures, emoji fallback, auto-fit and debug boxes."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import regex
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import (
    DEFAULT_FONT,
    BackgroundConfig,
    Box,
    DateConfig,
    DateSettings,
    FieldConfig,
    FrameConfig,
    Limits,
    RenderSettings,
    TemplateConfig,
    TermsConfig,
    TextStyle,
)

log = logging.getLogger(__name__)

_GRAPHEME_RE = regex.compile(r"\X")
# Extended_Pictographic covers most emoji; FE0F/200D/20E3 catch keycaps and
# variation-selector sequences such as ❤️ and 1️⃣.
_EMOJI_RE = regex.compile(r"[\p{Extended_Pictographic}\uFE0F\u200D\u20E3]")
_WHITESPACE_RE = regex.compile(r"\s+")
_CONTROL_RE = regex.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RenderError(RuntimeError):
    """Raised when an image cannot be rendered."""


# ---------------------------------------------------------------------------
# caches
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_image_cache: dict[str, tuple[int, Image.Image]] = {}
_canvas_cache: dict[str, tuple[tuple, Image.Image]] = {}


def clear_caches() -> None:
    """Drop cached fonts, template images and composed canvases."""
    _font_cache.clear()
    _image_cache.clear()
    _canvas_cache.clear()


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def default_date(settings: DateSettings | None = None) -> str:
    """Сегодняшняя дата в настроенном часовом поясе и формате."""
    dates = settings or DateSettings()
    try:
        zone = ZoneInfo(dates.timezone)
    except Exception:  # noqa: BLE001
        log.warning("Неизвестный часовой пояс %r, беру локальное время", dates.timezone)
        zone = None
    now = datetime.now(zone) if zone is not None else datetime.now()
    return now.strftime(dates.format)


def format_date(dates: DateConfig, value: str) -> str:
    """Подставить дату в шаблон блока: ``{date}`` — сама дата."""
    return dates.fmt.replace("{date}", value).strip()


def validate_date(text: str, dates: DateConfig, limits: Limits) -> SignatureCheck:
    """Проверить ручную дату: не пусто, не длинно, влезает в свой блок."""
    normalized = normalize_text(text)
    length = count_graphemes(normalized)
    max_length = min(dates.style.max_length, limits.signature_hard_cap)

    if not normalized:
        return SignatureCheck(False, normalized, 0, max_length, "empty")
    if length > max_length:
        return SignatureCheck(False, normalized, length, max_length, "too_long")

    layout = layout_text(
        format_date(dates, normalized),
        dates.box,
        dates.style,
        max_lines=dates.style.max_lines,
    )
    if layout is None:
        return SignatureCheck(False, normalized, length, max_length, "does_not_fit")
    return SignatureCheck(True, normalized, length, max_length, layout=layout)


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError:
        log.warning("Не удалось открыть шрифт %s, использую %s", path, DEFAULT_FONT)
        font = ImageFont.truetype(str(DEFAULT_FONT), size)
    _font_cache[key] = font
    return font


def load_template_image(path: Path) -> Image.Image:
    """Return a fresh RGB copy of a template image (cached by mtime)."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise RenderError(f"Файл бланка недоступен: {path}") from exc

    cached = _image_cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1].copy()

    try:
        with Image.open(path) as opened:
            opened.load()
            image = opened.copy()
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"Не удалось открыть бланк {path}: {exc}") from exc

    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    _image_cache[str(path)] = (mtime, image.copy())
    return image


def _path_stamp(path: Path | None) -> tuple[str, int, int]:
    if path is None:
        return ("", 0, 0)
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _fit_background(size: tuple[int, int], background: BackgroundConfig) -> Image.Image:
    """Вписать картинку фона в холст: cover, contain или stretch."""
    try:
        with Image.open(background.image) as opened:
            opened.load()
            source = opened.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"Не удалось открыть фон {background.image}: {exc}") from exc

    if background.blur > 0:
        source = source.filter(ImageFilter.GaussianBlur(background.blur))

    if background.fit == "stretch":
        fitted = source.resize(size, Image.LANCZOS)
    elif background.fit == "contain":
        fitted = ImageOps.pad(source, size, method=Image.LANCZOS, color=(255, 255, 255))
    else:  # cover — заполнить холст целиком, лишнее обрезать
        fitted = ImageOps.fit(source, size, method=Image.LANCZOS)

    if background.dim > 0:
        black = Image.new("RGB", size, (0, 0, 0))
        fitted = Image.blend(fitted, black, min(0.95, background.dim))
    return fitted


def _draw_frame(canvas: Image.Image, frame: FrameConfig) -> None:
    """Рамка поверх фона: картинка-оверлей или нарисованные линии."""
    width, height = canvas.size
    if frame.image is not None:
        try:
            with Image.open(frame.image) as opened:
                opened.load()
                overlay = opened.convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            raise RenderError(f"Не удалось открыть рамку {frame.image}: {exc}") from exc
        if overlay.size != canvas.size:
            overlay = overlay.resize((width, height), Image.LANCZOS)
        canvas.paste(overlay, (0, 0), overlay)
        return

    draw = ImageDraw.Draw(canvas)
    inset = max(0, min(frame.inset, min(width, height) // 2 - 2))
    width_line = max(1, min(frame.width, min(width, height) // 4))
    radius = max(0, min(frame.radius, min(width, height) // 2 - inset))
    box = [inset, inset, width - inset, height - inset]
    if radius:
        draw.rounded_rectangle(box, radius=radius, outline=frame.color, width=width_line)
    else:
        draw.rectangle(box, outline=frame.color, width=width_line)

    if frame.double:
        gap = max(1, frame.gap)
        inner = [inset + gap + width_line, inset + gap + width_line,
                 width - inset - gap - width_line, height - inset - gap - width_line]
        if inner[0] < inner[2] and inner[1] < inner[3]:
            inner_radius = max(0, radius - gap)
            if inner_radius:
                draw.rounded_rectangle(
                    inner, radius=inner_radius, outline=frame.color, width=max(1, width_line // 2)
                )
            else:
                draw.rectangle(
                    inner, outline=frame.color, width=max(1, width_line // 2)
                )

    if frame.corners:
        length = max(24, min(120, frame.inset))
        thick = width_line + max(1, width_line // 2)
        for x, y, dx, dy in (
            (inset, inset, 1, 1),
            (width - inset, inset, -1, 1),
            (inset, height - inset, 1, -1),
            (width - inset, height - inset, -1, -1),
        ):
            draw.line([(x, y), (x + dx * length, y)], fill=frame.color, width=thick)
            draw.line([(x, y), (x, y + dy * length)], fill=frame.color, width=thick)


def build_canvas(template: TemplateConfig) -> Image.Image:
    """Собрать бланк: готовый файл или холст с фоном и рамкой.

    Результат кэшируется по mtime всех файлов и настройкам, а наружу отдаётся
    копия: рисование подписей не должно портить кэш.
    """
    background = template.background
    frame = template.frame
    fingerprint = (
        _path_stamp(template.file),
        _path_stamp(background.image if background else None),
        _path_stamp(frame.image if frame else None),
        repr(background),
        repr(frame),
        tuple(template.canvas),
    )
    cached = _canvas_cache.get(template.key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1].copy()

    if template.file is not None:
        canvas = load_template_image(template.file)
        if background is not None:
            log.warning(
                "Бланк %s: background не применяется к готовому файлу", template.key
            )
    else:
        if background is None:
            raise RenderError(f"Бланк {template.key}: нет ни file, ни background")
        canvas = _fit_background(tuple(template.canvas), background)

    if frame is not None:
        _draw_frame(canvas, frame)

    _canvas_cache[template.key] = (fingerprint, canvas.copy())
    return canvas


# ---------------------------------------------------------------------------
# text layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FontSet:
    primary: ImageFont.FreeTypeFont
    emoji: ImageFont.FreeTypeFont | None


@dataclass(frozen=True)
class Glyph:
    text: str
    font: ImageFont.FreeTypeFont
    width: float


@dataclass(frozen=True)
class Line:
    glyphs: tuple[Glyph, ...]
    width: float
    ascent: int
    descent: int

    @property
    def height(self) -> int:
        return self.ascent + self.descent


@dataclass(frozen=True)
class TextLayout:
    lines: tuple[Line, ...]
    font_size: int
    spacing: int
    width: float

    @property
    def height(self) -> int:
        if not self.lines:
            return 0
        return sum(line.height for line in self.lines) + self.spacing * (len(self.lines) - 1)


@dataclass(frozen=True)
class SignatureCheck:
    ok: bool
    text: str
    length: int
    max_length: int
    error: str | None = None
    layout: TextLayout | None = None


def normalize_text(text: str) -> str:
    """Collapse whitespace and strip control characters."""
    text = _CONTROL_RE.sub("", text or "")
    return _WHITESPACE_RE.sub(" ", text).strip()


def count_graphemes(text: str) -> int:
    return len(_GRAPHEME_RE.findall(text))


def split_graphemes(text: str) -> list[str]:
    return _GRAPHEME_RE.findall(text)


def _is_emoji(cluster: str) -> bool:
    return bool(_EMOJI_RE.search(cluster))


def _font_for(cluster: str, fonts: FontSet) -> ImageFont.FreeTypeFont:
    if fonts.emoji is not None and _is_emoji(cluster):
        return fonts.emoji
    return fonts.primary


def _glyphs(text: str, fonts: FontSet) -> list[Glyph]:
    result: list[Glyph] = []
    for cluster in split_graphemes(text):
        font = _font_for(cluster, fonts)
        result.append(Glyph(text=cluster, font=font, width=float(font.getlength(cluster))))
    return result


def _make_line(glyphs: list[Glyph]) -> Line:
    ascent = max(glyph.font.getmetrics()[0] for glyph in glyphs)
    descent = max(glyph.font.getmetrics()[1] for glyph in glyphs)
    width = sum(glyph.width for glyph in glyphs)
    return Line(tuple(glyphs), width, ascent, descent)


def _wrap(text: str, max_width: float, fonts: FontSet) -> list[Line]:
    """Greedy word wrap with per-glyph fallback fonts."""
    words = text.split(" ")
    lines: list[list[Glyph]] = []
    current: list[Glyph] = []
    current_width = 0.0
    space = Glyph(" ", fonts.primary, float(fonts.primary.getlength(" ")))

    def flush() -> None:
        nonlocal current, current_width
        if current:
            lines.append(current)
            current = []
            current_width = 0.0

    def add_word(word: str) -> None:
        nonlocal current, current_width
        glyphs = _glyphs(word, fonts)
        word_width = sum(glyph.width for glyph in glyphs)
        if not glyphs:
            return

        # A single word wider than the box: split it by graphemes.
        if not current and word_width > max_width:
            chunk: list[Glyph] = []
            chunk_width = 0.0
            for glyph in glyphs:
                if chunk and chunk_width + glyph.width > max_width:
                    lines.append(chunk)
                    chunk = []
                    chunk_width = 0.0
                chunk.append(glyph)
                chunk_width += glyph.width
            if chunk:
                current = chunk
                current_width = chunk_width
            return

        candidate = current_width + (space.width if current else 0.0) + word_width
        if current and candidate > max_width:
            flush()
            add_word(word)
            return

        if current:
            current.append(space)
            current_width += space.width
        current.extend(glyphs)
        current_width += word_width

    for word in words:
        if word:
            add_word(word)
    flush()
    return [_make_line(line) for line in lines]


def _layout_at_size(
    text: str,
    box: Box,
    style: TextStyle,
    size: int,
    max_lines: int,
) -> TextLayout | None:
    fonts = FontSet(
        primary=_load_font(style.font, size),
        emoji=_load_font(style.emoji_font, size) if style.emoji_font else None,
    )
    lines = _wrap(text, box.w, fonts)
    if not lines or len(lines) > max_lines:
        return None

    spacing = int(round(size * (style.line_spacing - 1.0)))
    total_height = sum(line.height for line in lines) + spacing * (len(lines) - 1)
    if total_height > box.h:
        return None
    if any(line.width > box.w + 0.5 for line in lines):
        return None

    width = max(line.width for line in lines)
    return TextLayout(tuple(lines), size, spacing, width)


def layout_text(
    text: str,
    box: Box,
    style: TextStyle,
    *,
    max_lines: int | None = None,
) -> TextLayout | None:
    """Find the largest font size at which ``text`` fits into ``box``."""
    normalized = normalize_text(text)
    if not normalized:
        return None

    line_limit = max_lines or style.max_lines
    base = style.font_size
    minimum = max(8, int(round(base * style.min_font_scale)))
    sizes = list(range(base, minimum - 1, -2))
    if minimum not in sizes:
        sizes.append(minimum)

    for size in sizes:
        layout = _layout_at_size(normalized, box, style, size, line_limit)
        if layout is not None:
            return layout
    return None


def draw_layout(
    draw: ImageDraw.ImageDraw,
    layout: TextLayout,
    box: Box,
    color: str | tuple[int, int, int],
    align: str,
    valign: str,
) -> None:
    total_height = layout.height
    if valign == "top":
        y = float(box.y)
    elif valign == "bottom":
        y = float(box.y + box.h - total_height)
    else:
        y = box.y + (box.h - total_height) / 2

    for line in layout.lines:
        if align == "left":
            x = float(box.x)
        elif align == "right":
            x = float(box.x + box.w - line.width)
        else:
            x = box.x + (box.w - line.width) / 2

        baseline = y + line.ascent
        for glyph in line.glyphs:
            try:
                draw.text(
                    (x, baseline),
                    glyph.text,
                    font=glyph.font,
                    fill=color,
                    anchor="ls",
                )
            except ValueError:
                # Some exotic fonts don't support the baseline anchor.
                draw.text((x, baseline - line.ascent), glyph.text, font=glyph.font, fill=color)
            x += glyph.width
        y += line.height + layout.spacing


def validate_terms(text: str, terms: TermsConfig) -> SignatureCheck:
    """Проверить свободный текст условий: длина в графемах и вёрстка в box."""
    normalized = normalize_text(text)
    length = count_graphemes(normalized)
    max_length = terms.style.max_length

    if not normalized:
        return SignatureCheck(False, normalized, 0, max_length, "empty")
    if length > max_length:
        return SignatureCheck(False, normalized, length, max_length, "too_long")

    layout = layout_text(
        normalized,
        terms.box,
        terms.style,
        max_lines=terms.style.max_lines,
    )
    if layout is None:
        return SignatureCheck(False, normalized, length, max_length, "does_not_fit")
    return SignatureCheck(True, normalized, length, max_length, layout=layout)


def validate_signature(text: str, field: FieldConfig, limits: Limits) -> SignatureCheck:
    normalized = normalize_text(text)
    length = count_graphemes(normalized)
    max_length = min(field.style.max_length, limits.signature_hard_cap)

    if not normalized:
        return SignatureCheck(False, normalized, 0, max_length, "empty")
    if length > max_length:
        return SignatureCheck(False, normalized, length, max_length, "too_long")

    layout = layout_text(
        normalized,
        field.box,
        field.style,
        max_lines=field.style.max_lines,
    )
    if layout is None:
        return SignatureCheck(False, normalized, length, max_length, "does_not_fit")
    return SignatureCheck(True, normalized, length, max_length, layout=layout)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _encode(image: Image.Image, settings: RenderSettings) -> bytes:
    if max(image.size) > settings.max_photo_side:
        image = image.copy()
        image.thumbnail((settings.max_photo_side, settings.max_photo_side), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=settings.jpeg_quality,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


def render_contract(
    template: TemplateConfig,
    signatures: dict[str, str],
    *,
    self_sign_note: str | None = None,
    terms: str | None = None,
    date: str | None = None,
    settings: RenderSettings | None = None,
) -> bytes:
    """Draw terms, date, signatures and the optional note; return a JPEG."""
    settings = settings or RenderSettings()
    image = build_canvas(template)
    draw = ImageDraw.Draw(image)

    date_blocks: list[tuple[DateConfig, str]] = []
    if date and template.dates is not None:
        date_blocks.append((template.dates, date))
    if date:
        date_blocks.extend(
            (field.date, date)
            for field in template.fields.values()
            if field.date is not None
        )
    for dates, value in date_blocks:
        text = format_date(dates, value)
        layout = layout_text(
            text, dates.box, dates.style, max_lines=dates.style.max_lines
        )
        if layout is None:
            log.warning("Дата не помещается в блок %s", template.key)
            continue
        draw_layout(
            draw,
            layout,
            dates.box,
            dates.style.color,
            dates.style.align,
            dates.style.valign,
        )

    if terms and template.terms is not None:
        layout = layout_text(
            terms,
            template.terms.box,
            template.terms.style,
            max_lines=template.terms.style.max_lines,
        )
        if layout is None:
            raise RenderError("Условия не помещаются в блок на бланке")
        draw_layout(
            draw,
            layout,
            template.terms.box,
            template.terms.style.color,
            template.terms.style.align,
            template.terms.style.valign,
        )

    for key, text in signatures.items():
        field = template.fields.get(key)
        if field is None:
            log.warning("В бланке %s нет поля %s", template.key, key)
            continue
        layout = layout_text(text, field.box, field.style, max_lines=field.style.max_lines)
        if layout is None:
            raise RenderError(f"Подпись не помещается в поле {key}")
        draw_layout(draw, layout, field.box, field.style.color, field.style.align, field.style.valign)

    if self_sign_note and template.note is not None:
        layout = layout_text(
            self_sign_note,
            template.note.box,
            template.note.style,
            max_lines=template.note.style.max_lines,
        )
        if layout is None:
            log.warning("Пометка о самоподписи не помещается в поле бланка %s", template.key)
        else:
            draw_layout(
                draw,
                layout,
                template.note.box,
                template.note.style.color,
                template.note.style.align,
                template.note.style.valign,
            )

    return _encode(image, settings)


def debug_image(template: TemplateConfig, *, draw_samples: bool = True) -> Image.Image:
    """Return a copy with field boxes and labels for manual tuning."""
    image = load_template_image(template.file)
    draw = ImageDraw.Draw(image)
    label_font = _load_font(DEFAULT_FONT, 22)
    label_color = (200, 30, 30)

    def mark(box: Box, label: str, color: tuple[int, int, int]) -> None:
        draw.rectangle([box.x, box.y, box.right, box.bottom], outline=color, width=4)
        text = f"{label}  {box.x},{box.y} {box.w}x{box.h}"
        text_width = int(draw.textlength(text, font=label_font)) + 10
        # Inside the box, top-left: never collides with captions above the field.
        draw.rectangle(
            [box.x + 2, box.y + 2, box.x + text_width, box.y + 28],
            fill=(255, 255, 255),
            outline=color,
            width=1,
        )
        draw.text((box.x + 6, box.y + 4), text, font=label_font, fill=color)

    for key, field in template.fields.items():
        mark(field.box, key, label_color)
        if draw_samples and field.sample:
            layout = layout_text(field.sample, field.box, field.style)
            if layout is not None:
                draw_layout(
                    draw,
                    layout,
                    field.box,
                    (20, 120, 40),
                    field.style.align,
                    field.style.valign,
                )

    if template.note is not None:
        mark(template.note.box, "self_sign_note", (30, 80, 200))

    if template.dates is not None:
        mark(template.dates.box, "dates", (220, 120, 20))
        if draw_samples:
            sample = format_date(template.dates, "18.10.2026")
            layout = layout_text(
                sample,
                template.dates.box,
                template.dates.style,
                max_lines=template.dates.style.max_lines,
            )
            if layout is not None:
                draw_layout(
                    draw,
                    layout,
                    template.dates.box,
                    (220, 120, 20),
                    template.dates.style.align,
                    template.dates.style.valign,
                )

    for field_key, field in template.fields.items():
        if field.date is not None:
            mark(field.date.box, f"{field_key}.date", (220, 120, 20))

    if template.terms is not None:
        mark(template.terms.box, "terms", (150, 40, 150))
        if draw_samples:
            sample = (
                "Условия пишутся свободным текстом: сроки, оплата, "
                "ответственность сторон."
            )
            layout = layout_text(
                sample,
                template.terms.box,
                template.terms.style,
                max_lines=template.terms.style.max_lines,
            )
            if layout is not None:
                draw_layout(
                    draw,
                    layout,
                    template.terms.box,
                    (150, 40, 150),
                    template.terms.style.align,
                    template.terms.style.valign,
                )

    return image
