"""Explicit anonymous static export; raw_records is never queried here."""
import json
import hashlib
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from .config import load_json
from .models import KINDS
from .privacy import PUBLIC_STATUS, source_link

FIELDS = frozenset({'id','kind','chapterId','status','urgent','deadline','country','sourceUrl','city','location','hasOpenRoles','pickedAt'})


def validated_record(record):
    if not isinstance(record, dict) or not set(record) <= FIELDS:
        raise ValueError('unapproved_public_field')
    if not re.fullmatch(r'[0-9a-f]{20}', record.get('id', '')) or record.get('kind') not in KINDS or record.get('status') not in PUBLIC_STATUS:
        raise ValueError('invalid_public_record')
    if source_link(record['sourceUrl']) != record['sourceUrl']:
        raise ValueError('unsafe_public_source_link')
    if 'hasOpenRoles' in record and (record['kind'] != 'sending' or type(record['hasOpenRoles']) is not bool):
        raise ValueError('invalid_interview_roles')
    if 'pickedAt' in record:
        if record['kind'] != 'sending' or record['status'] != 'assigned' or date.fromisoformat(record['pickedAt']).isoformat() != record['pickedAt']:
            raise ValueError('invalid_pickup_metadata')
    location = record.get('location')
    if location:
        if set(location) - {'lat','lon','radiusKm','scope'}:
            raise ValueError('unapproved_location_field')
        expected = 0 if record['kind'] == 'hopees' else 1
        if location.get('radiusKm') != expected:
            raise ValueError('projection_needs_rebuild')
    if record['kind'] == 'hopees' and ('city' in record or location and location.get('scope') != 'country'):
        raise ValueError('invalid_country_projection')
    return record


def export_public(store, output: Path):
    output = output.resolve()
    if output.is_relative_to(store.directory.resolve()) or store.directory.resolve().is_relative_to(output):
        raise ValueError('private_export_target')
    if output.exists():
        if not (output / 'manifest.json').is_file() or json.loads((output / 'manifest.json').read_text()).get('privacy') != 'randomized-public-locality; 1km-circles; no-names-or-addresses':
            raise ValueError('unmanaged_export_target')
    output.parent.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(lambda: {kind: [] for kind in sorted(KINDS)})
    counts = {kind: 0 for kind in sorted(KINDS)}
    with store.connect(read_snapshot=True) as db:
        snapshot = store._active(db)
        if snapshot is None:
            raise ValueError('no_complete_snapshot')
        chapters = [dict(row) for row in db.execute('SELECT id,name FROM chapters WHERE snapshot=? ORDER BY name', (snapshot,))]
        chapter_ids = {c['id'] for c in chapters}
        for row in db.execute('SELECT body FROM public_records WHERE snapshot=? AND active=1 ORDER BY urgent DESC,id', (snapshot,)):
            record = validated_record(json.loads(row[0]))
            if record['chapterId'] not in chapter_ids | {'unassigned'}:
                raise ValueError('unknown_public_chapter')
            grouped['all'][record['kind']].append(record)
            if record['chapterId'] in chapter_ids:
                grouped[record['chapterId']][record['kind']].append(record)
            counts[record['kind']] += 1
        places = [[r['id'],r['city'],r['chapter'],round(r['latitude'],4),round(r['longitude'],4),r['postal_code']] for r in db.execute('SELECT * FROM places WHERE snapshot=? ORDER BY city,id', (snapshot,))]
        updated = db.execute('SELECT created_at FROM snapshots WHERE id=?', (snapshot,)).fetchone()[0]
        metadata = db.execute("SELECT value FROM meta WHERE key='source_manifest'").fetchone()
        # Old databases predate transactional source metadata; preserve their
        # clock until the next successful import migrates it automatically.
        committed = json.loads(metadata[0]) if metadata else load_json(store.directory / 'source-manifest.json', {})
        source_time = committed.get('fetchedAt', updated)
    munich = next((c for c in chapters if c['name'] == 'München'), None)
    if munich is None or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', c['id']) for c in chapters):
        raise ValueError('invalid_public_chapter_catalog')
    city_points = [p for p in places if p[1] == 'München' and p[2] == munich['id']]
    residence = {'chapterId': munich['id'], 'city':'München · Standard'}
    if city_points:
        residence['location'] = {'lat':sum(p[3] for p in city_points)/len(city_points),'lon':sum(p[4] for p in city_points)/len(city_points)}
    manifest = {'version':1,'updatedAt':source_time,'chapters':chapters,'counts':counts,'defaultChapterId':munich['id'],'defaultResidence':residence,'privacy':'randomized-public-locality; 1km-circles; no-names-or-addresses'}
    generation = hashlib.sha256(json.dumps([manifest,dict(grouped),places],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    manifest['generation'] = generation
    stage = Path(tempfile.mkdtemp(prefix='.public-export-', dir=output.parent))
    try:
        (stage / 'chapters').mkdir()
        def write(path, value):
            path.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n')
        write(stage / 'manifest.json', manifest)
        write(stage / 'places.json', {'places':places,'generation':generation})
        for chapter in [c['id'] for c in chapters]+['all']:
            write(stage / 'chapters' / (chapter+'.json'), {'chapter':chapter,'updatedAt':source_time,'generation':generation,'records':grouped[chapter]})
        if output.exists():
            shutil.rmtree(output)
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'exported':True,'counts':counts,'chapters':len(chapters),'defaultChapter':munich['name'],'files':len(chapters)+3}
