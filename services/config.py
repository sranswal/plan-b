"""Read Plan B settings from the process or the repository's private .env."""

import os
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def setting(name, default=None):
    if name in os.environ:
        return os.environ[name]
    value = dotenv_values(PROJECT_ROOT / ".env", interpolate=False, encoding="utf-8-sig").get(name)
    return default if value is None else value
