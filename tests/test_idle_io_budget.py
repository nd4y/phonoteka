"""Reproducible synthetic workload: no audio/providers, same fixture on old/new code."""
import json
import os
from pathlib import Path
import time
import uuid

from phonoteka import db, provider_policy
from test_backend import authenticated, client, settings


def counters():
    values = {}
    for line in Path('/proc/self/io').read_text().splitlines():
        key, value = line.split(':')
        values[key] = int(value)
    return values


def measure(action):
    before = counters(); start = time.monotonic()
    action()
    after = counters()
    return {'seconds': round(time.monotonic()-start, 4),
            **{k: max(0, after[k]-before[k]) for k in ('rchar', 'wchar', 'read_bytes', 'write_bytes', 'cancelled_write_bytes')}}


def test_idle_io_same_workload(client, settings):
    authenticated(client, settings)
    count = 300
    stamp = db.utcnow()
    payload = db.encode({'items': [], 'padding': 'x' * (128 * 1024)})
    analysis = db.encode({'candidates': [], 'padding': 'y' * (256 * 1024)})
    with db.transaction(settings.db_path) as conn:
        for index in range(count):
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'phonoteka-idle-fixture-' + str(index)))
            conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (identity, str(index)+'.mp3', 'a'*64, '{}', stamp, stamp))
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (identity, 'a'*64, analysis, stamp))
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,completed,created_at,updated_at) VALUES(?,'scan','succeeded','succeeded',?,1,1,?,?)", (identity, payload, stamp, stamp))
            for item in range(10):
                db.event(conn, 'job_progress', {'job_id': identity, 'state': 'succeeded', 'completed': item+1, 'failed': 0})
    improved = hasattr(provider_policy, 'cleanup_batch')
    if improved:
        while provider_policy.cleanup_batch(settings, limit=64)['pending']:
            pass
    else:
        provider_policy.cleanup_cache(settings)

    def polling():
        for _ in range(5):
            response = client.get('/api/jobs?active_first=true&recoverable_first=true')
            assert response.status_code == 200 and response.json()['total'] == count
            assert len(response.json()['items']) == 50

    def housekeeping():
        for _ in range(3):
            result = provider_policy.cleanup_batch(settings) if improved else provider_policy.cleanup_cache(settings)
            if improved:
                assert result['scanned'] == 0 and not result['pending']

    result = {'improved': improved, 'synthetic_only': True, 'jobs': count, 'analyses': count,
              'job_payload_bytes': len(payload.encode()), 'analysis_payload_bytes': len(analysis.encode()),
              'db_bytes': settings.db_path.stat().st_size, 'polls': 5, 'idle_cleanup_rounds': 3,
              'polling': measure(polling), 'housekeeping': measure(housekeeping)}
    target = os.environ.get('PHONOTEKA_IO_BENCHMARK_REPORT')
    if target:
        with Path(target).open('x', encoding='utf-8') as out:
            json.dump(result, out, indent=2)
    if improved:
        # Logical syscall bytes remain meaningful with a warm OS page cache.
        assert result['polling']['rchar'] < 16 * 1024**2, result
        assert result['housekeeping']['rchar'] < 2 * 1024**2, result
