import hashlib
import ipaddress
import json
import logging
import mimetypes
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.parse import unquote
from pathlib import Path

from .authentication import TokenRegistry
from .config import BridgeConfig
from .contact_intake import ContactIntake, ContactIntakeError
from .store import Store

LOG = logging.getLogger("afser.bridge")


class RateLimiter:
    def __init__(self, rate_per_minute=120, capacity=60, maximum_keys=1000):
        self.rate = rate_per_minute / 60
        self.capacity = capacity
        self.maximum_keys = maximum_keys
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        timestamp = time.monotonic()
        with self.lock:
            available, updated = self.items.pop(key, (self.capacity, timestamp))
            available = min(self.capacity, available + (timestamp - updated) * self.rate)
            allowed = available >= 1
            self.items[key] = (available - 1 if allowed else available, timestamp)
            while len(self.items) > self.maximum_keys:
                self.items.popitem(last=False)
            return allowed


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store: Store, config: BridgeConfig, port: int | None = None, frontend_dir: Path | None = None):
        # Binding is deliberately not configurable to a public interface.
        self.store = store
        self.config = config
        self.frontend_dir = frontend_dir.resolve() if frontend_dir else None
        if self.frontend_dir and not self.frontend_dir.is_dir():
            raise ValueError("missing_frontend_directory")
        self.tokens = TokenRegistry(store.directory)
        self.limiter = RateLimiter()
        # Cloudflare traffic shares a loopback socket; this global ceiling also
        # bounds random invalid tokens before token-file verification occurs.
        self.ip_limiter = RateLimiter(rate_per_minute=600, capacity=120)
        self.contact_limiter = RateLimiter(rate_per_minute=3, capacity=3)
        self.contact_sync_event = threading.Event()
        self.contact_sync_stop = threading.Event()
        try:
            self.contact_intake = ContactIntake(store.directory)
        except Exception:
            # Intake setup must never prevent the existing read-only map from starting.
            self.contact_intake = None
        self.contact_sync_thread = None
        if self.contact_intake:
            self.contact_sync_thread = threading.Thread(target=self._contact_export_worker, daemon=True)
        self.connections = threading.BoundedSemaphore(32)
        super().__init__(("127.0.0.1", config.port if port is None else port), BridgeHandler)
        if self.contact_sync_thread:
            self.contact_sync_thread.start()

    def notify_contact_submission(self):
        if self.contact_sync_thread and self.contact_intake and self.contact_intake.enabled:
            self.contact_sync_event.set()

    def _contact_export_worker(self):
        if self.contact_intake and self.contact_intake.enabled and self.contact_intake.records():
            self.contact_sync_event.set()
        while not self.contact_sync_stop.is_set():
            self.contact_sync_event.wait()
            if self.contact_sync_stop.is_set():
                break
            self.contact_sync_event.clear()
            try:
                if self.contact_intake and self.contact_intake.enabled:
                    self.contact_intake.export_and_push()
            except ContactIntakeError:
                LOG.warning("Visitor contact export is pending; it will retry automatically.")
                if self.contact_sync_stop.wait(60):
                    break
                self.contact_sync_event.set()
            except Exception:
                LOG.warning("Visitor contact export is pending; it will retry automatically.")
                if self.contact_sync_stop.wait(60):
                    break
                self.contact_sync_event.set()

    def server_close(self):
        self.contact_sync_stop.set()
        self.contact_sync_event.set()
        super().server_close()

    def runtime_config(self):
        # The owner can add an exact new HTTPS tunnel host without restarting sync.
        try:
            return BridgeConfig.load(self.store.directory)
        except Exception:
            return self.config

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        # A traceback could contain request headers/tokens. Log a constant only.
        LOG.warning("HTTP request failed")


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AFSER-Bridge"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        # URLs, headers and source record IDs must not appear in access logs.
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": "request_rejected"})

    def respond(self, code: int, payload: dict, cors: str | None = None, preflight=False, preflight_method="GET"):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Vary", "Origin")
            if preflight:
                methods = "POST, OPTIONS" if preflight_method == "POST" else "GET, OPTIONS"
                headers = "Content-Type" if preflight_method == "POST" else "Authorization"
                self.send_header("Access-Control-Allow-Methods", methods)
                self.send_header("Access-Control-Allow-Headers", headers)
                self.send_header("Access-Control-Max-Age", "300")
                # Needed by browsers when a hosted page calls a loopback bridge.
                self.send_header("Access-Control-Allow-Private-Network", "true")
        if code == 429:
            self.send_header("Retry-After", "30")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def validate_request(self, allow_body=False) -> tuple[bool, str | None]:
        config = self.server.runtime_config()
        host = self.headers.get("Host", "")
        try:
            parsed_host = urlsplit("http://" + host)
            if parsed_host.username or parsed_host.password or parsed_host.path or parsed_host.query or parsed_host.fragment:
                raise ValueError()
            hostname = parsed_host.hostname
        except ValueError:
            self.respond(400, {"error": "invalid_host"})
            return False, None
        if hostname not in {"localhost", "127.0.0.1", *config.remote_hosts}:
            self.respond(403, {"error": "host_not_allowed"})
            return False, None
        origin = self.headers.get("Origin")
        same_origin = {f"http://localhost:{self.server.server_address[1]}", f"http://127.0.0.1:{self.server.server_address[1]}"}
        if origin is not None and origin not in {*config.origins, *same_origin}:
            self.respond(403, {"error": "origin_not_allowed"})
            return False, None
        if len(self.path) > 2048 or sum(len(k) + len(v) for k, v in self.headers.items()) > 8192:
            self.respond(413, {"error": "request_too_large"}, origin)
            return False, origin
        if self.headers.get("Transfer-Encoding"):
            self.respond(400, {"error": "request_body_not_allowed"}, origin)
            return False, origin
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isdigit():
            self.respond(400, {"error": "invalid_content_length"}, origin)
            return False, origin
        length = int(raw_length)
        if allow_body:
            if length < 1 or length > 4096:
                self.respond(413 if length > 4096 else 400, {"error": "request_too_large" if length > 4096 else "empty_request"}, origin)
                return False, origin
        elif length:
            self.respond(400, {"error": "request_body_not_allowed"}, origin)
            return False, origin
        return True, origin

    def static(self, request_path: str, origin: str | None):
        root = self.server.frontend_dir
        if root is None:
            self.respond(404, {"error": "not_found"}, origin)
            return
        decoded = unquote(request_path)
        pieces = decoded.strip("/").split("/")
        if "\\" in decoded or "\x00" in decoded or any(piece.startswith(".") for piece in pieces):
            self.respond(404, {"error": "not_found"}, origin)
            return
        target = root.joinpath(*pieces)
        if target.is_dir():
            target = target / "index.html"
        resolved = target.resolve()
        allowed_extensions = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}
        if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix not in allowed_extensions:
            self.respond(404, {"error": "not_found"}, origin)
            return
        # Even an in-root symlink is excluded, so a later retarget cannot leak data.
        current = root
        for piece in resolved.relative_to(root).parts:
            current = current / piece
            if current.is_symlink():
                self.respond(404, {"error": "not_found"}, origin)
                return
        if target.absolute() != resolved or resolved.stat().st_size > 5 * 1024 * 1024:
            self.respond(404, {"error": "not_found"}, origin)
            return
        body = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if resolved.suffix == ".html" else "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        valid, origin = self.validate_request()
        if not valid:
            return
        requested = self.headers.get("Access-Control-Request-Method", "GET").upper()
        requested_headers = {header.strip().lower() for header in self.headers.get("Access-Control-Request-Headers", "").split(",") if header.strip()}
        path = urlsplit(self.path).path
        allowed_headers = {"content-type"} if requested == "POST" and path == "/api/contact" else {"authorization"}
        if requested not in {"GET", "POST"} or (requested == "POST" and path != "/api/contact") or not requested_headers.issubset(allowed_headers):
            self.respond(403, {"error": "preflight_not_allowed"}, origin)
            return
        self.respond(200, {}, origin, preflight=True, preflight_method=requested)

    def do_GET(self):
        valid, origin = self.validate_request()
        if not valid:
            return
        parsed = urlsplit(self.path)
        # Credentials in URLs are forbidden, even if Authorization is present.
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(key.lower() in {"token", "access_token", "password", "apikey"} for key in query):
            self.respond(400, {"error": "credentials_in_url"}, origin)
            return
        if not parsed.path.startswith("/api/"):
            self.static(parsed.path, origin)
            return
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not self.server.ip_limiter.allow(self.client_address[0]):
            self.respond(429, {"error": "rate_limited"}, origin)
            return
        verified = self.server.tokens.verify(token)
        rate_key = hashlib.sha256(token.encode()).hexdigest() if verified else "unauthenticated:" + self.client_address[0]
        if not self.server.limiter.allow(rate_key):
            self.respond(429, {"error": "rate_limited"}, origin)
            return
        if parsed.path == "/api/status" and not verified:
            self.respond(200, {"service": "afser-private-map", "authenticationRequired": True}, origin)
            return
        if not verified:
            self.respond(401, {"error": "pairing_required"}, origin)
            return
        try:
            if parsed.path == "/api/status":
                result = self.server.store.status()
                from .config import load_json

                sync_state = load_json(self.server.store.directory / "sync-status.json", {})
                result["sync"] = {key: sync_state[key] for key in ("ok", "error", "lastAttemptAt") if key in sync_state}
            elif parsed.path == "/api/chapters":
                result = self.server.store.chapters()
            elif parsed.path == "/api/places":
                result = self.server.store.places(query.get("q", [""])[0])
            elif parsed.path == "/api/records":
                result = self.server.store.records(query.get("kind", ["sending"])[0], query.get("chapter", [""])[0])
            else:
                self.respond(404, {"error": "not_found"}, origin)
                return
            self.respond(200, result, origin)
        except ValueError:
            self.respond(400, {"error": "invalid_query"}, origin)
        except Exception:
            self.respond(503, {"error": "data_unavailable"}, origin)

    def do_POST(self):
        if self.command != "POST":
            self.respond(405, {"error": "method_not_allowed"})
            return
        parsed = urlsplit(self.path)
        if parsed.path != "/api/contact" or parsed.query:
            self.respond(405, {"error": "method_not_allowed"})
            return
        valid, origin = self.validate_request(allow_body=True)
        if not valid:
            return
        config = self.server.runtime_config()
        if not origin or origin not in config.origins:
            self.respond(403, {"error": "origin_not_allowed"}, origin)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.respond(415, {"error": "unsupported_content_type"}, origin)
            return
        forwarded = self.headers.get("CF-Connecting-IP", "")
        try:
            client_ip = str(ipaddress.ip_address(forwarded)) if forwarded and self.headers.get("CF-Ray") else self.client_address[0]
        except ValueError:
            client_ip = self.client_address[0]
        if not self.server.ip_limiter.allow(self.client_address[0]) or not self.server.contact_limiter.allow(client_ip):
            self.respond(429, {"error": "rate_limited"}, origin)
            return
        if not self.server.contact_intake:
            self.respond(503, {"error": "intake_unavailable"}, origin)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            accepted = self.server.contact_intake.submit(payload)
            if accepted:
                self.server.notify_contact_submission()
            self.respond(202, {"accepted": True}, origin)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.respond(400, {"error": "invalid_submission"}, origin)
        except ContactIntakeError as error:
            status = 503 if str(error) == "intake_unavailable" else 400
            self.respond(status, {"error": "intake_unavailable" if status == 503 else "invalid_submission"}, origin)
        except Exception:
            LOG.warning("Visitor contact submission could not be saved.")
            self.respond(503, {"error": "intake_unavailable"}, origin)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_HEAD = do_POST
