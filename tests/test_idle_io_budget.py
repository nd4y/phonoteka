"""Reproducible synthetic workload: no audio/providers, same fixture on old/new code."""
import json
import hashlib
import os
from pathlib import Path
import time
import uuid

from phonoteka import db, provider_policy
from phonoteka.metrics import DatabaseCollector
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
    metadata = db.encode({'format': 'mp3', 'lrc': '[00:00]fixture', 'padding': 'z' * (64*1024)})
    with db.transaction(settings.db_path) as conn:
        for index in range(count):
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'phonoteka-idle-fixture-' + str(index)))
            conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (identity, str(index)+'.mp3', 'a'*64, metadata, stamp, stamp))
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (identity, 'a'*64, analysis, stamp))
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,completed,created_at,updated_at) VALUES(?,'scan','succeeded','succeeded',?,1,1,?,?)", (identity, payload, stamp, stamp))
            for item in range(10):
                conn.execute('INSERT INTO job_items VALUES(?,?,?,?)', (identity, str(item), 'succeeded', db.encode({'padding': 'i'*4096})))
                db.event(conn, 'job_progress', {'job_id': identity, 'state': 'succeeded', 'completed': item+1, 'failed': 0})
        for index in range(20):
            conn.execute('INSERT INTO provider_cache VALUES(?,?,?,?)',
                         ('cache-'+str(index), 'musicbrainz', 'ю' * (256 * 1024), 1900000000))
        # Production has many large, unrelated provider settings. LIKE prefixes
        # must filter keys before reading values, including the shared prefix.
        for index in range(12000):
            conn.execute('INSERT INTO settings VALUES(?,?)',
                         ('provider_fixture:'+str(index), db.encode({'padding': 's'*1024})))
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

    metric_samples = []
    def metrics():
        with db.connect(settings.db_path) as conn:
            conn.execute('PRAGMA query_only=ON')
            families = DatabaseCollector(settings).application(conn, 1800000000)
        metric_samples.extend(sorted((sample.name, sorted(sample.labels.items()), sample.value)
                                     for family in families for sample in family.samples))

    result = {'improved': improved, 'synthetic_only': True, 'jobs': count, 'analyses': count,
              'job_payload_bytes': len(payload.encode()), 'analysis_payload_bytes': len(analysis.encode()),
              'cache_entries': 20, 'cache_payload_characters': 256*1024,
              'track_metadata_bytes': len(metadata.encode()), 'job_items': count*10,
              'unrelated_settings': 12000, 'setting_payload_bytes': 1024,
              'db_bytes': settings.db_path.stat().st_size, 'polls': 5, 'idle_cleanup_rounds': 3,
              'polling': measure(polling), 'housekeeping': measure(housekeeping), 'metrics': measure(metrics),
              'metric_samples': len(metric_samples), 'metric_sha256': hashlib.sha256(json.dumps(metric_samples,sort_keys=True).encode()).hexdigest()}
    target = os.environ.get('PHONOTEKA_IO_BENCHMARK_REPORT')
    if target:
        with Path(target).open('x', encoding='utf-8') as out:
            json.dump(result, out, indent=2)
    if improved:
        # Logical syscall bytes remain meaningful with a warm OS page cache.
        assert result['polling']['rchar'] < 16 * 1024**2, result
        assert result['housekeeping']['rchar'] < 2 * 1024**2, result
        if db.fetchone(settings.db_path, "SELECT 1 FROM sqlite_master WHERE name='analysis_status'"):
            assert result['metrics']['rchar'] < 4 * 1024**2, result
