import io
import zipfile

from afser_data.geodata import GeoData


def test_all_localities_survive_shared_postcode_and_repeated_load(tmp_path):
    geo = GeoData(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("DE.txt", "\n".join(
            f"DE\t12345\t{city}\tState\tS\tDistrict\tD\tCounty\tC\t52.5\t13.4\t4"
            for city in ("Alpha", "Beta", "Gamma", "Gamma")
        ))
    geo._public_file = lambda name, url, download: buffer.getvalue() if name == "DE.zip" else None
    for _ in range(2):
        geo.load(download=False)
        places = geo.places({"12345": "BER"})
        assert {place.city for place in places} == {"Alpha", "Beta", "Gamma"}
        assert len(places) == 3
        assert all(place.chapter_id == "BER" and place.region == "County" for place in places)
        assert len({place.id for place in places}) == 3
