from __future__ import annotations

import dataclasses
import json
import os

import pytest

from app.storage import CachedFile, TemplateStorage


@pytest.fixture()
def storage(config, tmp_path, monkeypatch):
    settings = dataclasses.replace(
        config.settings,
        storage=dataclasses.replace(config.settings.storage, cache_dir=tmp_path),
    )
    monkeypatch.setattr(config, "settings", settings)
    return TemplateStorage(config), tmp_path


def test_file_id_cache_hit(storage, config):
    store, _ = storage
    template = config.templates.get("classic")
    stat = template.file.stat()
    store._cache["classic"] = CachedFile("file-1", stat.st_mtime_ns, stat.st_size)
    assert store.file_id("classic") == "file-1"


def test_file_id_cache_invalidated_by_mtime(storage, config):
    store, _ = storage
    template = config.templates.get("classic")
    stat = template.file.stat()
    store._cache["classic"] = CachedFile("file-1", stat.st_mtime_ns, stat.st_size)
    os.utime(template.file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert store.file_id("classic") is None


def test_cache_roundtrip(storage, config):
    store, tmp_path = storage
    template = config.templates.get("classic")
    stat = template.file.stat()
    store._cache["classic"] = CachedFile("file-1", stat.st_mtime_ns, stat.st_size)
    store._save()

    payload = json.loads((tmp_path / "file_ids.json").read_text())
    assert payload["classic"]["file_id"] == "file-1"

    fresh = TemplateStorage(config)
    assert fresh.file_id("classic") == "file-1"


def test_broken_cache_file_is_ignored(storage):
    store, tmp_path = storage
    (tmp_path / "file_ids.json").write_text("{not json", encoding="utf-8")
    fresh = TemplateStorage(store.config)
    assert fresh.file_id("classic") is None
