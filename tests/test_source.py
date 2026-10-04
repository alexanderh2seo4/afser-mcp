from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from afser_data.afser import AfserSource, parse_date
from afser_data.geodata import chapter_id
from afser_data.privacy import project
from afser_data.source import IncompleteSource, SourceError
from afser_data.store import Store


@pytest.fixture
def source(tmp_path):
    adapter = AfserSource({}, tmp_path)
    adapter.chapter_codes = {"BER": "BER", "HAM": "HAM"}
    adapter.geo.postcodes = {"10115": ("Berlin", 52.52, 13.4)}
    adapter.geo.countries = {"japan": ("Japan", 36.2, 138.25)}
    return adapter


def participant(**changes):
    return {"Id": "synthetic-id", "Chapter__r": {"Chapter_Code__c": "BER"}, "Status__c": "Participation", "ProgramStartDateDATE__c": "2026-08-01", "ProgramEndDateDATE__c": "2027-07-01", "Travel_Country__r": {"Name": "Japan"}, **changes}


def page(project_id, owner=True, pagination="", selected_extra=""):
    role_class = "freeowner" if owner else "assignedowner"
    return f'''<input name="filter[state][]" value="1" checked>
    {selected_extra}<table><tr><td></td><td><a href="/ereignis-liste/avtproject/{project_id}.html">Sending Homeinterview in 10115 Berlin</a></td><td>BER</td><td>2026/27</td><td>01.10.2026 - 10.10.2026</td><td></td></tr>
    <tr class="expand-{project_id}"><td><div class="row state_1"><h4>Sending Homeinterview</h4><div class="{role_class}"><a href="/ereignis-liste/avttaskform/addOwner/{project_id}.html">Join</a></div></div></td></tr></table>{pagination}'''


def test_business_dates_and_unicode_chapters():
    assert parse_date("05.10.2026") == date(2026, 10, 5)
    assert parse_date("2026-10-05T12:40:00") == date(2026, 10, 5)
    assert parse_date("invalid") is None
    assert chapter_id("KÖL") == "KOL"
    assert chapter_id("DÜS") == "DUS"


def test_expired_and_unknown_status_programs_are_excluded(source):
    today = date(2026, 10, 5)
    assert source._hopee(participant(), "getAllStudents", today) is not None
    for changes in ({"Status__c": "Cancelled"}, {"ProgramEndDateDATE__c": "2026-01-01"}, {"Status__c": "Unrecognized"}, {"ProgramStartDateDATE__c": None}):
        assert source._hopee(participant(**changes), "getAllStudents", today) is None


def test_active_hopee_only_projects_public_destination_country(source):
    record = source._hopee(participant(), "getAllStudents", date(2026, 10, 5))
    public = project(record, b"a" * 32)
    assert public["country"] == "Japan"
    assert public["location"]["scope"] == "country"
    assert "city" not in public
    assert public["sourceUrl"].startswith("https://www.afser.de/schueler-innenliste-sf.html?")
    assert "listKind=sendingStudents" in public["sourceUrl"]


def test_active_hopee_with_unknown_country_stays_visible_without_coordinates(source):
    record = source._hopee(participant(Travel_Country__r={"Name": "Unknown destination"}), "getAllStudents", date(2026, 10, 5))
    assert record is not None
    public = project(record, b"a" * 32)
    assert public["country"] is None
    assert "location" not in public and "city" not in public
    assert public["sourceUrl"].startswith("https://www.afser.de/")


def test_capped_student_api_union_verifies_complete_chapter_partitions(source):
    a = {"Id": "a", "Chapter__r": {"Chapter_Code__c": "BER"}}
    b = {"Id": "b", "Chapter__r": {"Chapter_Code__c": "HAM"}}
    partitions = {"BER": {"records": [a], "done": True, "totalSize": 1}, "HAM": {"records": [b], "done": True, "totalSize": 1}}
    source._task = lambda task, code: partitions[code]
    chapters = [{"Chapter_Code__c": "BER"}, {"Chapter_Code__c": "HAM"}]
    complete = source._complete_task("getAllStudents", {"records": [a], "done": False, "totalSize": 2}, chapters)
    assert {record["Id"] for record in complete} == {"a", "b"}
    with pytest.raises(IncompleteSource):
        source._complete_task("getAllStudents", {"records": [a], "done": False, "totalSize": 3}, chapters)
    partitions["BER"] = {"records": [b], "done": True, "totalSize": 1}
    with pytest.raises(IncompleteSource):
        source._complete_task("getAllStudents", {"records": [a], "done": False, "totalSize": 2}, chapters)


def test_student_status_slices_recover_records_without_assigned_chapter(source):
    a = {"Id": "a", "Chapter__r": {"Chapter_Code__c": "BER"}, "Status__c": "Preparation", "Program_Year__c": "2026"}
    b = {"Id": "b", "Chapter__r": {"Chapter_Code__c": "HAM"}, "Status__c": "Preparation", "Program_Year__c": "2026"}
    unassigned = {"Id": "unassigned", "Chapter__r": None, "Status__c": "Preparation", "Program_Year__c": "2026"}
    calls = []

    def api(task, chapter=None, filters=None):
        calls.append((chapter, filters))
        rows = [a] if chapter == "BER" else [b] if chapter == "HAM" else [a, b, unassigned]
        return {"records": rows, "done": True, "totalSize": len(rows)}

    source._task = api
    chapters = [{"Chapter_Code__c": "BER"}, {"Chapter_Code__c": "HAM"}]
    rows = source._complete_task("getAllStudents", {"records": [a], "done": False, "totalSize": 3}, chapters)
    assert {row["Id"] for row in rows} == {"a", "b", "unassigned"}
    assert (None, {"status": "Preparation"}) in calls


