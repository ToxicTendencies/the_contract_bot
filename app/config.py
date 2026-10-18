"""Configuration loading, validation and hot reload.

Three files drive the bot:

* ``config/settings.yaml``  — limits, TTL, render/storage options
* ``config/templates.yaml`` — blank images, boxes, fonts, labels
* ``config/texts.yaml``     — every user-visible message

Secrets live in ``.env`` only.  The bot re-reads the YAML files on the fly and
keeps the previous good copy if a reload fails, so a typo never takes it down.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from dotenv import load_dotenv
from PIL import Image, ImageColor

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_FONT = BASE_DIR / "assets" / "fonts" / "Neucha.ttf"


class ConfigError(RuntimeError):
    """Raised when a config file cannot be parsed or a value is invalid."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Файл не найден: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка YAML в {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Ожидался словарь в {path}, получено {type(data).__name__}")
    return data


def _as_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name}: должно быть >= {minimum}, получено {result}")
    return result


def _as_float(value: Any, name: str, *, minimum: float | None = None,
              maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: ожидалось число, получено {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name}: должно быть >= {minimum}, получено {result}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name}: должно быть <= {maximum}, получено {result}")
    return result


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "да"}:
            return True
        if lowered in {"0", "false", "no", "off", "нет"}:
            return False
    raise ConfigError(f"{name}: ожидалось true/false, получено {value!r}")


def _resolve_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def _dotted(data: dict[str, Any], key: str) -> Any:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: ожидался словарь, получено {type(value).__name__}")
    return value


class _SafeDict(dict[str, Any]):
    """``format_map`` helper: missing keys stay visible instead of crashing."""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


# ---------------------------------------------------------------------------
# settings.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    signature_max_length: int = 32
    signature_hard_cap: int = 64
    max_lines: int = 2
    min_font_scale: float = 0.6


@dataclass(frozen=True)
class ContractSettings:
    ttl_minutes: int = 60
    pending_input_minutes: int = 10
    max_active: int = 500
    cleanup_interval_seconds: int = 30


@dataclass(frozen=True)
class RenderSettings:
    jpeg_quality: int = 92
    max_photo_side: int = 2560


@dataclass(frozen=True)
class StorageSettings:
    cache_dir: Path = BASE_DIR / ".cache"
    file_ids_file: str = "file_ids.json"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    storage_chat_id: int | str
    log_level: str
    contract: ContractSettings
    limits: Limits
    render: RenderSettings
    storage: StorageSettings


def load_settings(
    path: Path = CONFIG_DIR / "settings.yaml",
    env_path: Path = BASE_DIR / ".env",
) -> Settings:
    """Read settings.yaml and environment variables."""
    load_dotenv(env_path, override=False)
    raw = _read_yaml(path)

    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    if not bot_token:
        raise ConfigError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен из @BotFather."
        )

    owner_raw = (os.getenv("OWNER_ID") or "").strip()
    if not owner_raw:
        raise ConfigError(
            "OWNER_ID не задан. Укажи свой Telegram user id в .env "
            "(узнать: @userinfobot)."
        )
    try:
        owner_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError(f"OWNER_ID должен быть числом, получено {owner_raw!r}") from exc
    if owner_id <= 0:
        raise ConfigError("OWNER_ID должен быть положительным числом")

    storage_raw = (os.getenv("STORAGE_CHAT_ID") or "").strip()
    if storage_raw:
        try:
            storage_chat_id: int | str = int(storage_raw)
        except ValueError:
            storage_chat_id = storage_raw
    else:
        storage_chat_id = owner_id

    contract_raw = _mapping(raw.get("contract"), "contract")
    limits_raw = _mapping(raw.get("limits"), "limits")
    render_raw = _mapping(raw.get("render"), "render")
    storage_cfg_raw = _mapping(raw.get("storage"), "storage")
    logging_raw = _mapping(raw.get("logging"), "logging")

    contract = ContractSettings(
        ttl_minutes=_as_int(contract_raw.get("ttl_minutes", 60), "contract.ttl_minutes", minimum=1),
        pending_input_minutes=_as_int(
            contract_raw.get("pending_input_minutes", 10),
            "contract.pending_input_minutes",
            minimum=1,
        ),
        max_active=_as_int(contract_raw.get("max_active", 500), "contract.max_active", minimum=1),
        cleanup_interval_seconds=_as_int(
            contract_raw.get("cleanup_interval_seconds", 30),
            "contract.cleanup_interval_seconds",
            minimum=5,
        ),
    )

    hard_cap = _as_int(
        limits_raw.get("signature_hard_cap", 64), "limits.signature_hard_cap", minimum=1
    )
    limits = Limits(
        signature_max_length=_as_int(
            limits_raw.get("signature_max_length", 32),
            "limits.signature_max_length",
            minimum=1,
        ),
        signature_hard_cap=hard_cap,
        max_lines=_as_int(limits_raw.get("max_lines", 2), "limits.max_lines", minimum=1),
        min_font_scale=_as_float(
            limits_raw.get("min_font_scale", 0.6),
            "limits.min_font_scale",
            minimum=0.2,
            maximum=1.0,
        ),
    )

    render = RenderSettings(
        jpeg_quality=_as_int(
            render_raw.get("jpeg_quality", 92),
            "render.jpeg_quality",
            minimum=40,
        ),
        max_photo_side=_as_int(
            render_raw.get("max_photo_side", 2560),
            "render.max_photo_side",
            minimum=320,
        ),
    )
    if render.jpeg_quality > 100:
        raise ConfigError("render.jpeg_quality должен быть <= 100")

    cache_dir = _resolve_path(storage_cfg_raw.get("cache_dir", ".cache"))
    storage = StorageSettings(
        cache_dir=cache_dir,
        file_ids_file=str(storage_cfg_raw.get("file_ids_file", "file_ids.json")),
    )

    log_level = (os.getenv("LOG_LEVEL") or logging_raw.get("level") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"Неизвестный уровень логирования: {log_level}")

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        storage_chat_id=storage_chat_id,
        log_level=log_level,
        contract=contract,
        limits=limits,
        render=render,
        storage=storage,
    )


