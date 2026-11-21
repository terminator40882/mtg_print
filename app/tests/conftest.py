import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    from mtgprint import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CARDS_FILE", tmp_path / "cards.json")
    config.ensure_dirs()
    return tmp_path
