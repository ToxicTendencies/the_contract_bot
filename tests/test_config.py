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


def test_inline_metadata():
    templates = load_templates()
    medieval = templates.get("medieval")
    assert medieval is not None
    assert medieval.inline_name == "Средневековый контракт"
    assert "Пергамент" in medieval.inline_hint
    assert "рыцарь" in medieval.keywords


def test_find_all_ranking():
    templates = load_templates()
    assert [t.key for t in templates.find_all("")] == [
        "classic",
        "minimal",
        "dark",
        "medieval",
    ]
    assert [t.key for t in templates.find_all("средневековый")] == ["medieval"]
    assert [t.key for t in templates.find_all("рыцарь")] == ["medieval"]
    assert [t.key for t in templates.find_all("договор")] == ["classic"]
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