# ---------------------------------------------------------------------------
# texts.yaml
# ---------------------------------------------------------------------------


class Texts:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        self._missing_logged: set[str] = set()

    def get(self, key: str, **kwargs: Any) -> str:
        value = _dotted(self.data, key)
        if value is None:
            if key not in self._missing_logged:
                self._missing_logged.add(key)
                log.warning("Ключ текста %r не найден в %s", key, self.path)
            return f"[{key}]"
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(item) for item in value)
        elif not isinstance(value, str):
            value = str(value)
        try:
            return value.format_map(_SafeDict(kwargs))
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("Не удалось подставить плейсхолдеры в %r: %s", key, exc)
            return value


# ---------------------------------------------------------------------------
# templates.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


@dataclass(frozen=True)
class TextStyle:
    font: Path
    emoji_font: Path | None
    font_size: int
    color: str
    align: str
    valign: str
    max_length: int
    max_lines: int
    min_font_scale: float
    line_spacing: float


@dataclass(frozen=True)
class FieldConfig:
    key: str
    label: str
    box: Box
    style: TextStyle
    sample: str = ""


@dataclass(frozen=True)
class NoteConfig:
    box: Box
    style: TextStyle


@dataclass(frozen=True)
class TemplateConfig:
    key: str
    file: Path
    title: str
    description: str
    tags: tuple[str, ...]
    fields: dict[str, FieldConfig]
    note: NoteConfig | None
    sort: int = 0


@dataclass(frozen=True)
class TemplatesConfig:
    items: dict[str, TemplateConfig]
    fonts: dict[str, Path]
    path: Path

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def get(self, key: str) -> TemplateConfig | None:
        return self.items.get(key)

    def ordered(self) -> list[TemplateConfig]:
        return list(self.items.values())

    def find(self, query: str) -> TemplateConfig | None:
        needle = query.strip().casefold()
        if not needle:
            return None
        for template in self.items.values():
            haystack = [template.key, template.title, *template.tags]
            for value in haystack:
                if needle == value.casefold():
                    return template
        for template in self.items.values():
            haystack = " ".join([template.key, template.title, *template.tags]).casefold()
            if needle in haystack:
                return template
        return None


STYLE_KEYS = {
    "font",
    "emoji_font",
    "font_size",
    "color",
    "align",
    "valign",
    "max_length",
    "max_lines",
    "min_font_scale",
    "line_spacing",
}

_ALIGNS = {"left", "center", "right"}
_VALIGNS = {"top", "middle", "bottom"}


