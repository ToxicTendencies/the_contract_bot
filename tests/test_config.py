from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import ConfigError, ConfigManager, load_settings, load_templates, Limits


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent.parent / "config"
    target = tmp_path / "config"
    shutil.copytree(source, target)
    return target


def test_templates_validate_strict():
    templates = load_templates(strict=True)
    assert len(templates) >= 3
    assert templates.get("classic") is not None
    assert len(templates.get("classic").fields) == 2


def test_hot_reload_texts(tmp_path, monkeypatch):
    config_dir = copy_config(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("OWNER_ID", "42")
    manager = ConfigManager(config_dir)

    assert "Привет" in manager.text("dm.greeting")
    path = config_dir / "texts.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Привет! Я собираю контракты", "Здравствуй! Я собираю контракты"
        ),
        encoding="utf-8",
    )
    assert manager.maybe_reload(force=True) is True
    assert "Здравствуй!" in manager.text("dm.greeting")


def test_broken_yaml_keeps_previous_config(tmp_path, monkeypatch):
    config_dir = copy_config(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("OWNER_ID", "42")
    manager = ConfigManager(config_dir)
    before = manager.text("dm.greeting")

    path = config_dir / "texts.yaml"
    path.write_text("dm: [broken", encoding="utf-8")

    assert manager.maybe_reload(force=True) is False
    assert manager.text("dm.greeting") == before


def test_missing_token_raises(tmp_path, monkeypatch):
    config_dir = copy_config(tmp_path)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_ID", raising=False)
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_settings(config_dir / "settings.yaml", tmp_path / "no.env")


def test_invalid_template_box(tmp_path, monkeypatch):
    config_dir = copy_config(tmp_path)
    path = config_dir / "templates.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "box: [78, 1280, 500, 140]", "box: [78, 99999, 500, 140]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_templates(path, Limits(), strict=True)


def test_disabled_template_is_skipped(tmp_path):
    config_dir = copy_config(tmp_path)
    path = config_dir / "templates.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "  dark:\n    file:", "  dark:\n    enabled: false\n    file:"
    )
    path.write_text(text, encoding="utf-8")
    templates = load_templates(path, Limits(), strict=True)
    assert templates.get("dark") is None
    assert templates.get("classic") is not None


def test_find_template():
    templates = load_templates()
    assert templates.find("классика").key == "classic"
    assert templates.find("Классический договор").key == "classic"
    assert templates.find("минимализм").key == "minimal"
    assert templates.find("ерунда") is None


def test_template_keywords():
    templates = load_templates()
    medieval = templates.get("medieval")
    assert medieval is not None
    assert medieval.title == "Средневековый контракт"
    assert "рыцарь" in medieval.keywords
    assert "пергамент" in medieval.keywords


def test_terms_block_is_loaded():
    templates = load_templates()
    free = templates.get("free")
    assert free is not None
    assert free.has_terms
    assert free.terms is not None
    assert free.terms.style.max_length == 700
    assert free.terms.style.max_lines == 18
    assert free.terms.box.w == 1020
    assert templates.get("classic").has_terms is False


def test_find_all_ranking():
    templates = load_templates()
    assert [t.key for t in templates.find_all("")] == [
        "classic",
        "minimal",
        "dark",
        "medieval",
        "free",
    ]
    assert [t.key for t in templates.find_all("средневековый")] == ["medieval"]
    assert [t.key for t in templates.find_all("рыцарь")] == ["medieval"]
    assert [t.key for t in templates.find_all("условия")] == ["free"]
    assert templates.find_all("ерунда") == []


def test_find_all_requires_every_word():
    templates = load_templates()
    assert [t.key for t in templates.find_all("ночной контракт")] == ["dark"]
    assert templates.find_all("ночной средневековый") == []


