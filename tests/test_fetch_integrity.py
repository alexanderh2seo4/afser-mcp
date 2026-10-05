import io
from urllib.error import HTTPError, URLError

import pytest

from afser_data.afser import AfserSource
from afser_data.source import IncompleteSource, SessionExpired, SourceError


def adapter(tmp_path, monkeypatch, outcomes):
    source = AfserSource({}, tmp_path)
    monkeypatch.setattr('afser_data.afser.time.sleep', lambda _: None)
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.get_method())
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return io.BytesIO(result)
    source.opener = Opener()
    return source, calls


def test_transient_read_error_recovers_without_losing_the_snapshot(tmp_path, monkeypatch):
    source, calls = adapter(tmp_path, monkeypatch, [HTTPError('https://www.afser.de/', 503, '', {}, None), URLError('private network detail'), b'recovered'])
    assert source._request('https://www.afser.de/') == b'recovered'
    assert calls == ['GET'] * 3


def test_failed_read_has_bounded_retries_and_no_private_exception_message(tmp_path, monkeypatch):
    source, calls = adapter(tmp_path, monkeypatch, [URLError('private') for _ in range(3)])
    with pytest.raises(SourceError) as error:
        source._request('https://www.afser.de/')
    assert str(error.value) == '' and len(calls) == 3


@pytest.mark.parametrize('status', [401, 403])
def test_authentication_error_is_not_retried(tmp_path, monkeypatch, status):
    source, calls = adapter(tmp_path, monkeypatch, [HTTPError('https://www.afser.de/', status, 'private', {}, None)])
    with pytest.raises(SessionExpired):
        source._request('https://www.afser.de/')
    assert calls == ['GET']


def test_login_post_is_never_replayed(tmp_path, monkeypatch):
    source, calls = adapter(tmp_path, monkeypatch, [HTTPError('https://www.afser.de/', 503, '', {}, None)])
    with pytest.raises(SourceError):
        source._request('https://www.afser.de/', b'synthetic-login')
    assert calls == ['POST']


def test_duplicate_ids_cannot_pass_a_complete_page_count(tmp_path):
    source = AfserSource({}, tmp_path)
    for task in ('getAllStudents', 'getAllFamiliesWithStudents'):
        with pytest.raises(IncompleteSource):
            source._complete_task(task, {'records':[{'Id':'duplicate'}, {'Id':'duplicate'}], 'totalSize':2, 'done':True}, [])
    with pytest.raises(IncompleteSource):
        source._complete_task('getAllStudents', {'records':[{'Id':'one'}], 'totalSize':True, 'done':True}, [])


def test_duplicate_partition_is_not_silently_deduplicated(tmp_path):
    source = AfserSource({}, tmp_path)
    row = {'Id':'a','Chapter__r':{'Chapter_Code__c':'MUC'}}
    source._task = lambda *args, **kwargs: {'records':[row,row], 'totalSize':2, 'done':True}
    with pytest.raises(IncompleteSource):
        source._complete_task('getAllStudents', {'records':[row], 'totalSize':2, 'done':False}, [{'Chapter_Code__c':'MUC'}])
