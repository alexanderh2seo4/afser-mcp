import json
import stat
import threading
from dataclasses import replace

import pytest

from afser_data.models import Chapter, Place, RawRecord, Record, SourceSnapshot
from afser_data.source import SessionExpired
from afser_data.store import Store
from afser_data.sync import SyncManager


def snapshot():
    chapters = [Chapter("BER", "Berlin"), Chapter("HAM", "Hamburg")]
    records = []
    for index, (chapter, active) in enumerate([("BER", True), ("HAM", True), ("BER", False)]):
        record = Record(str(index), "sending", chapter, active, "open", f"https://www.afser.de/interview/{index}", latitude=52.5, longitude=13.4)
        records.append(RawRecord(str(index), "interview", {"name": "Private Example", "email": "private@example.com", "street": "Hidden Road 17", "record": index}, record))
    return SourceSnapshot(chapters, records, [Place("berlin", "Berlin", "BER", 52.5, 13.4, "10115")])


def test_active_chapter_filter_runs_before_serialization(tmp_path):
    store = Store(tmp_path)
    result = store.activate(snapshot())
    assert result["counts"]["raw"] == 3
    assert result["counts"]["active"] == 2
    chapter = store.records("sending", "BER")
    assert len(chapter["records"]) == 1
    assert all(row["chapterId"] == "BER" for row in chapter["records"])
    assert len(store.records("sending", "all")["records"]) == 2
    public = json.dumps(chapter)
    assert "Private Example" not in public and "private@example.com" not in public and "Hidden Road" not in public
    # Test fixture raw data really was retained, but has no read API.
    with store.connect() as db:
        assert "Private Example" in db.execute("SELECT payload FROM raw_records LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError):
        store.records("sending", "unknown")


def test_failed_page_does_not_activate_partial_snapshot(tmp_path):
    store = Store(tmp_path)
    store.activate(snapshot())
    previous = store.status()["updatedAt"]

    def interrupted():
        yield snapshot().records[0]
        raise SessionExpired("private source content must never reach logs")

    class BrokenSource:
        def fetch(self):
            return SourceSnapshot(snapshot().chapters, interrupted())

    result = SyncManager(store).run(BrokenSource())
    assert result["error"] == "source_session_expired"
    assert "private source content" not in json.dumps(result)
    assert store.status()["updatedAt"] == previous
    assert len(store.records("sending", "all")["records"]) == 2
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_duplicate_raw_identity_rejects_entire_new_snapshot(tmp_path):
    store = Store(tmp_path)
    initial = snapshot()
    store.activate(initial)
    duplicate = SourceSnapshot(initial.chapters, [initial.records[0], initial.records[0]])
    with pytest.raises(Exception):
        store.activate(duplicate)
    assert len(store.records("sending", "all")["records"]) == 2


def test_private_permissions_and_local_place_search(tmp_path):
    directory = tmp_path / "private"
    store = Store(directory)
    store.activate(snapshot())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / "projection.key").stat().st_mode) == 0o600
    by_city = store.places("ber")
    by_postal = store.places("10115")
    assert by_city == by_postal
    assert by_city["places"][0]["chapterId"] == "BER"
    assert "postal_code" not in json.dumps(by_postal)


def test_city_results_merge_postal_centroids_and_disambiguate_chapters(tmp_path):
    store = Store(tmp_path)
    source = snapshot()
    source.places = [Place("a", "Neustadt", "BER", 52.4, 13.3, "10110"), Place("b", "Neustadt", "BER", 52.6, 13.5, "10111"), Place("c", "Neustadt", "HAM", 53.5, 10.0, "20110")]
    store.activate(source)
    places = store.places("Neustadt")["places"]
    assert len(places) == 2
    assert [place["label"] for place in places] == ["Neustadt · Berlin", "Neustadt · Hamburg"]
    assert places[0]["location"] == {"lat": 52.5, "lon": 13.4}


def test_readers_keep_previous_complete_snapshot_during_slow_ingestion(tmp_path):
    store = Store(tmp_path)
    source = snapshot()
    store.activate(source)
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def incoming():
        yield source.records[0]
        entered.set()
        release.wait(3)

    writer = threading.Thread(target=store.activate, args=(SourceSnapshot(source.chapters, incoming()),))
    writer.start()
    assert entered.wait(2)

    def read():
        observed.append(len(store.records("sending", "all")["records"]))
        read_done.set()

    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert read_done.wait(1), "reader was blocked by ingestion"
        assert observed == [2]
    finally:
        release.set()
        writer.join(timeout=4)
        reader.join(timeout=4)
    assert len(store.records("sending", "all")["records"]) == 1


def test_unassigned_open_tasks_are_all_only_and_do_not_create_a_chapter(tmp_path):
    source = snapshot()
    open_task = replace(source.records[0].normalized, source_id="unassigned-task", chapter_id="unassigned", latitude=None, longitude=None)
    source.records.append(RawRecord("unassigned-task", "interview", {"task": "synthetic unassigned"}, open_task))
    store = Store(tmp_path)
    store.activate(source)
    assert {chapter["id"] for chapter in store.chapters()["chapters"]} == {"BER", "HAM"}
    all_records = store.records("sending", "all")["records"]
    assert len(all_records) == 3
    unassigned = next(record for record in all_records if record["chapterId"] == "unassigned")
    assert "location" not in unassigned
    assert len(store.records("sending", "BER")["records"]) == 1
    with pytest.raises(ValueError):
        store.records("sending", "unassigned")
    invalid = replace(open_task, chapter_id="fabricated")
    with pytest.raises(ValueError):
        store.activate(SourceSnapshot(source.chapters, [RawRecord("bad", "interview", {}, invalid)]))
    assert len(store.records("sending", "all")["records"]) == 3
