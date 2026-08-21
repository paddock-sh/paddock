"""Every test runs against a throwaway config dir, never the developer's own."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("PADDOCK_CONFIG_DIR", str(path))
    return path
