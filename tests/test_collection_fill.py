from phonoteka import db, media, metadata_preparation, preparation
import pytest
from test_backend import accept_publication_review, drain
from test_confirmed_fields import library
from test_backend import settings


def start(client, headers, key):
    response = client.post('/api/preparation', json={'folder': '', 'autofill': True},
                           headers={**headers, 'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize('accepted', [False, True])
def test_one_start_applies_only_confirmed_fields_without_browser(library, accepted):
    cfg, client, headers, tid, sha, path = library
    packet_hash = media.audio_identity(path)
    if accepted:
        accept_publication_review(client, headers, tid)
    first = start(client, headers, 'autofill-integration')
    assert start(client, headers, 'autofill-integration')['job_id'] == first['job_id']
    for _ in range(20):
        drain(cfg)
        preparation.tick(cfg)
        metadata_preparation.tick(cfg)
        with db.connect(cfg.db_path) as conn:
            run = preparation.load(conn)
        if run['state'] != 'active':
            break
    assert run['state'] == 'review', run
    assert run['scanned_files'] == run['processed'] == 1
    assert run['applied'] == 1 and run['counts']['review'] == 1, run
    track = client.get('/api/tracks/' + tid).json()
    assert track['tags']['album'] == 'Confirmed album' and track['has_art']
    assert not track.get('lrc') and media.audio_identity(path) == packet_hash
    assert run.get('published', 0) == int(accepted)
    ready = cfg.publication_root / 'Ready' / path.name
    assert ready.exists() == accepted
    if accepted:
        assert media.sha256_file(ready) == media.sha256_file(path)
    assert run['reasons'] and db.fetchone(cfg.db_path, "SELECT count(*) AS n FROM history WHERE kind='apply'")['n'] == 1
    report = client.get('/api/preparation/' + run['id'] + '/report')
    assert report.status_code == 200 and path.name in report.text
    assert [row['id'] for row in client.get('/api/tracks', params={'filter': 'preparation'}).json()['items']] == [tid]


def test_stop_and_idempotency_before_first_write(library):
    cfg, client, headers, tid, sha, path = library
    run = start(client, headers, 'autofill-stop')
    stop = client.post('/api/preparation/' + run['id'] + '/stop', headers=headers)
    assert stop.status_code == 200 and stop.json()['state'] == 'stopped'
    drain(cfg)
    preparation.tick(cfg)
    assert start(client, headers, run['id'])['state'] == 'stopped'
    assert media.sha256_file(path) == sha


def test_single_byte_files_show_actionable_scan_error(library):
    cfg,client,headers,_,_,_=library
    path=cfg.work_root/'broken.webm'
    path.write_bytes(b'\x00')
    run=start(client,headers,'payload-test')
    drain(cfg);preparation.tick(cfg)
    report=client.get('/api/preparation/'+run['id']+'/report').text
    assert 'no_audio_payload' in report and 'копия со звуком' in report
    assert client.get('/api/preparation/'+run['id']+'/scan-errors').json()['items'][0]['code']=='no_audio_payload'
    assert path.read_bytes()==b'\x00'