def test_board_clears_filters_follows_all_pages_and_requires_open_owner_slot(source):
    first = page(42, pagination='<div class="pagination">Seite 1 von 2 <a href="/avt/avtprojects.html?start=20">2</a></div>')
    second = page(43, owner=False, pagination='<div class="pagination">Seite 2 von 2</div>')
    paths = []

    def fetch(path):
        paths.append(path)
        offset = parse_qs(urlsplit(path).query)["start"][0]
        return first if offset == "0" else second

    source._get_html = fetch
    raw = source._board(date(2026, 10, 5))
    mapped = [record.normalized for record in raw if record.normalized]
    assert len(mapped) == 1
    assert mapped[0].source_url == "https://www.afser.de/ereignis-liste/avtproject/42.html"
    assert mapped[0].urgent and mapped[0].deadline == "2026-10-10"
    assert mapped[0].city == "Berlin"
    assert len(paths) == 2
    for path in paths:
        query = parse_qs(urlsplit(path).query, keep_blank_values=True)
        assert query["filter[komitee]"] == [""]
        assert query["filter[templates]"] == [""]
        assert query["filter[programms]"] == [""]
        assert query["filter[state][]"] == ["1"]
        assert "addOwner" not in path
    assert len([record for record in raw if record.entity_type == "avtBoardPage"]) == 2


def test_board_rejects_saved_filters_or_missing_pages(source):
    source._get_html = lambda path: page(42, selected_extra='<input name="filter[komitee][]" value="BER" checked>')
    with pytest.raises(IncompleteSource):
        source._board(date(2026, 10, 5))
    source._get_html = lambda path: page(42, pagination='<div class="pagination">Seite 1 von 2</div>')
    with pytest.raises(IncompleteSource):
        source._board(date(2026, 10, 5))


def test_board_div_role_avt_enrollment_path_and_owner_action_column(source):
    content = page(42).replace("<h4>Sending Homeinterview</h4>", "<div>Sending Homeinterview</div>").replace("/ereignis-liste/avttaskform/addOwner/", "/avt/avttaskform/addOwner/").replace("<td></td></tr>", "<td></td><td>Owner action</td></tr>", 1)
    source._get_html = lambda path: content
    raw = source._board(date(2026, 10, 5))
    records = [item.normalized for item in raw if item.normalized]
    assert len(records) == 1
    assert records[0].source_url == "https://www.afser.de/ereignis-liste/avtproject/42.html"


def test_source_never_performs_signup_request(source):
    with pytest.raises(SourceError):
        source._request("https://www.afser.de/ereignis-liste/avttaskform/addOwner/42.html")


def test_public_country_formal_aliases_and_bounded_codes_avoid_false_substrings(source):
    usa = ("Vereinigte Staaten", 39.0, -98.0)
    guinea = ("Guinea", 10.0, -10.0)
    bissau = ("Guinea-Bissau", 12.0, -15.0)
    source.geo.countries = {"unitedstatesofamerica": usa, "usa": usa, "us": usa, "guinea": guinea, "guineabissau": bissau, "br": ("Brasilien", -10.0, -52.0), "canada": ("Kanada", 56.0, -106.0), "france": ("Frankreich", 47.0, 2.0)}
    assert source.geo.country("United States of America (partner program)") == usa
    assert source.geo.country("USA (partner program)") == usa
    assert source.geo.country("Guinea-Bissau (exchange)") == bissau
    assert source.geo.country("Austria") is None
    assert source.geo.country("US / BR") is None
    assert source.geo.country("Canada and France") is None


def test_verified_open_board_task_without_chapter_stays_unlocated_for_all(source):
    content = page(42).replace("10115 Berlin", "").replace("<td>BER</td>", "<td></td>")
    source._get_html = lambda path: content
    raw = source._board(date(2026, 10, 5))
    records = [item.normalized for item in raw if item.normalized]
    assert len(records) == 1
    record = records[0]
    assert record.chapter_id == "unassigned"
    assert record.city is None and record.latitude is None and record.longitude is None
    assert record.source_url == "https://www.afser.de/ereignis-liste/avtproject/42.html"


def test_two_hostees_in_one_family_keep_all_assignments_and_one_household_projection(source, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 12, tzinfo=timezone.utc)

    monkeypatch.setattr("afser_data.afser.datetime", FixedClock)
    source.chapter_codes = {}
    source._login = lambda: None
    source.geo.load = lambda **kwargs: source.geo
    source._postcode_chapters = lambda chapters: {"10115": "BER"}
    source._board = lambda today: []
    family = {"Id": "family-one", "Responsible_Chapter__r": {"Chapter_Code__c": "BER"}, "Status__c": "Participation", "Postal_Code__c": "10115"}
    assignments = [
        {"Id": "assignment-a", "Application__r": participant(Id="hostee-a"), "Hosting_Family__r": family},
        {"Id": "assignment-b", "Application__r": participant(Id="hostee-b"), "Hosting_Family__r": family},
    ]

    def api(task, chapter=None):
        records = [{"Id": "chapter-one", "Chapter_Code__c": "BER", "Name": "Berlin"}] if task == "getAllChapters" else assignments if task == "getAllFamiliesWithStudents" else []
        return {"records": records, "done": True, "totalSize": len(records)}

    source._task = api
    snapshot = source.fetch()
    assert len([record for record in snapshot.records if record.entity_type == "getAllFamiliesWithStudents"]) == 2
    assert len([record for record in snapshot.records if record.entity_type == "hostingFamily"]) == 1
    store = Store(source.private_dir)
    store.activate(snapshot)
    assert len(store.records("hostees", "BER")["records"]) == 2
    assert len(store.records("families", "BER")["records"]) == 1
