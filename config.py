"""
config.py — single source of truth for strategy_config.json.

Loads the config once and reloads from disk ONLY when the file's mtime changes.
Per-ticker scan code (detect_vcp, _score_day, _score_momentum_reversal, score_entry)
can therefore call this thousands of times per scan with zero repeated disk I/O.
Editing strategy_config.json (by hand or via the Strategy Review page) is picked up
automatically on the next call because save bumps the file mtime.
"""

import json
import os
import logging

log = logging.getLogger("config")

_CFG_FILE = os.path.join(os.path.dirname(__file__), "strategy_config.json")

_mtime: float = 0.0
_cache: dict = {}


def get_config() -> dict:
    """Return the full strategy config, reloading only if the file changed on disk."""
    global _mtime, _cache
    try:
        mtime = os.path.getmtime(_CFG_FILE)
        if mtime != _mtime:
            with open(_CFG_FILE) as f:
                _cache = json.load(f)
            _mtime = mtime
    except Exception as e:
        log.warning("Could not load %s: %s", _CFG_FILE, e)
    return _cache


def get_section(path: str, default=None):
    """Nested lookup by dot-path, e.g. get_section('swing.entry.hard_gates', {})."""
    obj = get_config()
    for key in path.split("."):
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key, default)
    return obj if obj is not None else default


def config_path() -> str:
    """Absolute path to the config file (for writers like strategy_review.save_config)."""
    return _CFG_FILE
