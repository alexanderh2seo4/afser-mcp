import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PRIVATE_DIR = Path(__file__).resolve().parents[3] / ".private-data"


def private_dir(path: Path | str | None = None) -> Path:
    target = Path(path or os.environ.get("AFSER_PRIVATE_DIR", DEFAULT_PRIVATE_DIR)).expanduser().resolve()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.chmod(0o700)
    return target


def private_write(path: Path, content: str | bytes) -> None:
    """Atomic private file replacement, including the temporary file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    temp = path.with_name(f".{path.name}.{secrets.token_hex(5)}.tmp")
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    if path.is_symlink():
        raise ValueError("private_config_symlink")
    path.chmod(0o600)
    return json.loads(path.read_text())


@dataclass
class BridgeConfig:
    origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    port: int = 8765
    poll_seconds: int = 1800
    remote_hosts: tuple[str, ...] = ()

    @classmethod
    def load(cls, directory: Path):
        data = load_json(directory / "bridge.json", {})
        origins = tuple(data.get("origins", cls.origins))
        for origin in origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or "*" in origin:
                raise ValueError("invalid_cors_origin")
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("non_tls_remote_origin")
        port = int(data.get("port", 8765))
        if not 1024 <= port <= 65535:
            raise ValueError("invalid_port")
        return cls(origins, port, max(60, int(data.get("pollSeconds", 1800))), tuple(data.get("remoteHosts", ())))
