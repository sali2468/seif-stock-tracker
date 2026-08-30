"""bootstrap_migrate.py — one-time data import for the Render migration.

On startup, if a base64'd tar.gz exists at /etc/secrets/migrate_b64 (a Render
Secret File) AND no accounts store exists yet on the persistent disk, decode it
and extract into DATA_DIR. This carries existing accounts + positions onto Render
without touching the (paste-hostile) web shell.

Idempotent and safe: runs once per process, and skips entirely once accounts.json
already exists — so it never overwrites live data. No-op locally (no DATA_DIR /
no secret file).
"""

import os
import io
import base64
import tarfile
import logging

log = logging.getLogger("bootstrap_migrate")

_done = False


def run() -> None:
    global _done
    if _done:
        return
    _done = True

    data_dir = os.getenv("DATA_DIR")
    src = os.getenv("MIGRATE_FILE", "/etc/secrets/migrate_b64")
    if not data_dir or not os.path.exists(src):
        return

    # Idempotent: if accounts already exist on the disk, do nothing.
    if os.path.exists(os.path.join(data_dir, ".veterans_edge", "accounts.json")):
        return

    try:
        with open(src) as f:
            raw = base64.b64decode(f.read().strip())
        os.makedirs(data_dir, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            try:
                tar.extractall(data_dir, filter="data")   # py3.12 safe extraction
            except TypeError:
                tar.extractall(data_dir)                    # older Python fallback
        log.info("bootstrap_migrate: imported migration data into %s", data_dir)
    except Exception as e:
        log.warning("bootstrap_migrate failed: %s", e)
