import asyncio
from dataclasses import replace
from email.utils import formatdate
import json
import math
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from phonoteka import db, provider_attempts, providers
from phonoteka.app import create_app
from phonoteka.config import Settings
from phonoteka.metrics import DatabaseCollector
from phonoteka.providers import Providers, ProviderError
from test_backend import settings, authenticated


def test_zero_is_explicit_and_negative_quota_is_rejected(monkeypatch):
    monkeypatch.setenv('PROVIDER_DAILY_LIMIT', '0')
    assert Settings.from_env().provider_daily_limit == 0
    monkeypatch.setenv('PROVIDER_DAILY_LIMIT', '-1')
    with pytest.raises(ValueError, match='PROVIDER_DAILY_LIMIT'):
        Settings.from_env()


def test_disabling_daily_quota_preserves_existing_accounting_and_fingerprint_limit(settings):
    db.initialize(settings.db_path)
    with db.transaction(settings.db_path) as conn:
        conn.executemany('INSERT INTO provider_calls(provider,at) VALUES(?,?)',
                         [('lrclib', time.time())] * 2500)
    cfg = replace(settings, provider_daily_limit=0)
    attempt = provider_attempts.reserve(cfg, 'lrclib')
    provider_attempts.finish(cfg, attempt['id'], 'succeeded', 'response_received', 200)
    assert db.fetchone(cfg.db_path, "SELECT COUNT(*) n FROM provider_calls WHERE provider='lrclib'")['n'] == 2501
    with pytest.raises(provider_attempts.UsageError, match='budget_exhausted'):
        provider_attempts.reserve(replace(cfg, provider_daily_limit=2500), 'lrclib')
    with provider_attempts.recognition_scope('b' * 64, 'unchanged-per-recording-budget'):
        for _ in range(3):
            attempt = provider_attempts.reserve(cfg, 'shazam', 'fingerprint')
            provider_attempts.finish(cfg, attempt['id'], 'succeeded', 'response_received', 200)
        with pytest.raises(provider_attempts.UsageError, match='recognition_attempt_budget_exhausted'):
            provider_attempts.reserve(cfg, 'acoustid', 'fingerprint')


def test_unlimited_api_and_prometheus_are_not_reported_as_exhausted(settings):
    cfg = replace(settings, provider_daily_limit=0)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        authenticated(client, cfg)
        value = client.get('/api/providers').json()
        assert value['daily_limit'] == 0 and value['daily_limit_enabled'] is False
        samples = [s for family in DatabaseCollector(settings=cfg).collect() for s in family.samples]
        remaining = [s.value for s in samples if s.name == 'phonoteka_provider_budget_remaining']
        assert remaining and all(math.isinf(v) and v > 0 for v in remaining)
        assert next(s.value for s in samples if s.name == 'phonoteka_provider_daily_limit_enabled') == 0


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_stops_queued_requests_and_survives_client_restart(settings, monkeypatch, status):
    db.initialize(settings.db_path)
    cfg = replace(settings, provider_daily_limit=0)
    monkeypatch.setitem(providers.INTERVAL, 'lrclib', 0)
    calls = []
    async def response(request):
        calls.append(request)
        await asyncio.sleep(.02)  # other requests enter before the first response
        return httpx.Response(status, headers={'Retry-After': '120'}, text='upstream busy, not JSON')
    async def run():
        p = Providers(cfg, httpx.MockTransport(response))
        async def one(index):
            try:
                await p.get('lrclib', 'https://lrclib.net/api/search', {'track_name': str(index)})
            except ProviderError as exc:
                return exc.code
        try:
            result = await asyncio.gather(*(one(i) for i in range(4)))
            assert result.count('rate_limited') == (1 if status == 429 else 0)
            assert result.count('provider_cooldown') == (3 if status == 429 else 4)
        finally:
            await p.close()
        restarted = Providers(cfg, httpx.MockTransport(response))
        try:
            with pytest.raises(ProviderError, match='Источник временно отключен') as error:
                await restarted.get('lrclib', 'https://lrclib.net/api/search', {'track_name': 'restart'})
            assert error.value.code == 'provider_cooldown'
        finally:
            await restarted.close()
    asyncio.run(run())
    assert len(calls) == 1
    assert db.fetchone(cfg.db_path, 'SELECT COUNT(*) n FROM provider_calls')['n'] == 1
    breaker = json.loads(db.setting(cfg.db_path, 'provider_breaker:lrclib'))
    assert breaker['until'] > time.time() + 115


def test_retry_after_accepts_http_date_and_never_shortens_long_pauses(monkeypatch):
    now = 1700000000
    monkeypatch.setattr(providers.time, 'time', lambda: now)
    monkeypatch.setattr(providers.random, 'uniform', lambda a, b: 0)
    assert providers.retry_after_seconds(formatdate(now + 7200, usegmt=True)) == 7200
    assert providers.retry_after_seconds('172800') == 172800
    assert providers.retry_after_seconds('0') == 1
    assert providers.retry_after_seconds('invalid') == 60
