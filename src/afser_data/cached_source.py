"""Rebuild projections from existing private caches without network access."""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from .afser import AfserSource
from .config import load_json, private_write
from .geodata import GeoData, chapter_id
from .source import IncompleteSource, SourceError
from .store import now


class OfflineGeoData(GeoData):
    def load(self, download=False):
        for name in ('DE.zip', 'countries-50m.geojson'):
            if not (self.directory / name).is_file():
                raise IncompleteSource()
        return super().load(download=False)


class CachedSource(AfserSource):
    def __init__(self, config, directory):
        super().__init__(config, directory)
        self.geo = OfflineGeoData(directory)

    def _login(self):
        pass

    def _request(self, *args, **kwargs):
        raise SourceError()

    def _task(self, task, chapter=None, filters=None):
        filters = filters or {}
        suffix = '-' + chapter_id(chapter) if chapter is not None else ''
        if filters:
            suffix += '-filter-' + hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]
        path = self.cache_dir / (task + suffix + '.json')
        if not path.exists() and chapter is None and not filters:
            path = self.private_dir / 'initial' / (task + '.json')
        response = load_json(path)
        self._records(response)
        return response

    def _get_html(self, path):
        parsed = urlsplit(path)
        if parsed.path != '/avt/avtprojects.html':
            raise SourceError()
        offset = parse_qs(parsed.query).get('start', ['0'])[0]
        if not offset.isdigit():
            raise IncompleteSource()
        file = self.cache_dir / ('avt-board-' + offset + '.html')
        if not file.is_file() or file.is_symlink():
            raise IncompleteSource()
        return file.read_text()

    def _postcode_chapters(self, chapters):
        mapping = load_json(self.private_dir / 'chapter-postcodes.json', {}).get('mapping', {})
        if not mapping:
            raise IncompleteSource()
        return {k: v for k, v in mapping.items() if v in self.chapter_codes and re.fullmatch(r'\d{5}', k)}

    def fetch(self):
        old = load_json(self.private_dir / 'source-manifest.json', {})
        snapshot = super().fetch()
        self.manifest['fetchedAt'] = old.get('fetchedAt', self.manifest['fetchedAt'])
        self.manifest['reprojectedAt'] = now()
        private_write(self.private_dir / 'source-manifest.json', json.dumps(self.manifest, ensure_ascii=False))
        return snapshot
