import gc
import json
import tracemalloc
import uuid

import pytest

from phonoteka import db, reports, review_queue
from test_backend import client, settings


def test_review_counts_release_large_text_fields_between_rows(client, settings):
    text = '[00:00.00]' + 'x' * (96 * 1024)
    metadata = db.encode({'format': 'mp3', 'tags': {}, 'lrc': text})
    proposal = db.encode({'tags': {}, 'lrc': text})
    revision = 'a' * 64
    count = 72
    with db.transaction(settings.db_path) as conn:
        now = db.utcnow()
        for index in range(count):
            identity = str(uuid.UUID(int=index + 1))
            conn.execute('''INSERT INTO tracks
                (id,relative_path,sha256,metadata,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''', (identity, f'{index:03}.mp3', revision, metadata, now, now))
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                (identity, identity, revision, proposal, now))
    gc.collect()
    tracemalloc.start()
    try:
        with db.connect(settings.db_path) as conn:
            result = review_queue.project(conn, settings)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result) == count
    assert all(item['proposal_state'] == 'no_changes' for item in result.values())
    assert review_queue.counts(result)['total'] == 0
    assert peak < 4 * 1024 * 1024, f'review counts retained lyric fields: {peak}'


@pytest.mark.parametrize('kind', ['json', 'csv'])
def test_export_does_not_retain_unreported_payloads(client, settings, kind):
    # Cached artwork/drafts and inspection details are deliberately much larger
    # than the public report. Its peak must not include every cached payload.
    private = 'unreported-synthetic-payload-' + 'x' * (128 * 1024)
    meta = db.encode({'format': 'mp3', 'tags': {'title': ['Example'],
        'artist': ['Artist'], 'album': ['Album']}, 'write_supported': True,
        'inspection_details': private})
    analysis = db.encode({'state': 'no_match', 'sources': [
        {'provider': 'lrclib', 'state': 'no_match'}], 'asr': {'text': private}})
    count = 72
    with db.transaction(settings.db_path) as conn:
        for index in range(count):
            identity = str(uuid.UUID(int=index + 1))
            conn.execute('''INSERT INTO tracks
                (id,relative_path,sha256,metadata,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''', (identity, f'{index:03}.mp3',
                'a' * 64, meta, db.utcnow(), db.utcnow()))
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',
                (identity, 'a' * 64, analysis, db.utcnow()))
    gc.collect()
    tracemalloc.start()
    try:
        result = reports.export(settings, kind)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 12 * 1024 * 1024, f'report retained unreported payloads: {peak}'
    assert 'unreported-synthetic-payload' not in result
    if kind == 'json':
        document = json.loads(result)
        assert len(document['files']) == count
        assert document['summary']['counts']['audio_files'] == count
        assert all(row['analysis_state'] == 'no_match' for row in document['files'])
        assert all(json.loads(row['analysis_sources']) == [
            {'provider': 'lrclib', 'state': 'no_match'}] for row in document['files'])
    else:
        import csv
        import io
        assert len(list(csv.DictReader(io.StringIO(result)))) == count
