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
    """Show a 1 km circle around a stable random public-locality point.

    The adapter supplies public postal-area centroids, never house coordinates.
    Stable keyed randomness prevents successive exports from revealing a base
    point by averaging independently generated offsets.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid_coordinates")
    digest = hmac.digest(salt, f"locality-point-v2:{identity}".encode(), "sha256")
    bearing = int.from_bytes(digest[:8], "big") / 2**64 * 2 * math.pi
    distance = (0.2 + 0.7 * math.sqrt(int.from_bytes(digest[8:16], "big") / 2**64)) / 6371
    lat, lon = math.radians(latitude), math.radians(longitude)
    result_lat = math.asin(math.sin(lat) * math.cos(distance) + math.cos(lat) * math.sin(distance) * math.cos(bearing))
    result_lon = lon + math.atan2(math.sin(bearing) * math.sin(distance) * math.cos(lat), math.cos(distance) - math.sin(lat) * math.sin(result_lat))
    return {"lat": round(math.degrees(result_lat), 4), "lon": round((math.degrees(result_lon) + 180) % 360 - 180, 4), "radiusKm": 1}


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
