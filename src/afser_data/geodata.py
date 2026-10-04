"""Offline joins against public locality/country data, never address geocoding.

GeoNames postal coordinates are public postal-area centroids (CC BY 4.0).
Natural Earth country label points are public domain. Neither download request
contains an AFSer record, a volunteer location, or a household address.
"""

import io
import json
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

from .config import private_write
from .models import Place

POSTCODE_URL = "https://download.geonames.org/export/zip/DE.zip"
COUNTRY_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
ATTRIBUTION = (
    {"name": "GeoNames", "url": "https://www.geonames.org/", "license": "CC BY 4.0", "licenseUrl": "https://creativecommons.org/licenses/by/4.0/"},
    {"name": "Natural Earth", "url": "https://www.naturalearthdata.com/", "license": "Public domain"},
)


def text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower())


def chapter_id(code: str) -> str:
    """Keep canonical AFSer committee codes compatible with public identifiers."""
    return text_key(code).upper()


class GeoData:
    def __init__(self, private_dir: Path):
        self.directory = private_dir / "geodata"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.postcodes: dict[str, tuple[str, float, float]] = {}
        self.countries: dict[str, tuple[str, float, float]] = {}

    def _public_file(self, name: str, url: str, download: bool) -> bytes | None:
        path = self.directory / name
        if path.exists():
            if path.is_symlink():
                raise ValueError("geodata_symlink")
            path.chmod(0o600)
            return path.read_bytes()
        if not download:
            return None
        request = Request(url, headers={"User-Agent": "AFSER-Private-Map/0.1 (public geodata only)"})
        with urlopen(request, timeout=60) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("geodata_too_large")
        private_write(path, payload)
        return payload

    def load(self, download: bool = True):
        postal = self._public_file("DE.zip", POSTCODE_URL, download)
        grouped = defaultdict(list)
        if postal:
            with zipfile.ZipFile(io.BytesIO(postal)) as archive:
                # Read one fixed entry into memory; never extract arbitrary paths.
                for line in archive.read("DE.txt").decode("utf-8").splitlines():
                    cells = line.split("\t")
                    if len(cells) < 11 or cells[0] != "DE" or not re.fullmatch(r"\d{5}", cells[1]):
                        continue
                    try:
                        lat, lon = float(cells[9]), float(cells[10])
                    except ValueError:
                        continue
                    if 47 <= lat <= 56 and 5 <= lon <= 16:
                        grouped[cells[1]].append((cells[2], lat, lon))
            for code, rows in grouped.items():
                # A postcode can have multiple localities. Centroid and city come
                # only from this public lookup, never from a private address.
                cities = sorted({row[0] for row in rows})
                city = cities[0] if len(cities) == 1 else " / ".join(cities[:2])
                self.postcodes[code] = (city[:100], sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows))
        # 50m includes smaller exchange destinations omitted by the 110m file.
        country = self._public_file("countries-50m.geojson", COUNTRY_URL, download)
        if country:
            for feature in json.loads(country).get("features", []):
                properties = feature.get("properties", {})
                try:
                    lat, lon = float(properties["LABEL_Y"]), float(properties["LABEL_X"])
                except (KeyError, TypeError, ValueError):
                    continue
                label = properties.get("NAME_DE") or properties.get("NAME_EN") or properties.get("NAME")
                if not isinstance(label, str):
                    continue
                entry = (label, lat, lon)
                for field in ("NAME", "ADMIN", "NAME_DE", "NAME_EN", "NAME_LONG", "FORMAL_EN", "ISO_A2", "ISO_A3", "POSTAL"):
                    value = properties.get(field)
                    if isinstance(value, str) and value and value != "-99":
                        self.countries[text_key(value)] = entry
            aliases = {
                "USA": "United States of America", "Vereinigte Staaten": "United States of America",
                "UK": "United Kingdom", "England": "United Kingdom", "Großbritannien": "United Kingdom",
                "Tschechien": "Czechia", "Südkorea": "South Korea", "Korea": "South Korea",
                "Russland": "Russia", "Hong Kong": "China", "Bosnien": "Bosnia and Herzegovina",
                "Mazedonien": "North Macedonia", "Eswatini": "eSwatini",
            }
            for alias, name in aliases.items():
                if text_key(name) in self.countries:
                    self.countries[text_key(alias)] = self.countries[text_key(name)]
        private_write(self.directory / "attribution.json", json.dumps(ATTRIBUTION, ensure_ascii=False, indent=2))
        return self

    def postcode(self, value) -> str | None:
        if value is None:
            return None
        # Interpret locally; never send the address to a geocoding provider.
        candidates = {code for code in re.findall(r"(?<!\d)\d{5}(?!\d)", str(value)) if code in self.postcodes}
        return next(iter(candidates)) if len(candidates) == 1 else None

    def locality(self, value) -> tuple[str, float, float] | None:
        code = self.postcode(value)
        return self.postcodes.get(code) if code else None

    def country(self, value) -> tuple[str, float, float] | None:
        if not isinstance(value, str):
            return None
        key = text_key(value)
        direct = self.countries.get(key)
        if direct:
            return direct
        # AFSer uses some longer formal names or names followed by partner
        # qualifiers. Resolve against public country aliases only. Ignore short
        # ISO codes during substring matching and require a unique longest name.
        matches = [(len(alias), entry) for alias, entry in self.countries.items()
                   if len(alias) >= 5 and alias in key]
        if matches:
            longest = max(length for length, _ in matches)
            entries = {entry for length, entry in matches if length == longest}
            if len(entries) == 1:
                return next(iter(entries))
        # Abbreviations plus a program qualifier (e.g. USA (program)) retain
        # word boundaries; never interpret a two-letter substring of a name.
        codes = {self.countries[text_key(code)] for code in re.findall(r"(?<!\w)[A-Z]{2,3}(?!\w)", value)
                 if text_key(code) in self.countries}
        if len(codes) == 1:
            return next(iter(codes))
        return None

    def places(self, chapter_postcodes: dict[str, str]) -> list[Place]:
        return [Place(code, city, chapter_postcodes.get(code), lat, lon, code)
                for code, (city, lat, lon) in sorted(self.postcodes.items())]
