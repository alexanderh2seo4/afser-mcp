from dataclasses import dataclass, field
from typing import Any, Iterable

KINDS = frozenset({"sending", "hopees", "hostees", "families"})


@dataclass(frozen=True)
class Chapter:
    id: str
    name: str


@dataclass(frozen=True)
class Place:
    """A public city/postal centroid, never a person's home address."""

    id: str
    city: str
    chapter_id: str | None
    latitude: float
    longitude: float
    postal_code: str | None = None
    region: str = ""


@dataclass(frozen=True)
class Record:
    source_id: str
    kind: str
    chapter_id: str
    active: bool
    status: str
    source_url: str
    urgent: bool = False
    deadline: str | None = None
    country: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_scope: str = "area"


@dataclass(frozen=True)
class RawRecord:
    source_id: str
    entity_type: str
    payload: Any
    normalized: Record | None = None


@dataclass
class SourceSnapshot:
    chapters: list[Chapter]
    records: Iterable[RawRecord]
    places: list[Place] = field(default_factory=list)
    manifest: dict | None = None
