"""paddock: sandboxed agent sessions for herdr."""

import os
from pathlib import Path

__version__ = "0.1.0"


def config_dir() -> Path:
    """Where profiles and agent entries live. `PADDOCK_CONFIG_DIR` overrides it."""
    # `or`, not a get() default: an empty value must not resolve to the current directory.
    return Path(os.environ.get("PADDOCK_CONFIG_DIR") or "~/.config/paddock").expanduser()


def state_dir() -> Path:
    """Where run directories live. `PADDOCK_STATE_DIR` overrides it."""
    return Path(os.environ.get("PADDOCK_STATE_DIR") or "~/.local/state/paddock").expanduser()
