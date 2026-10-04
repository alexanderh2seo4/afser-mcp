"""Read-only source extension and bounded, host-restricted JSON API helper."""

import json
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import load_json
from .models import SourceSnapshot
from .privacy import SOURCE_HOSTS


class SourceError(Exception):
    code = "source_unavailable"


class SourceNotConfigured(SourceError):
    code = "source_not_configured"


class SessionExpired(SourceError):
    code = "source_session_expired"


class IncompleteSource(SourceError):
    code = "source_incomplete"


class SourceAdapter(Protocol):
    def fetch(self) -> SourceSnapshot: ...


class RestrictedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate_url(newurl)
        if "login" in urlsplit(newurl).path.lower():
            raise SessionExpired()
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    @staticmethod
    def validate_url(url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in SOURCE_HOSTS or parts.username or parts.password or parts.port not in (None, 443):
            raise SourceError()


class JsonReader:
    """Credentials stay in memory; requests and source values are never logged."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = {"Accept": "application/json", "User-Agent": "AFSER-Private-Map/0.1 (read-only)", **(headers or {})}
        self.opener = build_opener(RestrictedRedirects())

    def get(self, url: str):
        RestrictedRedirects.validate_url(url)
        try:
            with self.opener.open(Request(url, headers=self.headers, method="GET"), timeout=45) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise IncompleteSource()
                if "json" not in response.headers.get("Content-Type", ""):
                    raise SessionExpired()
                return json.loads(body)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise SessionExpired() from None
            raise SourceError() from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise SourceError() from None

    def pages(self, first_url: str, items_key="items", next_key="next", maximum=10000):
        """Traverse all pages; cycles/partial pages fail the whole new snapshot."""
        next_url = first_url
        visited = set()
        for _ in range(maximum):
            if next_url in visited:
                raise IncompleteSource()
            visited.add(next_url)
            page = self.get(next_url)
            if not isinstance(page, dict) or not isinstance(page.get(items_key), list):
                raise IncompleteSource()
            yield from page[items_key]
            next_url = page.get(next_key)
            if not next_url:
                return
            if not isinstance(next_url, str):
                raise IncompleteSource()
        raise IncompleteSource()


def build_adapter(directory: Path) -> SourceAdapter:
    config = load_json(directory / "source.json", {})
    if config.get("adapter") != "afser":
        raise SourceNotConfigured()
    try:
        from .afser import AfserSource
    except ImportError:
        raise SourceNotConfigured() from None
    return AfserSource(config, directory)
