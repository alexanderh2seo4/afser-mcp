"""The only source-record projection used by either HTTP or MCP."""

import hashlib
import hmac
import math
import re
from datetime import date
from urllib.parse import urlsplit, urlunsplit

from .models import KINDS, Record

SOURCE_HOSTS = frozenset({"afser.de", "www.afser.de"})
PUBLIC_STATUS = frozenset({"open", "needed", "assigned", "active", "pending"})


def source_link(value: str) -> str:
    """Source links cannot redirect to arbitrary websites or carry login tokens."""
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname not in SOURCE_HOSTS:
        raise ValueError("invalid_source_url")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("invalid_source_url")
    # AFSER uses query parameters for record routing. Keep only simple IDs/actions.
    from urllib.parse import parse_qsl, urlencode

    permitted = {"id", "pid", "fid", "hid", "sid", "oid", "p", "page", "action", "module", "task", "cmd", "go", "view", "listkind", "chapter", "option", "controller", "itemid", "interview", "participant", "hoped", "hosted", "family", "asp_id", "ggp_id", "hopee_id", "hostee_id", "hostfamily_id"}
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() in permitted and re.fullmatch(r"[\w.:/-]{1,100}", v)]
    return urlunsplit(("https", parts.netloc, parts.path or "/", urlencode(query), ""))


def _short(value: str | None, maximum: int = 100) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_public_field")
    return value


def approximate_location(latitude: float, longitude: float, salt: bytes, identity: str) -> dict:
    """Replace a home coordinate with a stable random point in a ~7 km grid.

    Output depends only on the grid cell and a secret per-record key, never on
    distance to the home within that cell. radiusKm describes the approximation.
    The displayed point is not a household address and must not be geocoded back.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid_coordinates")
    grid = 0.1
    lat_cell = math.floor(latitude / grid)
    lon_cell = math.floor(longitude / grid)
    digest = hmac.digest(salt, f"point:{identity}:{lat_cell}:{lon_cell}".encode(), "sha256")
    lat_fraction = 0.25 + int.from_bytes(digest[:4], "big") / 2**32 * 0.5
    lon_fraction = 0.25 + int.from_bytes(digest[4:8], "big") / 2**32 * 0.5
    return {"lat": round((lat_cell + lat_fraction) * grid, 3), "lon": round((lon_cell + lon_fraction) * grid, 3), "radiusKm": 10}


def project(record: Record, salt: bytes) -> dict:
    if record.kind not in KINDS or record.status not in PUBLIC_STATUS:
        raise ValueError("invalid_public_classification")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record.chapter_id):
        raise ValueError("invalid_chapter")
    identity = f"{record.kind}:{record.source_id}"
    opaque_id = hmac.new(salt, identity.encode(), hashlib.sha256).hexdigest()[:20]
    deadline = None
    if record.deadline:
        deadline = date.fromisoformat(record.deadline).isoformat()
    public = {
        "id": opaque_id,
        "kind": record.kind,
        "chapterId": record.chapter_id,
        "status": record.status,
        "urgent": bool(record.urgent),
        "deadline": deadline,
        "country": _short(record.country),
        "sourceUrl": source_link(record.source_url),
    }
    # Outgoing students show only destination country and chapter, per request.
    if record.kind == "hopees":
        if record.location_scope == "country" and record.latitude is not None and record.longitude is not None:
            if not (-90 <= record.latitude <= 90 and -180 <= record.longitude <= 180):
                raise ValueError("invalid_country_coordinates")
            public["location"] = {"lat": round(record.latitude, 2), "lon": round(record.longitude, 2), "radiusKm": 0, "scope": "country"}
    else:
        if record.city:
            public["city"] = _short(record.city)
        if record.latitude is not None and record.longitude is not None:
            public["location"] = approximate_location(record.latitude, record.longitude, salt, identity)
    return public
