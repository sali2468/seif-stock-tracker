"""background_worker.py — in-process scan precompute for the cloud.

Render disks attach to a single service, so a standalone worker can't share the
web app's disk. Instead this runs a daemon THREAD inside the web process: it
periodically computes the full market scan once and writes the result to disk
(scanner.save_scan_result), which the app already loads via load_scan_result.

Net effect: ONE scan runs on a timer for everyone, instead of every visitor
triggering their own live 780-ticker scan — big win for concurrency and the
512MB memory budget. Gated behind RUN_SCAN_WORKER so it only runs where wanted
(Render), never locally where the real monitor_daemon already does this.
"""

import os
import time
import threading
import logging

log = logging.getLogger("bg_worker")

_started = False


def _loop() -> None:
    time.sleep(20)   # let the web server finish booting first
    check_every = int(os.getenv("SCAN_CHECK_SEC", "1800"))    # look every 30 min
    refresh_after = int(os.getenv("SCAN_REFRESH_SEC", str(4 * 3600)))  # re-scan every 4h
    while True:
        try:
            from scanner import run_and_cache_scan, scan_result_age_s
            from market_data import get_regime
            age = scan_result_age_s()
            if age is None or age > refresh_after:
                log.info("bg_worker: precomputing market scan…")
                run_and_cache_scan(get_regime())
                log.info("bg_worker: scan cached for the web app.")
        except Exception as e:
            log.warning("bg_worker scan cycle failed: %s", e)
        time.sleep(check_every)


def start() -> None:
    """Start the precompute thread once per process (Render only, via env flag)."""
    global _started
    if _started:
        return
    if os.getenv("RUN_SCAN_WORKER", "").lower() not in ("1", "true", "yes"):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="scan-precompute").start()
    log.info("bg_worker: scan-precompute thread started")
