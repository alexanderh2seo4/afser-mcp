import json

import pytest

from afser_data.models import Chapter, SourceSnapshot
from afser_data.store import Store
from afser_data.sync import SyncManager
import afser_data.sync as sync_module


class EmptySource:
    def fetch(self):
        return SourceSnapshot([Chapter('MUC', 'Munich')], [])


def test_status_is_published_while_both_sync_locks_are_held(tmp_path, monkeypatch):
    store = Store(tmp_path)
    manager = SyncManager(store)
    other = SyncManager(store)
    write = sync_module.private_write
    statuses = []

    def checked_write(path, value):
        assert manager.running.locked()
        # A separate manager bypasses the thread lock, testing the process lock.
        assert other.run(EmptySource()) == {'ok': False, 'error': 'sync_in_progress'}
        statuses.append(json.loads(value))
        write(path, value)

    monkeypatch.setattr(sync_module, 'private_write', checked_write)
    result = manager.run(EmptySource())
    assert result['ok'] is True
    assert statuses == [result]
    assert json.loads((tmp_path / 'sync-status.json').read_text()) == result
    assert not manager.running.locked()


def test_status_write_failure_still_releases_both_locks(tmp_path, monkeypatch):
    manager = SyncManager(Store(tmp_path))
    write = sync_module.private_write

    def broken_write(*_args):
        raise OSError('fixture disk error')

    monkeypatch.setattr(sync_module, 'private_write', broken_write)
    with pytest.raises(OSError):
        manager.run(EmptySource())
    assert not manager.running.locked()
    monkeypatch.setattr(sync_module, 'private_write', write)
    assert manager.run(EmptySource())['ok'] is True


def test_failed_activation_keeps_source_metadata_with_previous_database(tmp_path):
    from afser_data.models import RawRecord
    store = Store(tmp_path)
    previous = {'fetchedAt': '2026-10-05T05:00:00Z', 'entities': {'fixture': {'complete': True}}}
    incoming = {'fetchedAt': '2026-10-05T06:00:00Z'}
    first = SourceSnapshot([Chapter('MUC', 'Munich')], [], manifest=previous)

    class First:
        def fetch(self):
            return first

    assert SyncManager(store).run(First())['ok'] is True
    stamp = store.status()['updatedAt']

    class Broken:
        def fetch(self):
            duplicate = RawRecord('fixture', 'fixture', {})
            return SourceSnapshot(first.chapters, [duplicate, duplicate], manifest=incoming)

    assert SyncManager(store).run(Broken())['ok'] is False
    with store.connect() as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='source_manifest'").fetchone()[0]) == previous
    assert json.loads((tmp_path / 'source-manifest.json').read_text()) == previous
    assert store.status()['updatedAt'] == stamp
