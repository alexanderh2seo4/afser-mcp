import fcntl
import json
import logging
import os
import threading
from pathlib import Path

from .config import private_write
from .source import SourceAdapter, SourceError, build_adapter
from .store import Store, now

LOG = logging.getLogger("afser.sync")


class SyncManager:
    def __init__(self, store: Store):
        self.store = store
        self.directory = store.directory
        self.running = threading.Lock()

    def run(self, adapter: SourceAdapter | None = None) -> dict:
        if not self.running.acquire(blocking=False):
            return {"ok": False, "error": "sync_in_progress"}
        fd = None
        try:
            fd = os.open(self.directory / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"ok": False, "error": "sync_in_progress"}
            result = self.store.activate((adapter or build_adapter(self.directory)).fetch())
            state = {"ok": True, **result, "lastAttemptAt": now()}
            LOG.info("sync complete: raw=%s active=%s", result["counts"]["raw"], result["counts"]["active"])
        except SourceError as exc:
            state = {"ok": False, "error": exc.code, "lastAttemptAt": now()}
            LOG.warning("sync failed: %s", exc.code)
        except Exception:
            # Exception text/traceback may include private HTML or personal data.
            state = {"ok": False, "error": "source_validation_failed", "lastAttemptAt": now()}
            LOG.warning("sync failed: source_validation_failed")
        finally:
            if fd is not None:
                os.close(fd)
            self.running.release()
        private_write(self.directory / "sync-status.json", json.dumps(state))
        return state

    def poll(self, stop: threading.Event, interval: int, initial=True):
        if not initial and stop.wait(interval):
            return
        while not stop.is_set():
            self.run()
            if stop.wait(interval):
                return
