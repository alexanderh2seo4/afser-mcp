import json
from dataclasses import replace

import pytest

from afser_data.models import Record
from afser_data.privacy import approximate_location, project, source_link


def example(**kwargs):
    return replace(Record("source-42", "sending", "BER", True, "open", "https://www.afser.de/ereignis-liste/avtproject/42.html", latitude=52.5111, longitude=13.4011), **kwargs)


def test_projection_only_allows_approved_fields():
    value = project(example(city="Berlin", urgent=True), b"a" * 32)
    assert set(value) == {"id", "kind", "chapterId", "status", "urgent", "deadline", "country", "sourceUrl", "city", "location"}
    assert value["location"]["lat"] != 52.5111
    assert value["location"]["lon"] != 13.4011
    assert value["location"]["radiusKm"] == 10
    assert value["id"] != "source-42"


def test_home_point_contains_no_distance_information_within_grid_cell():
    # Two different home addresses in a cell yield the same output for this ID.
    a = approximate_location(52.5111, 13.4011, b"a" * 32, "home-42")
    b = approximate_location(52.5999, 13.4999, b"a" * 32, "home-42")
    assert a == b
    assert a == approximate_location(52.5111, 13.4011, b"a" * 32, "home-42")
    assert a != approximate_location(52.5111, 13.4011, b"b" * 32, "home-42")


def test_hopees_drop_city_and_home_coordinates():
    projected = project(example(kind="hopees", city="Berlin", country="Japan"), b"a" * 32)
    assert "city" not in projected and "location" not in projected
    country = project(example(kind="hopees", city="Berlin", country="Japan", latitude=36.20, longitude=138.25, location_scope="country"), b"a" * 32)
    assert country["location"] == {"lat": 36.2, "lon": 138.25, "radiusKm": 0, "scope": "country"}
    assert "city" not in country


@pytest.mark.parametrize("url", ["https://evil.example/private", "http://afser.de/42", "https://www.afser.de.evil.example/42", "https://name:password@www.afser.de/42", "https://www.afser.de:1234/42", "javascript:alert(1)"])
def test_source_links_reject_other_origins(url):
    with pytest.raises(ValueError):
        source_link(url)


def test_source_link_preserves_route_id_and_strips_login_token():
    assert source_link("https://www.afser.de/detail.php?id=42&token=secret&email=private%40example.com#private") == "https://www.afser.de/detail.php?id=42"


def test_verified_afser_list_parameters_and_unicode_chapter_preserved():
    value = "https://www.afser.de/teilnehmer-innenlisten-sf.html?view=participantslist&listKind=fwdStudents&chapter=K%C3%96L"
    assert source_link(value) == value


def test_nan_coordinates_fail_validation():
    with pytest.raises(ValueError):
        project(example(latitude=float("nan")), b"a" * 32)
