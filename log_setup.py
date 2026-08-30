"""
log_setup.py — one-line logging configuration for the Streamlit app process.

Call setup_logging() once at startup (top of app.py). Modules obtain their own
logger via `logging.getLogger("name")` and their records flow here — to both the
console and veterans_edge.log (already gitignored via *.log). The background
daemon configures its own logging separately (monitor_daemon.py).
"""

import logging
import os

_CONFIGURED = False
LOG_FILE = os.path.join(os.path.dirname(__file__), "veterans_edge.log")


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent — safe to call on every Streamlit rerun; only configures once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except Exception:
        pass  # read-only FS — console logging still works

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # yfinance / urllib3 are chatty at INFO — keep them at WARNING
    for noisy in ("urllib3", "yfinance", "peewee", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
