import asyncio
import io
import json

import pytest
from starlette.requests import ClientDisconnect

from phonoteka import db, reports
from test_backend import settings, client, authenticated, seed


@pytest.mark.parametrize('kind', ['json', 'csv'])
def test_file_report_keeps_snapshot_fields_and_csv_escaping(client, settings, monkeypatch, kind):
    authenticated(client, settings)
    identity, _ = seed(settings, relative='song.mp3')
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['tags']['title'] = ['=Пример 🎵']
        meta['youtube'] = {'evidence': 'Synthetic Unicode evidence 🎵'}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), identity))
    monkeypatch.setattr(db, 'utcnow', lambda:'2026-01-01T00:00:00+00:00')
    expected = reports.export(settings, kind)
    streams = []
    original = reports.export_file

    def capture(*args, **kwargs):
        stream = original(*args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(reports, 'export_file', capture)
    response = client.get('/api/reports/export/' + kind)
    assert response.status_code == 200
    assert response.content == expected.encode('utf-8')
    assert streams[0].closed
    assert response.headers['cache-control'] == 'private, no-store'
    assert 'attachment;' in response.headers['content-disposition']
    if kind == 'json':
        assert response.json()['files'][0]['title'] == '=Пример 🎵'
    else:
        assert "'=Пример 🎵" in response.text


def test_report_file_closes_when_snapshot_generation_fails(settings, monkeypatch):
    streams = []
    original = reports.tempfile.TemporaryFile

    def capture(*args, **kwargs):
        stream = original(*args, **kwargs)
        streams.append(stream)
        return stream

    def fail(*args, output, **kwargs):
        output.write('partial report')
        raise OSError('synthetic full disk')

    monkeypatch.setattr(reports.tempfile, 'TemporaryFile', capture)
    monkeypatch.setattr(reports, 'export', fail)
    with pytest.raises(OSError, match='synthetic full disk'):
        reports.export_file(settings, 'json')
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize('failure', [OSError, asyncio.CancelledError])
def test_report_response_closes_on_send_failure_or_cancellation(failure):
    stream = io.StringIO('x' * (128 * 1024))
    response = reports.ExportResponse(stream, media_type='application/json')

    async def receive():
        return {'type':'http.request'}

    async def send(message):
        if message['type'] == 'http.response.body':
            raise failure()

    async def run():
        with pytest.raises(ClientDisconnect if failure is OSError else asyncio.CancelledError):
            await response({'type':'http','asgi':{'spec_version':'2.4'}}, receive, send)

    asyncio.run(run())
    assert stream.closed
