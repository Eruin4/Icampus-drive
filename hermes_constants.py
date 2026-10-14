"""Minimal stand-in for Hermes' ``hermes_constants`` module.

Inside a real Hermes checkout the original module is used and this file is
not needed. It exists so this plugin can be imported and tested standalone:
the iCampus collector only needs ``get_hermes_home()``, which resolves the
``HERMES_HOME`` environment variable and falls back to ``~/.hermes``.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
