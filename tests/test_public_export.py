import json
from pathlib import Path

import pytest

from afser_data.models import Chapter, RawRecord, Record, SourceSnapshot
from afser_data.public_export import export_public, validated_record
from afser_data.store import Store


def test_export_is_active_only_and_defaults_munich_without_raw_payloads(tmp_path):
    store=Store(tmp_path/'private')
    def record(identity,chapter,active):
        return Record(identity,'sending',chapter,active,'open','https://www.afser.de/ereignis-liste/avtproject/42.html',city='München',latitude=48.14,longitude=11.58)
    snapshot=SourceSnapshot([Chapter('MUN','München'),Chapter('BER','Berlin')],[
        RawRecord('1','test',{'name':'DO_NOT_EXPORT','email':'secret@example.invalid'},record('1','MUN',True)),
        RawRecord('2','test',{'name':'ALSO_PRIVATE'},record('2','BER',True)),
        RawRecord('3','test',{},record('3','MUN',False))])
    store.activate(snapshot)
    output=tmp_path/'site'/'data'
    result=export_public(store,output)
    assert result['counts']['sending']==2
    assert json.loads((output/'manifest.json').read_text())['defaultChapterId']=='MUN'
    munich=json.loads((output/'chapters/MUN.json').read_text())['records']['sending']
    all_records=json.loads((output/'chapters/all.json').read_text())['records']['sending']
    assert len(munich)==1 and len(all_records)==2
    assert munich[0]['location']['radiusKm']==1
    text=''.join(p.read_text() for p in output.rglob('*.json'))
    assert 'DO_NOT_EXPORT' not in text and 'secret@example' not in text and 'ALSO_PRIVATE' not in text
    with pytest.raises(ValueError):export_public(store,store.directory)


def test_export_refuses_extra_private_fields_and_old_location_projection():
    record={'id':'a'*20,'kind':'sending','status':'open','chapterId':'MUN','sourceUrl':'https://www.afser.de/42','country':None}
    with pytest.raises(ValueError):validated_record({**record,'email':'secret@example.invalid'})
    with pytest.raises(ValueError):validated_record({**record,'location':{'lat':48,'lon':11,'radiusKm':10}})


def test_export_uses_timestamp_committed_with_records_not_a_newer_cache(tmp_path):
    store = Store(tmp_path / 'private')
    committed = '2026-10-05T05:00:00Z'
    snapshot = SourceSnapshot([Chapter('MUC', 'München')], [], manifest={'fetchedAt': committed})
    store.activate(snapshot)
    # Simulate a newer fetch writing its cache, but failing DB validation.
    (store.directory / 'source-manifest.json').write_text(json.dumps({'fetchedAt':'2026-10-05T06:00:00Z'}))
    output = tmp_path / 'site' / 'data'
    export_public(store, output)
    assert json.loads((output / 'manifest.json').read_text())['updatedAt'] == committed
    assert json.loads((output / 'chapters/MUC.json').read_text())['updatedAt'] == committed