def parse_box(value: Any, name: str) -> Box:
    if isinstance(value, dict):
        try:
            raw = [value["x"], value["y"], value["w"], value["h"]]
        except KeyError as exc:
            raise ConfigError(f"{name}: в box нужны ключи x, y, w, h") from exc
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        raw = list(value)
    else:
        raise ConfigError(f"{name}: box должен быть [x, y, ширина, высота]")
    try:
        x, y, w, h = (int(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: координаты box должны быть целыми") from exc
    if w <= 0 or h <= 0:
        raise ConfigError(f"{name}: ширина и высота box должны быть > 0")
    return Box(x, y, w, h)


def _resolve_font(ref: Any, fonts: dict[str, Path], name: str, strict: bool) -> Path:
    if ref is None or str(ref).strip() == "":
        ref = "handwriting"
    ref_str = str(ref).strip()
    path = fonts.get(ref_str)
    if path is None:
        path = _resolve_path(ref_str)
    if not path.exists():
        if strict:
            raise ConfigError(f"{name}: шрифт не найден: {path}")
        log.warning("%s: шрифт не найден, использую %s", name, DEFAULT_FONT)
        fallback = _resolve_path(DEFAULT_FONT)
        if fallback.exists():
            return fallback
        for candidate in fonts.values():
            if candidate.exists():
                return candidate
        raise ConfigError(f"{name}: не найден ни один шрифт, проверь assets/fonts")
    return path


def _style_from(
    raw: dict[str, Any],
    defaults: dict[str, Any],
    fonts: dict[str, Path],
    limits: Limits,
    name: str,
    strict: bool,
) -> TextStyle:
    data = dict(defaults)
    data.update({key: value for key, value in raw.items() if key in STYLE_KEYS})

    font = _resolve_font(data.get("font", "handwriting"), fonts, f"{name}.font", strict)
    emoji_ref = data.get("emoji_font", "emoji")
    emoji_font: Path | None = None
    if emoji_ref:
        try:
            emoji_font = _resolve_font(emoji_ref, fonts, f"{name}.emoji_font", strict)
        except ConfigError:
            if strict:
                raise
            emoji_font = None

    font_size = _as_int(data.get("font_size", 58), f"{name}.font_size", minimum=8)
    if font_size > 500:
        raise ConfigError(f"{name}.font_size: слишком большой ({font_size})")

    color = str(data.get("color", "#1b2a4a"))
    try:
        ImageColor.getrgb(color)
    except ValueError as exc:
        raise ConfigError(f"{name}.color: неизвестный цвет {color!r}") from exc

    align = str(data.get("align", "center")).lower()
    if align not in _ALIGNS:
        raise ConfigError(f"{name}.align: одно из {sorted(_ALIGNS)}")
    valign = str(data.get("valign", "middle")).lower()
    if valign not in _VALIGNS:
        raise ConfigError(f"{name}.valign: одно из {sorted(_VALIGNS)}")

    max_length = _as_int(
        data.get("max_length", limits.signature_max_length),
        f"{name}.max_length",
        minimum=1,
    )
    if max_length > 512:
        raise ConfigError(f"{name}.max_length: слишком много ({max_length})")

    max_lines = _as_int(
        data.get("max_lines", limits.max_lines), f"{name}.max_lines", minimum=1
    )
    if max_lines > 10:
        raise ConfigError(f"{name}.max_lines: максимум 10")

    min_font_scale = _as_float(
        data.get("min_font_scale", limits.min_font_scale),
        f"{name}.min_font_scale",
        minimum=0.2,
        maximum=1.0,
    )
    line_spacing = _as_float(
        data.get("line_spacing", 1.05), f"{name}.line_spacing", minimum=0.8, maximum=3.0
    )

    return TextStyle(
        font=font,
        emoji_font=emoji_font,
        font_size=font_size,
        color=color,
        align=align,
        valign=valign,
        max_length=max_length,
        max_lines=max_lines,
        min_font_scale=min_font_scale,
        line_spacing=line_spacing,
    )


def _image_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise ConfigError(f"Не удалось открыть картинку {path}: {exc}") from exc


def load_templates(
    path: Path = CONFIG_DIR / "templates.yaml",
    limits: Limits | None = None,
    strict: bool = False,
    warnings: list[str] | None = None,
) -> TemplatesConfig:
    """Load templates.  In non-strict mode broken templates are skipped."""
    limits = limits or Limits()
    raw = _read_yaml(path)
    warnings = warnings if warnings is not None else []

    fonts_raw = _mapping(raw.get("fonts"), "fonts")
    fonts: dict[str, Path] = {
        str(alias): _resolve_path(str(value)) for alias, value in fonts_raw.items()
    }

    defaults = _mapping(raw.get("defaults"), "defaults")
    templates_raw = _mapping(raw.get("templates"), "templates")
    if not templates_raw:
        raise ConfigError(f"В {path} нет ни одного бланка в templates")

    items: list[TemplateConfig] = []
    for key, template_raw in templates_raw.items():
        key = str(key)
        if not isinstance(template_raw, dict):
            message = f"templates.{key}: ожидался словарь"
            if strict:
                raise ConfigError(message)
            warnings.append(message)
            continue
        if template_raw.get("enabled", True) is False:
            continue

        file_value = template_raw.get("file")
        if not file_value:
            message = f"templates.{key}.file: не указан файл"
            if strict:
                raise ConfigError(message)
            warnings.append(message)
            continue
        file_path = _resolve_path(str(file_value))
        if not file_path.exists():
            message = f"templates.{key}.file: файл не найден: {file_path}"
            if strict:
                raise ConfigError(message)
            warnings.append(message)
            log.error("%s", message)
            continue

        try:
            width, height = _image_size(file_path)
        except ConfigError as exc:
            if strict:
                raise
            warnings.append(str(exc))
            log.error("%s", exc)
            continue

        fields_raw = _mapping(template_raw.get("fields"), f"templates.{key}.fields")
        if not fields_raw:
            message = f"templates.{key}: нет полей fields"
            if strict:
                raise ConfigError(message)
            warnings.append(message)
            log.error("%s", message)
            continue

        fields: dict[str, FieldConfig] = {}
        template_ok = True
        for field_key, field_raw in fields_raw.items():
            field_key = str(field_key)
            if not isinstance(field_raw, dict):
                message = f"templates.{key}.fields.{field_key}: ожидался словарь"
                if strict:
                    raise ConfigError(message)
                warnings.append(message)
                template_ok = False
                continue
            try:
                box = parse_box(
                    field_raw.get("box"), f"templates.{key}.fields.{field_key}.box"
                )
                if box.x < 0 or box.y < 0 or box.right > width or box.bottom > height:
                    raise ConfigError(
                        f"templates.{key}.fields.{field_key}.box выходит за картинку "
                        f"{width}x{height}: {box}"
                    )
                style = _style_from(
                    field_raw,
                    defaults,
                    fonts,
                    limits,
                    f"templates.{key}.fields.{field_key}",
                    strict,
                )
            except ConfigError as exc:
                if strict:
                    raise
                warnings.append(str(exc))
                log.error("%s", exc)
                template_ok = False
                continue
            fields[field_key] = FieldConfig(
                key=field_key,
                label=str(field_raw.get("label", field_key)),
                box=box,
                style=style,
                sample=str(field_raw.get("sample", "")),
            )

        if not template_ok or not fields:
            if strict:
                raise ConfigError(f"templates.{key}: не удалось загрузить поля")
            warnings.append(f"templates.{key}: пропущен из-за ошибок в полях")
            continue

        note: NoteConfig | None = None
        note_raw = template_raw.get("self_sign_note")
        if isinstance(note_raw, dict) and note_raw.get("enabled", True) is not False:
            try:
                note_box = parse_box(note_raw.get("box"), f"templates.{key}.self_sign_note.box")
                if (
                    note_box.x < 0
                    or note_box.y < 0
                    or note_box.right > width
                    or note_box.bottom > height
                ):
                    raise ConfigError(
                        f"templates.{key}.self_sign_note.box выходит за картинку "
                        f"{width}x{height}: {note_box}"
                    )
                note_style = _style_from(
                    note_raw,
                    defaults,
                    fonts,
                    limits,
                    f"templates.{key}.self_sign_note",
                    strict,
                )
                note = NoteConfig(box=note_box, style=note_style)
            except ConfigError as exc:
                if strict:
                    raise
                warnings.append(str(exc))
                log.error("%s", exc)

        sort_value = _as_int(template_raw.get("sort", 0), f"templates.{key}.sort")
        items.append(
            TemplateConfig(
                key=key,
                file=file_path,
                title=str(template_raw.get("title", key)),
                description=str(template_raw.get("description", "")),
                tags=tuple(str(tag) for tag in template_raw.get("tags", []) or []),
                fields=fields,
                note=note,
                sort=sort_value,
            )
        )

    if not items:
        raise ConfigError(f"В {path} не осталось ни одного рабочего бланка")

    items.sort(key=lambda item: (item.sort, list(templates_raw).index(item.key)))
    return TemplatesConfig(
        items={item.key: item for item in items},
        fonts=fonts,
        path=path,
    )


# ---------------------------------------------------------------------------
# runtime config manager with hot reload
# ---------------------------------------------------------------------------


class ConfigManager:
    """Holds the active config and reloads it when files change."""

    def __init__(
        self,
        config_dir: Path = CONFIG_DIR,
        env_path: Path = BASE_DIR / ".env",
    ):
        self.config_dir = config_dir
        self.settings_path = config_dir / "settings.yaml"
        self.texts_path = config_dir / "texts.yaml"
        self.templates_path = config_dir / "templates.yaml"
        self.env_path = env_path

        self.settings = load_settings(self.settings_path, self.env_path)
        self.texts = Texts(_read_yaml(self.texts_path), self.texts_path)
        self.templates = load_templates(self.templates_path, self.settings.limits)
        self._mtimes = self._stat_all()
        self._last_check = 0.0
        self._reload_callbacks: list[Callable[[], None]] = []

    def _stat_all(self) -> dict[Path, int]:
        result: dict[Path, int] = {}
        for path in (self.settings_path, self.texts_path, self.templates_path):
            try:
                result[path] = path.stat().st_mtime_ns
            except OSError:
                result[path] = 0
        return result

    def text(self, key: str, **kwargs: Any) -> str:
        return self.texts.get(key, **kwargs)

    def on_reload(self, callback: Callable[[], None]) -> None:
        self._reload_callbacks.append(callback)

    def maybe_reload(self, *, force: bool = False) -> bool:
        """Reload YAML files if they changed.  Never raises."""
        now = time.monotonic()
        if not force and now - self._last_check < 1.0:
            return False
        self._last_check = now

        mtimes = self._stat_all()
        if mtimes == self._mtimes and not force:
            return False

        try:
            new_settings = load_settings(self.settings_path, self.env_path)
            new_texts = Texts(_read_yaml(self.texts_path), self.texts_path)
            new_templates = load_templates(self.templates_path, new_settings.limits)
        except Exception as exc:  # noqa: BLE001 - keep the bot alive
            log.error("Не удалось перечитать конфиг, оставляю старый: %s", exc)
            # Remember current mtimes so a broken file is not retried every second.
            # The next save changes mtime and triggers another attempt.
            self._mtimes = mtimes
            return False

        old = self.settings
        if (
            old.bot_token != new_settings.bot_token
            or old.owner_id != new_settings.owner_id
            or old.storage_chat_id != new_settings.storage_chat_id
        ):
            log.warning(
                "BOT_TOKEN/OWNER_ID/STORAGE_CHAT_ID изменились — для применения "
                "нужен перезапуск. Остальные настройки применены."
            )
            new_settings = Settings(
                bot_token=old.bot_token,
                owner_id=old.owner_id,
                storage_chat_id=old.storage_chat_id,
                log_level=new_settings.log_level,
                contract=new_settings.contract,
                limits=new_settings.limits,
                render=new_settings.render,
                storage=new_settings.storage,
            )

        self.settings = new_settings
        self.texts = new_texts
        self.templates = new_templates
        self._mtimes = mtimes
        for callback in self._reload_callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в обработчике перезагрузки конфига")
        log.info("Конфиг перечитан: бланков %d", len(new_templates))
        return True


def required_text_keys() -> Iterable[str]:
    """Keys the code depends on.  Used by ``generate_samples.py --check``."""
    return (
        "dm.greeting",
        "dm.help",
        "dm.owner_only",
        "dm.guest_mode_disabled",
        "dm.started",
        "dm.storage_failed",
        "dm.templates_ok",
        "dm.no_templates",
        "dm.template_line",
        "dm.template_status_ready",
        "dm.template_status_missing",
        "dm.reloaded",
        "dm.reload_failed",
        "dm.status",
        "dm.unknown_dm",
        "guest.preview_caption",
        "guest.preview_caption_no_description",
        "guest.contract_caption",
        "guest.sign_prompt",
        "guest.sign_prompt_anonymous",
        "guest.one_signed",
        "guest.done_caption",
        "guest.done_self_caption",
        "guest.self_sign_note",
        "guest.self_sign_note_caption",
        "guest.too_long",
        "guest.empty",
        "guest.does_not_fit",
        "guest.text_only",
        "guest.no_commands",
        "guest.not_pending",
        "guest.pending_expired",
        "guest.pending_other",
        "guest.unknown_query",
        "guest.already_signed",
        "guest.cancel_done",
        "guest.cancelled_by_other",
        "guest.render_failed",
        "guest.upload_failed",
        "guest.contract_expired",
        "guest.no_templates",
        "guest.help",
        "buttons.prev",
        "buttons.next",
        "buttons.pick",
        "buttons.sign",
        "buttons.signed",
        "buttons.cancel",
        "buttons.retry",
        "callback.expired",
        "callback.done",
        "callback.already_signed",
        "callback.waiting",
        "callback.pick_first",
        "callback.cancelled",
        "callback.not_yours",
        "errors.generic",
        "errors.no_templates",
    )