def test_template_key_too_long_is_rejected(tmp_path):
    config_dir = copy_config(tmp_path)
    path = config_dir / "templates.yaml"
    text = path.read_text(encoding="utf-8").replace("  classic:\n", "  very_long_template_key_name:\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="длиннее 16"):
        load_templates(path, Limits(), strict=True)


def test_template_key_rejects_non_ascii(tmp_path):
    config_dir = copy_config(tmp_path)
    path = config_dir / "templates.yaml"
    text = path.read_text(encoding="utf-8").replace("  classic:\n", "  классика:\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="длиннее 16"):
        load_templates(path, Limits(), strict=True)


def test_text_values_can_contain_braces(tmp_path, monkeypatch):
    """Текст условий пишут пользователи: фигурные скобки не должны ломать подстановку."""
    config_dir = copy_config(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("OWNER_ID", "42")
    manager = ConfigManager(config_dir)

    line = manager.text(
        "archive.terms_line", terms="платит {1000} ₽, шаблон {terms} и {заказчик}"
    )
    assert "платит {1000} ₽, шаблон {terms} и {заказчик}" in line

    # не переданный плейсхолдер остаётся видимым, как и раньше
    assert manager.text("archive.terms_line").endswith("{terms}")


def test_dates_are_parsed_for_every_template(config):
    for key in ("classic", "minimal", "dark", "medieval", "free"):
        template = config.templates.get(key)
        assert template is not None
        assert template.has_dates
        dates = template.dates
        assert "{date}" in dates.fmt
        assert dates.style.max_length >= 10
        assert dates.box.x >= 0 and dates.box.y >= 0
        assert dates.box.right <= template.canvas[0]
        assert dates.box.bottom <= template.canvas[1]


def test_settings_dates_defaults(config):
    assert config.settings.dates.timezone == "Europe/Moscow"
    assert config.settings.dates.format == "%d.%m.%Y"


def test_custom_template_with_background_and_frame(tmp_path: Path):
    background = Path(__file__).resolve().parent.parent / "templates" / "classic.png"
    source = tmp_path / "custom.yaml"
    source.write_text(
        f"""
fonts:
  serif: {Path(__file__).resolve().parent.parent / "assets/fonts/PTSerif-Regular.ttf"}

templates:
  my_blank:
    title: "Мой бланк"
    description: "Свой фон"
    background:
      image: {background}
      fit: contain
      dim: 0.15
      blur: 2
    frame:
      color: "#334455"
      width: 6
      inset: 30
      gap: 10
      double: true
      corners: true
      radius: 18
    canvas: [1000, 1400]
    dates:
      box: [600, 40, 320, 50]
      font_size: 24
      align: right
      format: "от {{date}}"
    fields:
      party1:
        label: "Сторона 1"
        box: [80, 1000, 380, 120]
        date:
          box: [80, 1130, 380, 50]
          font_size: 18
          align: center
          format: "подписано {{date}}"
      party2:
        label: "Сторона 2"
        box: [540, 1000, 380, 120]
""",
        encoding="utf-8",
    )
    templates = load_templates(extra_paths=[source], strict=True)
    template = templates.get("my_blank")
    assert template is not None
    assert template.is_custom
    assert template.canvas == (1000, 1400)
    assert template.background is not None and template.background.fit == "contain"
    assert template.background.dim == 0.15 and template.background.blur == 2
    assert template.frame is not None
    assert template.frame.double and template.frame.corners
    assert template.frame.radius == 18
    assert template.dates is not None and template.dates.fmt == "от {date}"
    field_date = template.fields["party1"].date
    assert field_date is not None and field_date.fmt == "подписано {date}"
    assert field_date.style.max_length == 40
    assert template.fields["party2"].date is None


def test_custom_template_without_file_needs_background(tmp_path: Path, monkeypatch):
    source = tmp_path / "custom.yaml"
    source.write_text(
        """
templates:
  broken:
    title: "Без фона"
    fields:
      party1:
        box: [10, 10, 100, 50]
""",
        encoding="utf-8",
    )
    warnings: list[str] = []
    templates = load_templates(extra_paths=[source], warnings=warnings)
    assert templates.get("broken") is None
    assert any("background" in warning for warning in warnings)

    with pytest.raises(ConfigError):
        load_templates(extra_paths=[source], strict=True)


def test_background_fit_is_validated(tmp_path: Path):
    background = Path(__file__).resolve().parent.parent / "templates" / "classic.png"
    source = tmp_path / "custom.yaml"
    source.write_text(
        f"""
templates:
  weird:
    title: "Странный фон"
    background:
      image: {background}
      fit: tile
    fields:
      party1:
        box: [10, 10, 100, 50]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_templates(extra_paths=[source], strict=True)
