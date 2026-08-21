"""paddock — sandboxed agent sessions for herdr."""

import os
from pathlib import Path

__version__ = "0.1.0"


def config_dir() -> Path:
    """Where profiles and agent entries live. `PADDOCK_CONFIG_DIR` overrides it."""
    return Path(os.environ.get("PADDOCK_CONFIG_DIR", "~/.config/paddock")).expanduser()
