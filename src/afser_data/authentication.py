import hashlib
import hmac
import json
import fcntl
import os
import secrets
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from .config import load_json, private_write


class TokenRegistry:
    """Only digests persist; raw pairing tokens are held in a private invite file."""

    def __init__(self, directory: Path):
        self.path = directory / "access-tokens.json"
        self.lock = threading.RLock()

    @contextmanager
    def file_lock(self):
        fd = os.open(self.path.parent / "access.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def create(self, label: str, days: int = 30) -> str:
        if not 1 <= days <= 365 or not 1 <= len(label) <= 80:
            raise ValueError("invalid_invite")
        token = secrets.token_urlsafe(32)
        with self.lock, self.file_lock():
            entries = load_json(self.path, [])
            entries.append({"id": secrets.token_hex(6), "label": label, "digest": hashlib.sha256(token.encode()).hexdigest(), "expiresAt": int(time.time() + days * 86400)})
            private_write(self.path, json.dumps(entries, indent=2))
        return token

    def verify(self, token: str) -> bool:
        if not isinstance(token, str) or len(token) < 32 or len(token) > 128:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        accepted = False
        with self.lock:
            try:
                entries = load_json(self.path, [])
            except (ValueError, OSError):
                return False
            for entry in entries:
                matches = hmac.compare_digest(digest, entry.get("digest", ""))
                accepted |= matches and entry.get("expiresAt", 0) > time.time()
        return accepted

    def revoke_all(self):
        with self.lock, self.file_lock():
            private_write(self.path, "[]")
